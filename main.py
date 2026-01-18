import os
import logging
import feedparser
import requests
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
from bs4 import BeautifulSoup
import json

# --- Конфигурация (берётся из переменных окружения Render) ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHANNEL_ID = os.environ.get('CHANNEL_ID')  # Используйте числовой ID, например -1001234567890

if not BOT_TOKEN or not CHANNEL_ID:
    raise ValueError("Ошибка: не заданы BOT_TOKEN или CHANNEL_ID в переменных окружения.")

# --- Настройка логирования ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Основная логика бота ---
class ITNewsBot:
    def __init__(self, token, channel_id):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        # Список RSS -лент
        self.feeds = [
            "https://habr.com/ru/rss/hubs/all/",
            "https://www.opennet.ru/opennews/opennews_all.rss",
        ]

    def fetch_news(self):
        """Получение и обработка новостей из RSS."""
        all_articles = []
        for url in self.feeds:
            try:
                feed = feedparser.parse(url)
                for entry in feed.entries[:3]:  # Берём 3 последние новости из каждого источника
                    article = {
                        'title': entry.title,
                        'link': entry.link,
                        'summary': entry.get('summary', ''),
                        'published': entry.get('published', ''),
                        'source': feed.feed.get('title', url)
                    }
                    all_articles.append(article)
            except Exception as e:
                logger.error(f"Ошибка при парсинге {url}: {e}")
        return all_articles

    def create_post(self, article):
        """Форматирование статьи в пост для Telegram."""
        title = article['title'][:200] + "..." if len(article['title']) > 200 else article['title']
        reasoning = article['summary'][:500] + "..." if len(article['summary']) > 500 else article['summary']

        post = f"""📰 *{title}*

💭 *Рассуждения:*
{reasoning}

📖 [Читать полностью]({article['link']})

#ITНовости #Программирование #Технологии
"""
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
        """Главная функция, которую запускает Cron Job."""
        logger.info("Запуск сбора и отправки новостей...")
        articles = self.fetch_news()
        if not articles:
            logger.warning("Новости не найдены.")
            return

        # Отправляем самую свежую статью
        latest_article = articles[0]
        await self.send_post(latest_article)
        logger.info("Задача выполнена успешно.")

# --- Точка входа для Cron Job ---
def main():
    """Функция для запуска из командной строки."""
    import asyncio
    bot = ITNewsBot(BOT_TOKEN, CHANNEL_ID)
    asyncio.run(bot.run())

if __name__ == "__main__":
    main()
