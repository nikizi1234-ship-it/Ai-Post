import os
import logging
import feedparser
import sqlite3
from datetime import datetime
from telegram import Bot
from telegram.error import TelegramError
from bs4 import BeautifulSoup
import asyncio
from flask import Flask
import re
import hashlib
import requests
import time
from urllib.parse import urlparse

# --- Конфигурация ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHANNEL_ID = os.environ.get('CHANNEL_ID')

if not BOT_TOKEN or not CHANNEL_ID:
    raise ValueError("Не заданы BOT_TOKEN или CHANNEL_ID.")

# --- Настройка ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)
app = Flask(__name__)

# Настройки базы данных
DB_NAME = 'news_bot.db'

class DatabaseManager:
    """Класс для управления базой данных SQLite"""
    
    def __init__(self, db_name=DB_NAME):
        self.db_name = db_name
        self.init_database()
    
    def get_connection(self):
        """Создает соединение с базой данных"""
        conn = sqlite3.connect(self.db_name)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_database(self):
        """Инициализирует базу данных и создает таблицы"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Таблица для хранения отправленных постов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_hash TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    source TEXT NOT NULL,
                    sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    content_hash TEXT NOT NULL,
                    last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Таблица для хранения истории источников
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS source_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    article_count INTEGER DEFAULT 0,
                    last_check TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            
            # Индексы для ускорения поиска
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_post_hash ON sent_posts(post_hash)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_content_hash ON sent_posts(content_hash)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_last_seen ON sent_posts(last_seen)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_source_link ON sent_posts(source, link)')
            
            conn.commit()
    
    def is_post_sent(self, content_hash):
        """Проверяет, был ли пост уже отправлен по хэшу контента"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM sent_posts WHERE content_hash = ?', (content_hash,))
            return cursor.fetchone() is not None
    
    def get_post_by_url(self, url):
        """Получает пост по URL"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT * FROM sent_posts WHERE link = ?', (url,))
            result = cursor.fetchone()
            return dict(result) if result else None
    
    def mark_post_as_sent(self, post_hash, title, link, source, content_hash):
        """Помечает пост как отправленный"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO sent_posts 
                    (post_hash, title, link, source, content_hash, last_seen) 
                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ''', (post_hash, title, link, source, content_hash))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Ошибка при сохранении поста в БД: {e}")
            return None
    
    def update_source_stats(self, source, count):
        """Обновляет статистику источника"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR REPLACE INTO source_history 
                    (source, article_count, last_check) 
                    VALUES (?, ?, CURRENT_TIMESTAMP)
                ''', (source, count))
                conn.commit()
        except Exception as e:
            logger.error(f"Ошибка при обновлении статистики источника: {e}")
    
    def get_unsent_articles(self, articles, limit=5):
        """Возвращает неотправленные статьи из списка"""
        unsent = []
        for article in articles[:limit]:
            if not self.is_post_sent(article['content_hash']):
                unsent.append(article)
        return unsent
    
    def cleanup_old_posts(self, days_to_keep=90):
        """Удаляет старые записи (для поддержания размера БД)"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM sent_posts 
                    WHERE last_seen < datetime('now', ?)
                ''', (f'-{days_to_keep} days',))
                deleted_count = cursor.rowcount
                conn.commit()
                
                if deleted_count > 0:
                    logger.info(f"Удалено {deleted_count} старых записей из БД")
                return deleted_count
        except Exception as e:
            logger.error(f"Ошибка при очистке старых постов: {e}")
            return 0
    
    def get_total_sent_posts(self):
        """Возвращает общее количество отправленных постов"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as count FROM sent_posts')
            result = cursor.fetchone()
            return result['count'] if result else 0
    
    def get_stats_by_source(self):
        """Возвращает статистику по источникам"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT source, COUNT(*) as count, 
                       MAX(sent_date) as last_sent 
                FROM sent_posts 
                GROUP BY source 
                ORDER BY count DESC
            ''')
            return cursor.fetchall()

class ITNewsBot:
    def __init__(self, token, channel_id):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        self.db = DatabaseManager()
        
        # RSS-ленты для парсинга
        self.feeds = [
            {
                'url': 'https://habr.com/ru/rss/hubs/all/',
                'name': 'Habr',
                'hashtags': '#Хабр #Программирование #IT',
                'parser': self.parse_habr_article
            },
            {
                'url': 'https://www.opennet.ru/opennews/opennews_all.rss',
                'name': 'OpenNet',
                'hashtags': '#OpenNet #Linux #OpenSource',
                'parser': self.parse_opennet_article
            }
        ]
        
        # User-Agent для запросов
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
    
    def generate_content_hash(self, content):
        """Генерирует хэш контента статьи"""
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def generate_post_hash(self, title, link):
        """Генерирует хэш поста"""
        return hashlib.md5(f"{title}{link}".encode('utf-8')).hexdigest()
    
    def fetch_article_content(self, url):
        """Получает полный текст статьи по URL"""
        try:
            response = requests.get(url, headers=self.headers, timeout=10)
            response.raise_for_status()
            return response.text
        except Exception as e:
            logger.error(f"Ошибка при получении статьи {url}: {e}")
            return None
    
    def parse_habr_article(self, html_content):
        """Парсит статью с Habr"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Удаляем ненужные элементы
            for element in soup.find_all(['script', 'style', 'iframe', 'nav', 'header', 'footer']):
                element.decompose()
            
            # Ищем основной контент
            article_body = soup.find('div', {'class': 'tm-article-body'})
            if not article_body:
                article_body = soup.find('article')
            
            if article_body:
                # Ограничиваем длину контента
                text = article_body.get_text(separator='\n', strip=True)
                # Берем первые 2000 символов для хэширования
                return text[:2000]
            
            return None
        except Exception as e:
            logger.error(f"Ошибка парсинга Habr статьи: {e}")
            return None
    
    def parse_opennet_article(self, html_content):
        """Парсит статью с OpenNet"""
        try:
            soup = BeautifulSoup(html_content, 'html.parser')
            
            # Удаляем ненужные элементы
            for element in soup.find_all(['script', 'style', 'iframe', 'nav', 'header', 'footer']):
                element.decompose()
            
            # Ищем основной контент
            content_div = soup.find('div', id='text')
            if not content_div:
                content_div = soup.find('div', class_='content')
            
            if content_div:
                text = content_div.get_text(separator='\n', strip=True)
                # Берем первые 2000 символов для хэширования
                return text[:2000]
            
            return None
        except Exception as e:
            logger.error(f"Ошибка парсинга OpenNet статьи: {e}")
            return None
    
    def fetch_new_articles(self):
        """Получает новые статьи из RSS и проверяет их контент"""
        new_articles = []
        
        for feed_config in self.feeds:
            try:
                logger.info(f"Проверяем RSS: {feed_config['name']}")
                feed = feedparser.parse(feed_config['url'])
                
                # Берем только последние 10 статей из RSS
                recent_entries = feed.entries[:10] if feed.entries else []
                
                for entry in recent_entries:
                    try:
                        # Проверяем, есть ли уже такая статья
                        existing_post = self.db.get_post_by_url(entry.link)
                        if existing_post:
                            # Обновляем время последнего просмотра
                            self.db.mark_post_as_sent(
                                existing_post['post_hash'],
                                existing_post['title'],
                                existing_post['link'],
                                existing_post['source'],
                                existing_post['content_hash']
                            )
                            continue
                        
                        # Получаем полный текст статьи
                        html_content = self.fetch_article_content(entry.link)
                        if not html_content:
                            continue
                        
                        # Парсим контент
                        parsed_content = feed_config['parser'](html_content)
                        if not parsed_content:
                            continue
                        
                        # Генерируем хэш контента
                        content_hash = self.generate_content_hash(parsed_content)
                        
                        # Проверяем, не отправляли ли уже этот контент
                        if self.db.is_post_sent(content_hash):
                            logger.info(f"Контент уже был отправлен ранее: {entry.title[:50]}...")
                            continue
                        
                        # Создаем объект статьи
                        article = {
                            'title': entry.title[:200],
                            'link': entry.link,
                            'summary': self._clean_html(entry.get('summary', ''))[:500],
                            'source': feed_config['name'],
                            'hashtags': feed_config['hashtags'],
                            'content_hash': content_hash,
                            'post_hash': self.generate_post_hash(entry.title, entry.link),
                            'published': entry.get('published', ''),
                            'full_content': parsed_content[:1000]  # Для отладки
                        }
                        
                        new_articles.append(article)
                        logger.info(f"Найдена новая статья: {article['title'][:50]}...")
                        
                        # Делаем паузу между запросами
                        time.sleep(1)
                        
                    except Exception as e:
                        logger.error(f"Ошибка обработки статьи {entry.get('link', 'unknown')}: {e}")
                        continue
                
                # Обновляем статистику источника
                self.db.update_source_stats(feed_config['name'], len(recent_entries))
                
            except Exception as e:
                logger.error(f"Ошибка при парсинге RSS {feed_config['url']}: {e}")
                continue
        
        return new_articles
    
    def _clean_html(self, html_text):
        """Очищает HTML-текст."""
        if not html_text:
            return ""
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            text = soup.get_text(separator=' ', strip=True)
            text = re.sub(r'\s+', ' ', text)
            return text.strip()
        except Exception as e:
            logger.error(f"Ошибка очистки HTML: {e}")
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
    
    def create_post(self, article):
        """Форматируем пост для Telegram"""
        # Обработка заголовка
        title = article['title']
        if len(title) > 200:
            title = title[:197] + "..."
        
        # Обработка описания
        summary = article['summary']
        if not summary or summary.strip() == "":
            summary = f"Статья '{title[:50]}...' не содержит описания."
        
        # Интеллектуальное сокращение
        summary = self._truncate_text(summary, 800)
        
        # Текущая дата для подписи
        current_date = datetime.now().strftime("%d.%m.%Y")
        date_info = f"\n\n📅 Информация на {current_date}"
        
        # Формируем финальный пост
        post = f"""📰 {title}

💡 *Источник:* {article['source']}

💭 *Краткое содержание:*
{summary}

📖 [Читать статью полностью]({article['link']})

{date_info}

{article.get('hashtags', '#ITНовости #Технологии')}"""
        
        return post
    
    async def send_post(self, article):
        """Отправляем пост в канал"""
        try:
            post_content = self.create_post(article)
            
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=post_content,
                parse_mode='Markdown',
                disable_web_page_preview=True
            )
            
            # Сохраняем пост в базу данных
            post_id = self.db.mark_post_as_sent(
                article['post_hash'],
                article['title'],
                article['link'],
                article['source'],
                article['content_hash']
            )
            
            logger.info(f"Успешно отправлена статья: {article['title'][:50]}...")
            return True
            
        except TelegramError as e:
            logger.error(f"Ошибка отправки в Telegram: {e}")
            return False
        except Exception as e:
            logger.error(f"Неизвестная ошибка: {e}")
            return False
    
    async def run(self):
        """Главная функция для запуска бота."""
        logger.info("Начинаем поиск новых статей...")
        
        try:
            # Получаем новые статьи
            new_articles = self.fetch_new_articles()
            
            if not new_articles:
                logger.info("Нет новых статей для отправки.")
                
                # Отправляем статистику раз в 24 часа
                total_posts = self.db.get_total_sent_posts()
                last_check_file = 'last_stats_sent.txt'
                
                try:
                    with open(last_check_file, 'r') as f:
                        last_sent = datetime.fromisoformat(f.read().strip())
                        hours_since_last = (datetime.now() - last_sent).total_seconds() / 3600
                except:
                    hours_since_last = 25
                
                if hours_since_last >= 24:
                    try:
                        stats = self.db.get_stats_by_source()
                        stats_text = f"📊 *Статистика бота*\n\n"
                        stats_text += f"Всего отправлено постов: {total_posts}\n\n"
                        
                        for stat in stats:
                            stats_text += f"{stat['source']}: {stat['count']} постов\n"
                        
                        stats_text += f"\nПоследняя проверка: {datetime.now().strftime('%d.%m.%Y %H:%M')}"
                        
                        await self.bot.send_message(
                            chat_id=self.channel_id,
                            text=stats_text,
                            parse_mode='Markdown'
                        )
                        
                        with open(last_check_file, 'w') as f:
                            f.write(datetime.now().isoformat())
                        
                    except Exception as e:
                        logger.error(f"Ошибка отправки статистики: {e}")
                
                return
            
            # Отправляем новые статьи (максимум 2 за раз)
            sent_count = 0
            for article in new_articles[:2]:
                success = await self.send_post(article)
                if success:
                    sent_count += 1
                    # Пауза между отправками
                    await asyncio.sleep(2)
            
            logger.info(f"Отправлено {sent_count} новых статей.")
            
            # Периодически чистим старые записи
            if self.db.get_total_sent_posts() % 100 == 0:
                self.db.cleanup_old_posts(days_to_keep=60)
            
        except Exception as e:
            logger.error(f"Критическая ошибка в run(): {e}")

