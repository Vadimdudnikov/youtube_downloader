import os
import yt_dlp
import asyncio
from celery import current_task
from app.celery_app import celery_app
from app.proxy_manager import proxy_manager
from app.config import settings

@celery_app.task(bind=True)
def download_video_task(self, youtube_url: str):
    """
    Задача для загрузки видео с YouTube с поддержкой прокси и cookies
    """
    try:
        # Обновляем статус задачи
        self.update_state(state='PROGRESS', meta={'status': 'Начинаем загрузку...', 'progress': 0})
        
        # Проверяем и обновляем прокси если нужно
        if proxy_manager.should_update_proxies():
            asyncio.run(proxy_manager.update_working_proxies())
        
        # Получаем прокси
        proxy_url = proxy_manager.get_proxy_for_ytdlp()
        current_proxy = None
        
        # Настройки для yt-dlp
        ydl_opts = {
            'outtmpl': 'assets/%(title)s.%(ext)s',
            'format': 'best[height<=720]',  # Максимум 720p
            'progress_hooks': [lambda d: self.update_progress(d)],
            'extractor_retries': 3,
            'fragment_retries': 3,
            'retries': 3,
            'socket_timeout': 30,
            'http_chunk_size': 10485760,  # 10MB chunks
            'writethumbnail': False,
            'writeinfojson': False,
            'writesubtitles': False,
            'writeautomaticsub': False,
            'ignoreerrors': False,
            'no_warnings': False,
            'extract_flat': False,
            'age_limit': None,
            'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        }
        
        # Добавляем cookies если файл существует
        if os.path.exists(settings.cookies_file):
            ydl_opts['cookiefile'] = settings.cookies_file
            print(f"Используем cookies из файла: {settings.cookies_file}")
        
        # Добавляем прокси если доступен
        if proxy_url:
            ydl_opts['proxy'] = proxy_url
            current_proxy = proxy_manager.get_next_proxy()
            print(f"Используем прокси: {proxy_url}")
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            # Получаем информацию о видео
            info = ydl.extract_info(youtube_url, download=False)
            video_title = info.get('title', 'Unknown')
            video_duration = info.get('duration', 0)
            
            self.update_state(
                state='PROGRESS', 
                meta={
                    'status': f'Загружаем: {video_title}',
                    'progress': 10,
                    'title': video_title,
                    'duration': video_duration
                }
            )
            
            # Загружаем видео
            ydl.download([youtube_url])
            
            # Ищем загруженный файл
            downloaded_file = None
            for file in os.listdir('assets'):
                if file.startswith(video_title.replace('/', '_').replace('\\', '_')):
                    downloaded_file = file
                    break
            
            if downloaded_file:
                file_path = os.path.join('assets', downloaded_file)
                file_size = os.path.getsize(file_path)
                
                return {
                    'status': 'completed',
                    'progress': 100,
                    'message': 'Видео успешно загружено',
                    'file_path': file_path,
                    'file_name': downloaded_file,
                    'file_size': file_size,
                    'title': video_title,
                    'duration': video_duration
                }
            else:
                raise Exception("Файл не найден после загрузки")
                
    except Exception as e:
        # Если ошибка связана с прокси, помечаем его как нерабочий
        if current_proxy and ("proxy" in str(e).lower() or "connection" in str(e).lower()):
            proxy_manager.mark_proxy_failed(current_proxy)
            print(f"Прокси помечен как нерабочий из-за ошибки: {e}")
        
        error_message = str(e)
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка загрузки', 
                'error': error_message,
                'exc_type': type(e).__name__
            }
        )
        # Не поднимаем исключение, чтобы избежать проблем с Celery
        return {
            'status': 'failed',
            'error': error_message,
            'exc_type': type(e).__name__
        }

def update_progress(self, d):
    """Обновление прогресса загрузки"""
    if d['status'] == 'downloading':
        if 'total_bytes' in d and d['total_bytes']:
            progress = (d['downloaded_bytes'] / d['total_bytes']) * 100
            self.update_state(
                state='PROGRESS',
                meta={
                    'status': 'Загружаем видео...',
                    'progress': int(progress),
                    'downloaded_bytes': d['downloaded_bytes'],
                    'total_bytes': d['total_bytes']
                }
            )

@celery_app.task
def update_proxies_task():
    """Задача для обновления списка прокси"""
    asyncio.run(proxy_manager.update_working_proxies())
    return f"Обновлено {len(proxy_manager.working_proxies)} рабочих прокси"
