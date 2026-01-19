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
from flask import Flask, Response
from threading import Thread
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
is_running = False

class ITNewsBot:
    def __init__(self, token, channel_id):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        self.feeds = [
            "https://habr.com/ru/rss/hubs/all/",
            "https://www.opennet.ru/opennews/opennews_all.rss",
        ]

    def fetch_news(self):
        """Получение новостей из RSS с обработкой ошибок."""
        all_articles = []
        for url in self.feeds:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:3]:
                    # Извлекаем текст из описания, удаляя HTML-теги
                    summary_html = entry.get('summary', '')
                    summary_text = self._clean_html(summary_html)

                    article = {
                        'title': entry.title,
                        'link': entry.link,
                        'summary': summary_text,  # Теперь здесь чистый текст
                        'published': entry.get('published', ''),
                        'source': feed.feed.get('title', url)
                    }
                    all_articles.append(article)
            except Exception as e:
                logger.error(f"Ошибка при парсинге {url}: {e}")
        return all_articles

    def _clean_html(self, html_text):
        """Очищает HTML-текст, оставляя только читаемый текст."""
        if not html_text:
            return ""
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            # Удаляем все теги, оставляя только текст
            text = soup.get_text(separator=' ', strip=True)
            # Убираем лишние пробелы и переносы строк
            text = re.sub(r'\s+', ' ', text)
            return text.strip()
        except Exception as e:
            logger.error(f"Ошибка очистки HTML: {e}")
            return html_text  # Возвращаем оригинал в случае ошибки

    def _truncate_text(self, text, max_length=500):
        """Обрезает текст до max_length, стараясь закончить на границе предложения."""
        if len(text) <= max_length:
            return text
        
        # Обрезаем до max_length
        truncated = text[:max_length]
        
        # Ищем последнюю точку, восклицательный или вопросительный знак
        last_sentence_end = max(
            truncated.rfind('.'),
            truncated.rfind('!'),
            truncated.rfind('?')
        )
        
        # Если нашли конец предложения, обрезаем там
        if last_sentence_end > 0 and last_sentence_end > max_length * 0.7:
            truncated = truncated[:last_sentence_end + 1]
        
        return truncated + "..."

    def create_post(self, article):
        """Форматирование статьи в пост для Telegram."""
        # Обработка заголовка
        title = article['title']
        if len(title) > 200:
            title = title[:197] + "..."
        
        # Обработка описания (рассуждений)
        reasoning = article['summary']
        
        # Если описание пустое, используем заголовок
        if not reasoning or reasoning.strip() == "":
            reasoning = f"Статья '{title[:50]}...' не содержит описания. Рекомендуем перейти по ссылке для полного ознакомления."
        else:
            # Обрезаем текст интеллектуально
            reasoning = self._truncate_text(reasoning, 800)  # Увеличил лимит для рассуждений
        
        # Формируем хештеги на основе источника
        source = article['source'].lower()
        if 'habr' in source:
            hashtags = "#Хабр #Программирование #IT"
        elif 'opennet' in source:
            hashtags = "#OpenNet #Linux #OpenSource"
        else:
            hashtags = "#ITНовости #Технологии"
        
        # Создаем красивый пост
        post = f"""📰 {title}

💡 *Источник:* {article['source']}

💭 *Краткое содержание:*
{reasoning}

📖 [Читать статью полностью]({article['link']})

{hashtags}"""
        
        return post

    async def send_post(self, article):
        """Отправка поста в канал."""
        post_content = self.create_post(article)
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=post_content,
                parse_mode='Markdown',
                disable_web_page_preview=False
            )
            logger.info(f"Новость отправлена: {article['title'][:50]}...")
            return True
        except TelegramError as e:
            logger.error(f"Ошибка отправки: {e}")
            return False

    async def run(self):
        """Главная функция для запуска бота."""
        logger.info("Запуск сбора и отправки новостей...")
        articles = self.fetch_news()
        
        if not articles:
            logger.warning("Новости не найдены.")
            # Можно отправить сообщение об отсутствии новостей
            try:
                await self.bot.send_message(
                    chat_id=self.channel_id,
                    text="⚠️ На данный момент новости не найдены. Следующая попытка через 6 часов."
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения об отсутствии новостей: {e}")
            return
        
        # Отправляем самую свежую статью
        latest_article = articles[0]
        success = await self.send_post(latest_article)
        
        if success:
            logger.info(f"Успешно отправлена новость: {latest_article['title'][:50]}...")
        else:
            logger.error("Не удалось отправить новость")

# --- Flask маршруты (остаются без изменений) ---
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
        logger.info("Запуск задачи по сбору и отправке новостей...")
        bot = ITNewsBot(BOT_TOKEN, CHANNEL_ID)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.run())
        
        logger.info("Задача успешно выполнена.")
        return "Новость отправлена", 200
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        return f"Ошибка: {e}", 500
    finally:
        is_running = False

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)
