import os
import logging
import feedparser
import requests
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
from bs4 import BeautifulSoup
import json
import asyncio
from flask import Flask
import re

# --- Конфигурация ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHANNEL_ID = os.environ.get('CHANNEL_ID')

if not BOT_TOKEN or not CHANNEL_ID:
    raise ValueError("Не заданы BOT_TOKEN или CHANNEL_ID.")

# --- Настройка ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# Файл для хранения истории отправленных статей
SENT_POSTS_FILE = 'sent_posts.json'

class ITNewsBot:
    def __init__(self, token, channel_id):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        self.feeds = [
            "https://habr.com/ru/rss/hubs/all/",
            "https://www.opennet.ru/opennews/opennews_all.rss",
        ]
        # Загружаем историю отправленных статей
        self.sent_posts = self.load_sent_posts()

    def load_sent_posts(self):
        """Загружаем список уже отправленных статей из файла."""
        try:
            if os.path.exists(SENT_POSTS_FILE):
                with open(SENT_POSTS_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Ошибка загрузки истории: {e}")
        return []

    def save_sent_posts(self):
        """Сохраняем список отправленных статей в файл."""
        try:
            with open(SENT_POSTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.sent_posts, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Ошибка сохранения истории: {e}")

    def fetch_all_articles(self):
        """Получаем ВСЕ статьи из RSS (и новые, и старые)."""
        all_articles = []
        for url in self.feeds:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries:
                    # Извлекаем текст из описания
                    summary_html = entry.get('summary', '')
                    summary_text = self._clean_html(summary_html)

                    article = {
                        'id': entry.get('id', entry.link),  # Уникальный ID статьи
                        'title': entry.title,
                        'link': entry.link,
                        'summary': summary_text,
                        'published': entry.get('published', ''),
                        'source': feed.feed.get('title', url)
                    }
                    all_articles.append(article)
            except Exception as e:
                logger.error(f"Ошибка при парсинге {url}: {e}")
        return all_articles

    def _clean_html(self, html_text):
        """Очищает HTML-текст."""
        if not html_text:
            return ""
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            text = soup.get_text(separator=' ', strip=True)
            text = re.sub(r'\s+', ' ', text)
            return text.strip()
        except Exception:
            return html_text

    def _truncate_text(self, text, max_length=500):
        """Обрезает текст до max_length."""
        if len(text) <= max_length:
            return text
        
        truncated = text[:max_length]
        last_sentence_end = max(
            truncated.rfind('.'),
            truncated.rfind('!'),
            truncated.rfind('?')
        )
        
        if last_sentence_end > 0 and last_sentence_end > max_length * 0.7:
            truncated = truncated[:last_sentence_end + 1]
        
        return truncated + "..."

    def find_post_to_send(self):
        """
        Находим пост для отправки:
        1. Сначала ищем НОВЫЕ посты (еще не отправленные)
        2. Если новых нет, ищем СТАРЫЕ неотправленные
        3. Если все отправлено - возвращаем None
        """
        all_articles = self.fetch_all_articles()
        
        # Сортируем по дате публикации (новые сначала)
        try:
            all_articles.sort(key=lambda x: x.get('published', ''), reverse=True)
        except:
            pass

        # 1. Ищем новые посты (еще не отправленные)
        for article in all_articles:
            if article['id'] not in self.sent_posts:
                return article, "новая"

        # 2. Если все новые уже отправлены, ищем любую неотправленную (старую)
        # Это резервный вариант, если RSS не обновлялся
        for article in all_articles:
            if article['id'] not in self.sent_posts:
                return article, "старая"

        # 3. Если ВСЕ статьи уже отправлены
        logger.info("Все статьи из RSS уже были отправлены ранее.")
        return None, None

    def create_post(self, article, post_type):
        """Форматируем пост для Telegram с датой внизу."""
        # Обработка заголовка
        title = article['title']
        if len(title) > 200:
            title = title[:197] + "..."

        # Обработка описания
        reasoning = article['summary']
        if not reasoning or reasoning.strip() == "":
            reasoning = f"Статья '{title[:50]}...' не содержит описания."

        # Интеллектуальное сокращение
        reasoning = self._truncate_text(reasoning, 800)

        # Хештеги в зависимости от источника
        source = article['source'].lower()
        if 'habr' in source:
            hashtags = "#Хабр #Программирование #IT"
        elif 'opennet' in source:
            hashtags = "#OpenNet #Linux #OpenSource"
        else:
            hashtags = "#ITНовости #Технологии"

        # Текущая дата для подписи
        current_date = datetime.now().strftime("%d.%m.%Y")
        date_info = f"\n\n📅 Информация на {current_date}"

        # Добавляем пометку о типе поста (только для логов, не в публикацию)
        type_marker = ""
        if post_type == "старая":
            logger.info("Отправляется старая, но еще не отправленная статья.")

        # Формируем финальный пост
        post = f"""📰 {title}

💡 *Источник:* {article['source']}

💭 *Краткое содержание:*
{reasoning}

📖 [Читать статью полностью]({article['link']})

{date_info}

{hashtags}"""
        
        return post

    async def send_post(self, article, post_type):
        """Отправляем пост в канал и сохраняем в историю."""
        if not article:
            logger.warning("Нет статей для отправки.")
            return False

        post_content = self.create_post(article, post_type)
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=post_content,
                parse_mode='Markdown',
                disable_web_page_preview=True,
            )
            
            # Сохраняем ID отправленной статьи
            if article['id'] not in self.sent_posts:
                self.sent_posts.append(article['id'])
                # Ограничиваем размер истории (последние 500 статей)
                if len(self.sent_posts) > 500:
                    self.sent_posts = self.sent_posts[-500:]
                self.save_sent_posts()
            
            logger.info(f"Успешно отправлена статья (тип: {post_type}): {article['title'][:50]}...")
            return True
            
        except TelegramError as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False
        except Exception as e:
            logger.error(f"Неизвестная ошибка: {e}")
            return False

    async def run(self):
        """Главная функция для запуска бота."""
        logger.info("Начинаем поиск статьи для отправки...")
        
        # Ищем пост для отправки
        article, post_type = self.find_post_to_send()
        
        if article:
            success = await self.send_post(article, post_type)
            if success:
                logger.info("Задача успешно выполнена.")
            else:
                logger.error("Не удалось отправить статью.")
        else:
            # Если вообще нет статей для отправки
            logger.warning("Нет доступных статей для отправки (все уже были отправлены).")
            try:
                await self.bot.send_message(
                    chat_id=self.channel_id,
                    text=f"⚠️ На {datetime.now().strftime('%d.%m.%Y')} новых IT-новостей не найдено. Следующая проверка через 6 часов."
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")

# --- Flask маршруты ---
is_running = False

@app.route('/health')
def health():
    return "Bot is alive", 200

@app.route('/run')
def run_bot():
    global is_running
    if is_running:
        logger.info("Задача уже выполняется, пропускаем.")
        return "Задача уже выполняется", 429
    
    is_running = True
    try:
        logger.info("Запуск бота...")
        bot = ITNewsBot(BOT_TOKEN, CHANNEL_ID)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.run())
        
        logger.info("Задача выполнена.")
        return "Статья отправлена (или нет доступных)", 200
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        return f"Ошибка: {e}", 500
    finally:
        is_running = False

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
