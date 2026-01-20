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

# Ключевые слова для фильтрации IT-новостей (русский + английский)
IT_KEYWORDS = [
    # Программирование и разработка
    'программирование', 'разработка', 'код', 'github', 'git', 'api', 'sdk',
    'programming', 'development', 'code', 'software', 'developer',
    
    # Языки программирования
    'python', 'javascript', 'java', 'c++', 'c#', 'go', 'golang', 'rust',
    'php', 'ruby', 'swift', 'kotlin', 'typescript', 'html', 'css', 'sql',
    
    # Фреймворки и библиотеки
    'react', 'vue', 'angular', 'django', 'flask', 'spring', 'laravel',
    'node.js', 'express', 'jquery', 'bootstrap', 'tailwind',
    
    # Базы данных
    'база данных', 'sql', 'nosql', 'mysql', 'postgresql', 'mongodb',
    'redis', 'elasticsearch', 'database', 'db',
    
    # Операционные системы
    'linux', 'ubuntu', 'debian', 'windows', 'macos', 'ios', 'android',
    'unix', 'centos', 'fedora',
    
    # Инфраструктура и облака
    'docker', 'kubernetes', 'devops', 'ci/cd', 'aws', 'azure', 'gcp',
    'cloud', 'облако', 'сервер', 'хостинг', 'vps', 'виртуализация',
    
    # Безопасность
    'безопасность', 'security', 'кибербезопасность', 'hack', 'vulnerability',
    'уязвимость', 'шифрование', 'encryption', 'firewall',
    
    # Искусственный интеллект и данные
    'искусственный интеллект', 'ai', 'машинное обучение', 'ml',
    'нейросеть', 'нейронная сеть', 'data science', 'big data',
    'анализ данных', 'data analysis',
    
    # Мобильная разработка
    'мобильное приложение', 'android', 'ios', 'react native', 'flutter',
    
    # Веб-технологии
    'веб', 'web', 'сайт', 'интернет', 'браузер', 'chrome', 'firefox',
    'safari', 'http', 'https', 'ssl', 'tls', 'домен', 'хостинг',
    
    # Аппаратное обеспечение
    'процессор', 'cpu', 'gpu', 'видеокарта', 'nvidia', 'amd', 'intel',
    'оперативная память', 'ram', 'ssd', 'жесткий диск', 'hdd',
    
    # ИТ-компании и продукты
    'microsoft', 'google', 'apple', 'amazon', 'meta', 'facebook',
    'yandex', 'vk', 'telegram', 'whatsapp', 'discord',
    
    # Стандарты и протоколы
    'json', 'xml', 'rest', 'graphql', 'soap', 'websocket',
    
    # Методологии
    'agile', 'scrum', 'kanban', 'waterfall'
]

# RSS-ленты строго IT-тематики
IT_FEEDS = [
    {
        'url': 'https://habr.com/ru/rss/hub/programming/',
        'name': 'Habr Programming',
        'hashtags': '#Хабр #Программирование #Разработка',
        'categories': ['Программирование', 'IT']
    },
    {
        'url': 'https://habr.com/ru/rss/hub/infosecurity/',
        'name': 'Habr Security',
        'hashtags': '#Хабр #Безопасность #ИнфоСек',
        'categories': ['Безопасность', 'IT']
    },
    {
        'url': 'https://habr.com/ru/rss/hub/devops/',
        'name': 'Habr DevOps',
        'hashtags': '#Хабр #DevOps #Инфраструктура',
        'categories': ['DevOps', 'IT']
    },
    {
        'url': 'https://www.opennet.ru/opennews/opennews_all.rss',
        'name': 'OpenNet',
        'hashtags': '#OpenNet #Linux #OpenSource',
        'categories': ['Linux', 'Open Source', 'IT']
    },
    {
        'url': 'https://news.ycombinator.com/rss',
        'name': 'Hacker News',
        'hashtags': '#HackerNews #Tech #Programming',
        'categories': ['Technology', 'Programming', 'IT']
    },
    {
        'url': 'https://www.reddit.com/r/programming/.rss',
        'name': 'Reddit Programming',
        'hashtags': '#Reddit #Programming #Tech',
        'categories': ['Programming', 'IT']
    },
    {
        'url': 'https://www.reddit.com/r/linux/.rss',
        'name': 'Reddit Linux',
        'hashtags': '#Reddit #Linux #OpenSource',
        'categories': ['Linux', 'Open Source', 'IT']
    }
]