# --- Flask маршруты ---
is_running = False

@app.route('/health')
def health():
    """Маршрут для проверки работоспособности приложения"""
    try:
        db = DatabaseManager()
        post_count = db.get_total_sent_posts()
        return {
            'status': 'healthy',
            'database': 'connected',
            'total_posts': post_count,
            'timestamp': datetime.now().isoformat()
        }, 200
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/run')
def run_bot():
    """Основной маршрут для запуска бота (вызывается Cron Job)"""
    global is_running
    
    if is_running:
        logger.info("Задача уже выполняется, пропускаем.")
        return {'status': 'busy', 'message': 'Задача уже выполняется'}, 429
    
    is_running = True
    try:
        logger.info("Запуск бота...")
        bot = ITNewsBot(BOT_TOKEN, CHANNEL_ID)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.run())
        
        # Получаем статистику
        db = DatabaseManager()
        stats = {
            'status': 'completed',
            'message': 'Проверка новых статей завершена',
            'total_posts': db.get_total_sent_posts(),
            'timestamp': datetime.now().isoformat()
        }
        
        logger.info(f"Задача выполнена. Статистика: {stats}")
        return stats, 200
        
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        return {'status': 'error', 'message': str(e)}, 500
    finally:
        is_running = False

@app.route('/stats')
def get_stats():
    """Маршрут для получения статистики"""
    try:
        db = DatabaseManager()
        total_posts = db.get_total_sent_posts()
        source_stats = db.get_stats_by_source()
        
        stats = {
            'total_posts': total_posts,
            'sources': [
                {
                    'name': stat['source'],
                    'count': stat['count'],
                    'last_sent': stat['last_sent']
                }
                for stat in source_stats
            ],
            'timestamp': datetime.now().isoformat()
        }
        
        return stats, 200
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/')
def index():
    """Главная страница (для проверки работы)"""
    return """
    <h1>IT News Telegram Bot</h1>
    <p>Бот для автоматической отправки IT-новостей в Telegram-канал.</p>
    <p>Доступные эндпоинты:</p>
    <ul>
        <li><a href="/health">/health</a> - Проверка работоспособности</li>
        <li><a href="/stats">/stats</a> - Статистика бота</li>
        <li><a href="/run">/run</a> - Запуск бота (для Cron Job)</li>
    </ul>
    """

if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Запуск приложения на порту {port}")
    
    # Инициализируем базу данных при старте
    db = DatabaseManager()
    logger.info(f"База данных инициализирована. Всего постов: {db.get_total_sent_posts()}")
    
    app.run(host='0.0.0.0', port=port)
