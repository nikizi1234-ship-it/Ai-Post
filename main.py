import os
import logging
import feedparser
import requests
from datetime import datetime
from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError
from bs4 import BeautifulSoup
import json
import asyncio
import aiohttp
from typing import Optional, List, Tuple
import traceback
import sys

# Настройка логирования для Railway
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Конфигурация из переменных окружения (Railway)
BOT_TOKEN = os.environ.get('BOT_TOKEN')
CHANNEL_ID = os.environ.get('CHANNEL_ID')

# Проверка обязательных переменных
if not BOT_TOKEN:
    logger.error("BOT_TOKEN не установлен!")
    sys.exit(1)
if not CHANNEL_ID:
    logger.error("CHANNEL_ID не установлен!")
    sys.exit(1)

class ITNewsBot:
    def __init__(self):
        """Инициализация бота"""
        self.bot = Bot(token=BOT_TOKEN)
        self.channel_id = CHANNEL_ID
        
        # Для Railway используем временную директорию
        self.data_dir = os.path.join(os.getcwd(), 'data')
        os.makedirs(self.data_dir, exist_ok=True)
        self.sent_articles_file = os.path.join(self.data_dir, 'sent_articles.json')
        
        self.sent_articles = self.load_sent_articles()
        
        # RSS-ленты IT-новостей с разными источниками
        self.rss_feeds = [
            "https://habr.com/ru/rss/hubs/all/",
            "https://www.opennet.ru/opennews/opennews_all.rss",
            "https://news.ycombinator.com/rss",
            "https://www.reddit.com/r/programming/.rss",
            "https://dev.to/feed",
            "https://techcrunch.com/feed/",
            "https://feeds.feedburner.com/TheHackersNews",
        ]
        
        # User-Agent для запросов
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        
        # Сессия для асинхронных запросов
        self.session = None
        
    async def init_session(self):
        """Инициализация HTTP-сессии"""
        if not self.session:
            self.session = aiohttp.ClientSession(headers=self.headers)
    
    def load_sent_articles(self) -> set:
        """Загрузка списка уже отправленных статей"""
        try:
            if os.path.exists(self.sent_articles_file):
                with open(self.sent_articles_file, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        data = json.loads(content)
                        return set(data.get('articles', []))
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка декодирования JSON: {e}")
            # Создаем резервную копию поврежденного файла
            if os.path.exists(self.sent_articles_file):
                backup_file = self.sent_articles_file + '.bak'
                os.rename(self.sent_articles_file, backup_file)
                logger.info(f"Создана резервная копия файла: {backup_file}")
        except Exception as e:
            logger.error(f"Ошибка загрузки отправленных статей: {e}")
            logger.error(traceback.format_exc())
        
        return set()
    
    def save_sent_article(self, article_id: str):
        """Сохранение ID отправленной статьи"""
        try:
            self.sent_articles.add(article_id)
            
            # Ограничиваем размер истории (последние 1000 статей)
            if len(self.sent_articles) > 1000:
                self.sent_articles = set(list(self.sent_articles)[-1000:])
            
            data = {'articles': list(self.sent_articles)}
            with open(self.sent_articles_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            logger.debug(f"Сохранена статья ID: {article_id}")
            
        except Exception as e:
            logger.error(f"Ошибка сохранения статьи: {e}")
            logger.error(traceback.format_exc())
    
    async def fetch_feed(self, rss_url: str) -> Tuple[List, str]:
        """Асинхронное получение RSS-ленты"""
        try:
            await self.init_session()
            async with self.session.get(rss_url, timeout=10) as response:
                if response.status == 200:
                    content = await response.text()
                    feed = feedparser.parse(content)
                    return feed.entries[:3], feed.feed.get('title', 'Unknown Source')
                else:
                    logger.warning(f"Ошибка HTTP {response.status} для {rss_url}")
        except Exception as e:
            logger.error(f"Ошибка получения RSS {rss_url}: {e}")
        
        return [], 'Unknown Source'
    
    async def extract_image_url(self, entry) -> str:
        """Извлечение URL изображения из записи"""
        image_url = None
        
        # Пытаемся найти изображение в разных полях
        try:
            # В медиа-контенте
            if hasattr(entry, 'media_content') and entry.media_content:
                for media in entry.media_content:
                    if media.get('type', '').startswith('image/'):
                        image_url = media.get('url')
                        if image_url:
                            break
            
            # В ссылках
            if not image_url and hasattr(entry, 'links'):
                for link in entry.links:
                    if getattr(link, 'type', '').startswith('image/'):
                        image_url = getattr(link, 'href', None)
                        if image_url:
                            break
            
            # В контенте (HTML)
            if not image_url and hasattr(entry, 'content'):
                for content in entry.content:
                    soup = BeautifulSoup(content.value, 'html.parser')
                    img_tags = soup.find_all('img')
                    for img in img_tags:
                        src = img.get('src')
                        if src and (src.startswith('http://') or src.startswith('https://')):
                            image_url = src
                            break
                    if image_url:
                        break
            
            # В описании
            if not image_url and hasattr(entry, 'description'):
                soup = BeautifulSoup(entry.description, 'html.parser')
                img_tags = soup.find_all('img')
                for img in img_tags:
                    src = img.get('src')
                    if src and (src.startswith('http://') or src.startswith('https://')):
                        image_url = src
                        break
        
        except Exception as e:
            logger.debug(f"Ошибка извлечения изображения: {e}")
        
        # Заглушка, если изображение не найдено
        if not image_url:
            # Используем разные IT-изображения для разнообразия
            image_urls = [
                "https://images.unsplash.com/photo-1555949963-aa79dcee981c?w=800&h=400&fit=crop",
                "https://images.unsplash.com/photo-1518709268805-4e9042af2176?w=800&h=400&fit=crop",
                "https://images.unsplash.com/photo-1457305237443-44c3d5a30b89?w=800&h=400&fit=crop",
            ]
            import random
            image_url = random.choice(image_urls)
        
        return image_url
    
    async def fetch_news_from_rss(self) -> List[Tuple[dict, str]]:
        """Получение новостей из RSS-лент"""
        all_articles = []
        
        # Запускаем все RSS-запросы параллельно
        tasks = [self.fetch_feed(rss_url) for rss_url in self.rss_feeds]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for idx, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(f"Ошибка в RSS задаче {self.rss_feeds[idx]}: {result}")
                continue
            
            entries, source = result
            
            for entry in entries:
                try:
                    # Создаем уникальный ID для статьи
                    article_id = f"{self.rss_feeds[idx]}_{entry.get('id', entry.link)}"
                    
                    if article_id in self.sent_articles:
                        continue
                    
                    # Извлекаем изображение
                    image_url = await self.extract_image_url(entry)
                    
                    # Формируем данные статьи
                    article = {
                        'title': entry.title[:200] + "..." if len(entry.title) > 200 else entry.title,
                        'description': entry.get('summary', entry.get('description', ''))[:300] + "..."
                                    if len(entry.get('summary', '')) > 300 
                                    else entry.get('summary', entry.get('description', '')),
                        'url': entry.link,
                        'image_url': image_url,
                        'published': entry.get('published', entry.get('updated', datetime.now().isoformat())),
                        'source': source,
                        'content': entry.get('content', [{}])[0].get('value', '')[:500] + "..."
                                if entry.get('content') 
                                else entry.get('summary', entry.get('description', ''))[:500] + "..."
                    }
                    
                    all_articles.append((article, article_id))
                    
                except Exception as e:
                    logger.error(f"Ошибка обработки записи: {e}")
                    continue
        
        # Сортируем статьи по дате (новые сначала)
        try:
            all_articles.sort(key=lambda x: x[0].get('published', ''), reverse=True)
        except:
            pass
        
        return all_articles
    
    def generate_hashtags(self, title: str, source: str) -> str:
        """Генерация хештегов на основе заголовка и источника"""
        hashtags = []
        
        # Ключевые слова для IT-тематики
        it_keywords = {
            'python': ['Python', 'Программирование'],
            'javascript': ['JavaScript', 'JS', 'Web'],
            'java': ['Java'],
            'c#': ['CSharp', 'DotNet'],
            'php': ['PHP'],
            'ruby': ['Ruby'],
            'go': ['Go', 'Golang'],
            'rust': ['Rust'],
            'ai': ['AI', 'ИскусственныйИнтеллект', 'МашинноеОбучение'],
            'machine learning': ['MachineLearning', 'ML'],
            'deep learning': ['DeepLearning', 'НейронныеСети'],
            'cybersecurity': ['Кибербезопасность', 'Security'],
            'blockchain': ['Blockchain', 'Криптовалюты'],
            'web': ['WebDevelopment', 'ВебРазработка'],
            'mobile': ['Mobile', 'МобильнаяРазработка'],
            'devops': ['DevOps'],
            'cloud': ['Cloud', 'ОблачныеТехнологии'],
            'database': ['БазыДанных', 'SQL', 'NoSQL'],
            'linux': ['Linux'],
            'windows': ['Windows'],
            'ios': ['iOS'],
            'android': ['Android'],
            'startup': ['Стартапы', 'Startup'],
        }
        
        title_lower = title.lower()
        
        # Добавляем теги на основе ключевых слов
        for keyword, tags in it_keywords.items():
            if keyword in title_lower:
                hashtags.extend(tags)
        
        # Добавляем теги на основе источника
        source_tags = {
            'habr': ['Habr', 'Хабр'],
            'hacker news': ['HackerNews'],
            'reddit': ['Reddit'],
            'dev.to': ['DevCommunity'],
            'techcrunch': ['TechCrunch'],
            'opennet': ['OpenNet'],
        }
        
        for source_key, tags in source_tags.items():
            if source_key in source.lower():
                hashtags.extend(tags)
        
        # Добавляем общие теги
        general_tags = ['IT', 'Технологии', 'ITНовости', 'НовостиТехнологий', 'Прогресс']
        hashtags.extend(general_tags)
        
        # Уникальные теги и ограничение по количеству
        hashtags = list(dict.fromkeys(hashtags))[:8]
        
        return ' '.join(['#' + tag.replace(' ', '').replace('-', '') for tag in hashtags])
    
    def format_published_date(self, date_str: str) -> str:
        """Форматирование даты публикации"""
        try:
            # Пробуем разные форматы дат
            date_formats = [
                '%a, %d %b %Y %H:%M:%S %Z',
                '%a, %d %b %Y %H:%M:%S %z',
                '%Y-%m-%dT%H:%M:%SZ',
                '%Y-%m-%d %H:%M:%S',
                '%d.%m.%Y %H:%M',
            ]
            
            for fmt in date_formats:
                try:
                    dt = datetime.strptime(date_str, fmt)
                    return dt.strftime('%d.%m.%Y %H:%M')
                except:
                    continue
            
            # Если ни один формат не подошел, возвращаем исходную строку
            return date_str[:16]
        except:
            return "Дата не указана"
    
    def create_post_content(self, article: dict) -> str:
        """Создание контента для поста в указанном формате"""
        try:
            # Форматируем дату
            date_str = self.format_published_date(article.get('published', ''))
            
            # Создаем рассуждения
            reasoning = article.get('content', article.get('description', ''))
            if len(reasoning) > 800:
                reasoning = reasoning[:800] + "..."
            
            # Создаем пост
            post = f"""
📰 *{article['title']}*

*Источник:* {article['source']}
*Дата:* {date_str}

💭 *Рассуждения:*
{reasoning}

📖 [Читать полностью]({article['url']})

{self.generate_hashtags(article['title'], article['source'])}
"""
            
            return post.strip()
            
        except Exception as e:
            logger.error(f"Ошибка создания поста: {e}")
            return "Произошла ошибка при формировании новости"
    
    async def download_image(self, image_url: str) -> Optional[bytes]:
        """Скачивание изображения"""
        try:
            await self.init_session()
            async with self.session.get(image_url, timeout=15) as response:
                if response.status == 200:
                    return await response.read()
        except Exception as e:
            logger.error(f"Ошибка загрузки изображения {image_url}: {e}")
        return None
    
    async def send_news_to_channel(self):
        """Отправка новости в канал"""
        try:
            logger.info("Начинаю поиск новых статей...")
            articles = await self.fetch_news_from_rss()
            
            if not articles:
                logger.warning("Новых статей не найдено")
                return
            
            # Выбираем самую свежую статью
            article, article_id = articles[0]
            
            logger.info(f"Найдена статья: {article['title'][:50]}...")
            
            # Создаем контент поста
            post_content = self.create_post_content(article)
            
            # Скачиваем изображение
            image_data = await self.download_image(article['image_url'])
            
            if image_data:
                # Сохраняем временно изображение
                temp_image = os.path.join(self.data_dir, 'temp_image.jpg')
                with open(temp_image, 'wb') as f:
                    f.write(image_data)
                
                # Отправляем пост с изображением
                with open(temp_image, 'rb') as photo:
                    await self.bot.send_photo(
                        chat_id=self.channel_id,
                        photo=photo,
                        caption=post_content,
                        parse_mode='Markdown'
                    )
                
                # Удаляем временный файл
                os.remove(temp_image)
            else:
                # Отправляем без изображения
                await self.bot.send_message(
                    chat_id=self.channel_id,
                    text=post_content,
                    parse_mode='Markdown',
                    disable_web_page_preview=False
                )
            
            # Сохраняем ID отправленной статьи
            self.save_sent_article(article_id)
            logger.info(f"Статья отправлена успешно: {article['title'][:50]}...")
            
            return True
            
        except Exception as e:
            logger.error(f"Ошибка отправки новости: {e}")
            logger.error(traceback.format_exc())
            return False
    
    async def cleanup(self):
        """Очистка ресурсов"""
        if self.session:
            await self.session.close()
    
    async def run(self):
        """Основной цикл бота"""
        logger.info("Бот запущен")
        
        try:
            while True:
                await self.send_news_to_channel()
                
                # Ждем 6 часов (21600 секунд)
                logger.info("Ожидание 6 часов до следующей отправки...")
                await asyncio.sleep(21600)
                
        except asyncio.CancelledError:
            logger.info("Бот остановлен")
        except Exception as e:
            logger.error(f"Критическая ошибка: {e}")
            logger.error(traceback.format_exc())
        finally:
            await self.cleanup()

# Запуск бота
async def main():
    bot = ITNewsBot()
    try:
        await bot.run()
    except KeyboardInterrupt:
        logger.info("Остановка бота по запросу пользователя")
        await bot.cleanup()

if __name__ == '__main__':
    asyncio.run(main())