# Настройки базы данных
DB_NAME = 'it_news_bot.db'

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
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS sent_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    content_hash TEXT UNIQUE NOT NULL,
                    title TEXT NOT NULL,
                    link TEXT NOT NULL,
                    source TEXT NOT NULL,
                    category TEXT,
                    sent_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    it_score INTEGER DEFAULT 0
                )
            ''')
            
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_content_hash ON sent_posts(content_hash)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_it_score ON sent_posts(it_score)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_source ON sent_posts(source)')
            
            conn.commit()
    
    def is_post_sent(self, content_hash):
        """Проверяет, был ли пост уже отправлен"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT id FROM sent_posts WHERE content_hash = ?', (content_hash,))
            return cursor.fetchone() is not None
    
    def save_post(self, content_hash, title, link, source, category, it_score):
        """Сохраняет отправленный пост"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR IGNORE INTO sent_posts 
                    (content_hash, title, link, source, category, it_score) 
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (content_hash, title, link, source, category, it_score))
                conn.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Ошибка при сохранении поста: {e}")
            return None
    
    def get_stats(self):
        """Возвращает статистику"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Общая статистика
            cursor.execute('SELECT COUNT(*) as total FROM sent_posts')
            total = cursor.fetchone()['total']
            
            # Статистика по источникам
            cursor.execute('''
                SELECT source, COUNT(*) as count 
                FROM sent_posts 
                GROUP BY source 
                ORDER BY count DESC
            ''')
            sources = cursor.fetchall()
            
            # Статистика по категориям
            cursor.execute('''
                SELECT category, COUNT(*) as count 
                FROM sent_posts 
                WHERE category IS NOT NULL 
                GROUP BY category 
                ORDER BY count DESC
            ''')
            categories = cursor.fetchall()
            
            return {
                'total': total,
                'sources': [dict(row) for row in sources],
                'categories': [dict(row) for row in categories]
            }
    
    def cleanup_old_posts(self, days_to_keep=60):
        """Удаляет старые записи"""
        try:
            with self.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    DELETE FROM sent_posts 
                    WHERE sent_date < datetime('now', ?)
                ''', (f'-{days_to_keep} days',))
                deleted = cursor.rowcount
                conn.commit()
                return deleted
        except Exception as e:
            logger.error(f"Ошибка при очистке старых постов: {e}")
            return 0

