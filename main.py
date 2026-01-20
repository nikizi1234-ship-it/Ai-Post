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
                    post_type TEXT
                )
            ''')
            
            # Индексы для ускорения поиска
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_post_hash ON sent_posts(post_hash)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_sent_date ON sent_posts(sent_date)')
            
            conn.commit()
    
    def is_post_sent(self, post_hash):
        """Проверяет, был ли пост уже отправлен"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM sent_posts WHERE post_hash = ?', (post_hash,))
            return cursor.fetchone() is not None
    
    def mark_post_as_sent(self, post_hash, title, link, source, post_type):
        """Помечает пост как отправленный"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR IGNORE INTO sent_posts 
                    (post_hash, title, link, source, post_type) 
                    VALUES (?, ?, ?, ?, ?)
                ''', (post_hash, title, link, source, post_type))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Ошибка при сохранении поста в БД: {e}")
            return None
    
    def get_total_sent_posts(self):
        """Возвращает общее количество отправленных постов"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) as count FROM sent_posts')
            result = cursor.fetchone()
            return result['count'] if result else 0
    
    def get_recent_posts(self, limit=10):
        """Возвращает последние отправленные посты"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                SELECT title, link, source, sent_date, post_type 
                FROM sent_posts 
                ORDER BY sent_date DESC 
                LIMIT ?
            ''', (limit,))
            return cursor.fetchall()
    
    def cleanup_old_posts(self, days_to_keep=30):
        """Удаляет старые записи (для поддержания размера БД)"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM sent_posts 
                    WHERE sent_date < datetime('now', ?)
                ''', (f'-{days_to_keep} days',))
                deleted_count = cursor.rowcount
                conn.commit()
                
                if deleted_count > 0:
                    logger.info(f"Удалено {deleted_count} старых записей из БД")
                return deleted_count
        except Exception as e:
            logger.error(f"Ошибка при очистке старых постов: {e}")
            return 0

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
                'hashtags': '#Хабр #Программирование #IT'
            },
            {
                'url': 'https://www.opennet.ru/opennews/opennews_all.rss',
                'name': 'OpenNet',
                'hashtags': '#OpenNet #Linux #OpenSource'
            }
        ]
    
    def generate_post_hash(self, post_data):
        """Генерирует уникальный хэш для поста на основе заголовка и ссылки"""
        content = f"{post_data.get('title', '')}{post_data.get('link', '')}"
        return hashlib.md5(content.encode('utf-8')).hexdigest()
    
    def fetch_all_articles(self):
        """Получаем ВСЕ статьи из RSS (и новые, и старые)."""
        all_articles = []
        
        for feed_config in self.feeds:
            url = feed_config['url']
            try:
                feed = feedparser.parse(url)
                
                for entry in feed.entries:
                    # Извлекаем текст из описания
                    summary_html = entry.get('summary', '')
                    summary_text = self._clean_html(summary_html)
                    
                    # Генерируем уникальный ID для статьи
                    article_data = {
                        'id': entry.get('id', entry.link),
                        'title': entry.title,
                        'link': entry.link,
                        'summary': summary_text,
                        'published': entry.get('published', ''),
                        'source': feed.feed.get('title', feed_config['name']),
                        'hashtags': feed_config['hashtags']
                    }
                    
                    # Генерируем хэш для уникальной идентификации
                    article_data['hash'] = self.generate_post_hash(article_data)
                    
                    all_articles.append(article_data)
                    
            except Exception as e:
                logger.error(f"Ошибка при парсинге {url}: {e}")
        
        return all_articles
    
    def _clean_html(self, html_text):
        """Очищает HTML-текст."""
        if not html_text:
            return ""
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            
            # Удаляем все теги img и script
            for tag in soup.find_all(['img', 'script', 'style']):
                tag.decompose()
            
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
    
    def find_post_to_send(self):
        """
        Находим пост для отправки:
        1. Сначала ищем НОВЫЕ посты (еще не отправленные)
        2. Если новых нет, ищем СТАРЫЕ неотправленные
        3. Если все отправлено - возвращаем None
        """
        all_articles = self.fetch_all_articles()
        
        if not all_articles:
            logger.warning("Не удалось получить статьи из RSS")
            return None, None
        
        # Сортируем по дате публикации (новые сначала)
        try:
            all_articles.sort(key=lambda x: x.get('published', ''), reverse=True)
        except:
            pass
        
        # 1. Ищем новые посты (еще не отправленные)
        for article in all_articles:
            if not self.db.is_post_sent(article['hash']):
                return article, "новая"
        
        # 2. Если все новые уже отправлены, ищем любую неотправленную (старую)
        # Это резервный вариант, если RSS не обновлялся
        for article in all_articles:
            if not self.db.is_post_sent(article['hash']):
                return article, "старая"
        
        # 3. Если ВСЕ статьи уже отправлены
        logger.info(f"Все статьи из RSS уже были отправлены ранее. Всего в БД: {self.db.get_total_sent_posts()}")
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
        
        # Текущая дата для подписи
        current_date = datetime.now().strftime("%d.%m.%Y")
        date_info = f"\n\n📅 Информация на {current_date}"
        
        # Формируем финальный пост
        post = f"""📰 {title}

💡 *Источник:* {article['source']}

💭 *Краткое содержание:*
{reasoning}

📖 [Читать статью полностью]({article['link']})

{date_info}

{article.get('hashtags', '#ITНовости #Технологии')}"""
        
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
                disable_web_page_preview=True  # Отключаем предпросмотр ссылки
            )
            
            # Сохраняем пост в базу данных
            post_id = self.db.mark_post_as_sent(
                article['hash'],
                article['title'],
                article['link'],
                article['source'],
                post_type
            )
            
            if post_id:
                logger.info(f"Успешно отправлена статья (тип: {post_type}): {article['title'][:50]}... (ID: {post_id})")
            else:
                logger.info(f"Статья отправлена, но не сохранена в БД (возможно, дубликат): {article['title'][:50]}...")
            
            # Периодически чистим старые записи
            if self.db.get_total_sent_posts() % 50 == 0:  # Каждые 50 записей
                self.db.cleanup_old_posts(days_to_keep=60)
            
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
                    text=f"⚠️ На {datetime.now().strftime("%d.%m.%Y")} новых IT-новостей не найдено. "
                         f"Следующая проверка через 6 часов.\n"
                         f"Всего отправлено постов: {self.db.get_total_sent_posts()}",
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления: {e}")

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

@app.route('/')
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
            'message': 'Статья отправлена (или нет доступных)',
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
        recent_posts = db.get_recent_posts(limit=5)
        
        stats = {
            'total_posts': total_posts,
            'recent_posts': [
                {
                    'title': post['title'],
                    'date': post['sent_date'],
                    'type': post['post_type']
                }
                for post in recent_posts
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