class ITNewsBot:
    def __init__(self, token, channel_id):
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        self.db = DatabaseManager()
        
        # Настройки
        self.feeds = IT_FEEDS
        self.keywords = IT_KEYWORDS
        self.headers = {
            'User-Agent': 'IT-News-Bot/1.0 (+https://github.com/your-repo)'
        }
    
    def calculate_it_score(self, text):
        """Рассчитывает IT-релевантность текста"""
        if not text:
            return 0
        
        text_lower = text.lower()
        score = 0
        
        # Проверка ключевых слов
        for keyword in self.keywords:
            if keyword.lower() in text_lower:
                score += 1
        
        # Дополнительные критерии
        if any(tech in text_lower for tech in ['github.com', 'stackoverflow', 'gitlab']):
            score += 2
        
        if 'http' in text_lower or 'www.' in text_lower:
            score += 1
        
        # Длина текста (слишком короткие тексты менее информативны)
        if len(text) > 500:
            score += 1
        
        return score
    
    def is_it_related(self, title, description, min_score=3):
        """Проверяет, относится ли новость к IT"""
        combined_text = f"{title} {description}"
        score = self.calculate_it_score(combined_text)
        
        logger.debug(f"IT-оценка: {score} для '{title[:50]}...'")
        return score >= min_score
    
    def fetch_articles(self):
        """Получает статьи из RSS и фильтрует по IT-тематике"""
        it_articles = []
        
        for feed_config in self.feeds:
            try:
                logger.info(f"Проверяем RSS: {feed_config['name']}")
                feed = feedparser.parse(feed_config['url'])
                
                if not feed.entries:
                    logger.warning(f"Нет записей в RSS: {feed_config['name']}")
                    continue
                
                for entry in feed.entries[:15]:  # Берем последние 15 записей
                    try:
                        title = entry.get('title', 'Без названия')
                        description = self._clean_html(entry.get('summary', ''))
                        link = entry.link
                        
                        # Проверяем IT-релевантность
                        if not self.is_it_related(title, description):
                            logger.debug(f"Пропускаем не IT-новость: {title[:50]}...")
                            continue
                        
                        # Генерируем хэш контента
                        content_for_hash = f"{title}{description[:500]}"
                        content_hash = hashlib.md5(content_for_hash.encode()).hexdigest()
                        
                        # Проверяем, не отправляли ли уже
                        if self.db.is_post_sent(content_hash):
                            continue
                        
                        # Рассчитываем IT-оценку
                        it_score = self.calculate_it_score(f"{title} {description}")
                        
                        # Определяем категорию
                        category = feed_config['categories'][0] if feed_config['categories'] else 'IT'
                        
                        article = {
                            'title': title[:200],
                            'link': link,
                            'description': description[:800],
                            'source': feed_config['name'],
                            'hashtags': feed_config['hashtags'],
                            'content_hash': content_hash,
                            'it_score': it_score,
                            'category': category,
                            'published': entry.get('published', ''),
                            'full_text': f"{title}. {description[:500]}"
                        }
                        
                        it_articles.append(article)
                        logger.info(f"Найдена IT-новость [{category}]: {title[:50]}... (оценка: {it_score})")
                        
                    except Exception as e:
                        logger.error(f"Ошибка обработки статьи: {e}")
                        continue
                
                time.sleep(1)  # Пауза между RSS
                
            except Exception as e:
                logger.error(f"Ошибка RSS {feed_config['url']}: {e}")
                continue
        
        # Сортируем по IT-оценке (самые релевантные сначала)
        it_articles.sort(key=lambda x: x['it_score'], reverse=True)
        return it_articles
    
    def _clean_html(self, html_text):
        """Очищает HTML-текст"""
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
    
    def format_post(self, article):
        """Форматирует пост для Telegram"""
        title = article['title']
        if len(title) > 150:
            title = title[:147] + "..."
        
        description = article['description']
        if len(description) > 600:
            # Обрезаем до последнего полного предложения
            truncated = description[:600]
            last_sentence = max(truncated.rfind('.'), truncated.rfind('!'), truncated.rfind('?'))
            if last_sentence > 400:  # Если есть нормальное предложение
                description = truncated[:last_sentence + 1]
            else:
                description = truncated + "..."
        
        current_date = datetime.now().strftime("%d.%m.%Y")
        
        # Добавляем эмодзи в зависимости от категории
        emoji_map = {
            'Программирование': '💻',
            'Безопасность': '🔒',
            'DevOps': '⚙️',
            'Linux': '🐧',
            'Open Source': '📖',
            'Technology': '🚀'
        }
        
        emoji = emoji_map.get(article['category'], '📰')
        
        post = f"""{emoji} {title}

📊 *IT-релевантность:* {article['it_score']}/10
🏷️ *Категория:* {article['category']}
📡 *Источник:* {article['source']}

📝 *Описание:*
{description}

🔗 [Читать полностью]({article['link']})

📅 *Дата публикации:* {current_date}

{article['hashtags']}"""
        
        return post
    
    async def send_post(self, article):
        """Отправляет пост в канал"""
        try:
            post_content = self.format_post(article)
            
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=post_content,
                parse_mode='Markdown',
                disable_web_page_preview=False,
                disable_notification=False
            )
            
            # Сохраняем в БД
            post_id = self.db.save_post(
                article['content_hash'],
                article['title'],
                article['link'],
                article['source'],
                article['category'],
                article['it_score']
            )
            
            if post_id:
                logger.info(f"Отправлена IT-новость: {article['title'][:50]}...")
            else:
                logger.warning(f"Пост отправлен, но не сохранен (дубликат?): {article['title'][:50]}...")
            
            return True
            
        except TelegramError as e:
            logger.error(f"Ошибка Telegram: {e}")
            return False
        except Exception as e:
            logger.error(f"Ошибка отправки: {e}")
            return False
    
    async def run(self):
        """Основной метод запуска"""
        logger.info("=== Начало поиска IT-новостей ===")
        
        try:
            # Получаем и фильтруем статьи
            articles = self.fetch_articles()
            
            if not articles:
                logger.info("Новых IT-новостей не найдено.")
                
                # Раз в день отправляем статистику
                stats = self.db.get_stats()
                last_stats = self._get_last_stats_time()
                
                if last_stats is None or (datetime.now() - last_stats).days >= 1:
                    stats_text = self._format_stats(stats)
                    await self._send_stats(stats_text)
                    self._save_stats_time()
                
                return
            
            logger.info(f"Найдено {len(articles)} IT-новостей")
            
            # Отправляем лучшие новости (максимум 3 за раз)
            sent_count = 0
            for article in articles[:3]:
                if article['it_score'] >= 3:  # Минимальный порог
                    success = await self.send_post(article)
                    if success:
                        sent_count += 1
                        await asyncio.sleep(3)  # Пауза между отправками
            
            logger.info(f"Отправлено {sent_count} IT-новостей")
            
            # Периодическая очистка БД
            if stats['total'] % 100 == 0:
                deleted = self.db.cleanup_old_posts()
                if deleted:
                    logger.info(f"Очищено {deleted} старых записей")
            
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}")
    
    async def _send_stats(self, stats_text):
        """Отправляет статистику в канал"""
        try:
            await self.bot.send_message(
                chat_id=self.channel_id,
                text=stats_text,
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Ошибка отправки статистики: {e}")
    
    def _format_stats(self, stats):
        """Форматирует статистику"""
        stats_text = f"""📈 *Статистика IT-News Bot*

📊 Всего опубликовано: *{stats['total']}* IT-новостей

📡 *По источникам:*
"""
        for source in stats['sources'][:5]:  # Топ-5 источников
            stats_text += f"• {source['source']}: {source['count']}\n"
        
        if stats['categories']:
            stats_text += "\n🏷️ *По категориям:*\n"
            for cat in stats['categories'][:5]:
                stats_text += f"• {cat['category']}: {cat['count']}\n"
        
        stats_text += f"\n⏰ *Обновлено:* {datetime.now().strftime('%d.%m.%Y %H:%M')}"
        return stats_text
    
    def _get_last_stats_time(self):
        """Получает время последней отправки статистики"""
        try:
            with open('.last_stats', 'r') as f:
                return datetime.fromisoformat(f.read().strip())
        except:
            return None
    
    def _save_stats_time(self):
        """Сохраняет время отправки статистики"""
        try:
            with open('.last_stats', 'w') as f:
                f.write(datetime.now().isoformat())
        except Exception as e:
            logger.error(f"Ошибка сохранения времени статистики: {e}")

# --- Flask приложение для Railway ---
is_running = False

@app.route('/health')
def health():
    """Проверка работоспособности"""
    try:
        db = DatabaseManager()
        stats = db.get_stats()
        return {
            'status': 'healthy',
            'bot': 'IT-News Bot',
            'total_posts': stats['total'],
            'timestamp': datetime.now().isoformat()
        }, 200
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/run')
def run_bot():
    """Запуск бота (для Cron Job)"""
    global is_running
    
    if is_running:
        logger.info("Бот уже работает, пропускаем...")
        return {'status': 'busy', 'message': 'Бот уже запущен'}, 429
    
    is_running = True
    try:
        logger.info("=== Запуск IT-News Bot ===")
        bot = ITNewsBot(BOT_TOKEN, CHANNEL_ID)
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(bot.run())
        
        stats = bot.db.get_stats()
        result = {
            'status': 'success',
            'message': 'Проверка IT-новостей завершена',
            'stats': stats,
            'timestamp': datetime.now().isoformat()
        }
        
        logger.info(f"Завершено. Результат: {result}")
        return result, 200
        
    except Exception as e:
        logger.error(f"Ошибка запуска: {e}")
        return {'status': 'error', 'message': str(e)}, 500
    finally:
        is_running = False

@app.route('/stats')
def get_stats():
    """API для получения статистики"""
    try:
        db = DatabaseManager()
        stats = db.get_stats()
        return {
            'status': 'success',
            'data': stats,
            'timestamp': datetime.now().isoformat()
        }, 200
    except Exception as e:
        return {'status': 'error', 'message': str(e)}, 500

@app.route('/')
def index():
    """Главная страница"""
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>IT-News Telegram Bot</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 40px; }
            h1 { color: #333; }
            .container { max-width: 800px; margin: 0 auto; }
            .endpoint { background: #f5f5f5; padding: 10px; margin: 10px 0; border-radius: 5px; }
            code { background: #eee; padding: 2px 5px; border-radius: 3px; }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🤖 IT-News Telegram Bot</h1>
            <p>Бот публикует <strong>только IT-новости</strong> с фильтрацией по релевантности.</p>
            
            <h2>Доступные эндпоинты:</h2>
            <div class="endpoint">
                <strong>GET</strong> <code>/health</code> - Проверка работоспособности
            </div>
            <div class="endpoint">
                <strong>GET</strong> <code>/run</code> - Запуск бота (для Cron Job)
            </div>
            <div class="endpoint">
                <strong>GET</strong> <code>/stats</code> - Статистика бота
            </div>
            
            <h2>📡 Источники новостей:</h2>
            <ul>
                <li>Habr (Programming, Security, DevOps)</li>
                <li>OpenNet (Linux, Open Source)</li>
                <li>Hacker News</li>
                <li>Reddit (Programming, Linux)</li>
            </ul>
            
            <p><em>Бот фильтрует новости по IT-тематике и публикует только релевантные.</em></p>
        </div>
    </body>
    </html>
    """

if __name__ == '__main__':
    # Инициализация
    logger.info("Инициализация IT-News Bot...")
    
    port = int(os.environ.get("PORT", 8080))
    logger.info(f"Запуск сервера на порту {port}")
    
    # Запуск Flask
    app.run(host='0.0.0.0', port=port, debug=False)
