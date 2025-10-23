import os
import yt_dlp
import asyncio
import subprocess
import sys
import re
from celery import current_task
from app.celery_app import celery_app
from app.proxy_manager import proxy_manager
from app.config import settings


def get_video_info(youtube_url: str) -> dict:
    """Получаем информацию о видео без загрузки"""
    try:
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
        }
        
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            return {
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'uploader': info.get('uploader', 'Unknown'),
                'view_count': info.get('view_count', 0),
                'upload_date': info.get('upload_date', ''),
            }
    except Exception as e:
        print(f"Ошибка получения информации о видео: {e}")
        return {
            'title': 'Unknown',
            'duration': 0,
            'uploader': 'Unknown',
            'view_count': 0,
            'upload_date': '',
        }


def extract_youtube_id(url: str) -> str:
    """Извлекаем YouTube ID из URL"""
    patterns = [
        r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})',
        r'youtube\.com/v/([a-zA-Z0-9_-]{11})',
        r'youtube\.com/shorts/([a-zA-Z0-9_-]{11})'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    
    # Если не удалось извлечь ID, используем хеш от URL
    import hashlib
    return hashlib.md5(url.encode()).hexdigest()[:11]


def check_and_update_ytdlp():
    """Проверяем и обновляем yt-dlp если необходимо"""
    try:
        # Проверяем текущую версию
        result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'], 
                              capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            current_version = result.stdout.strip()
            print(f"Текущая версия yt-dlp: {current_version}")
            
            # Пытаемся обновить
            print("Проверяем обновления yt-dlp...")
            update_result = subprocess.run([sys.executable, '-m', 'yt_dlp', '-U'], 
                                         capture_output=True, text=True, timeout=60)
            
            if update_result.returncode == 0:
                print("yt-dlp обновлён успешно")
                # Проверяем новую версию
                new_result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'], 
                                         capture_output=True, text=True, timeout=30)
                if new_result.returncode == 0:
                    new_version = new_result.stdout.strip()
                    print(f"Новая версия yt-dlp: {new_version}")
            else:
                print(f"Ошибка обновления yt-dlp: {update_result.stderr}")
                
        else:
            print(f"Ошибка проверки версии yt-dlp: {result.stderr}")
            
    except subprocess.TimeoutExpired:
        print("Таймаут при проверке/обновлении yt-dlp")
    except Exception as e:
        print(f"Ошибка при проверке yt-dlp: {e}")


@celery_app.task(bind=True)
def download_video_task(self, youtube_url: str, audio_only: bool = False):
    """
    Задача для загрузки видео или аудио с YouTube с поддержкой прокси и cookies
    """
    
    def update_progress(d):
        """Обновление прогресса загрузки"""
        if d['status'] == 'downloading':
            if 'total_bytes' in d and d['total_bytes']:
                progress = (d['downloaded_bytes'] / d['total_bytes']) * 100
                download_type = "аудио" if audio_only else "видео"
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'status': f'Загружаем {download_type}...',
                        'progress': int(progress),
                        'downloaded_bytes': d['downloaded_bytes'],
                        'total_bytes': d['total_bytes']
                    }
                )
    
    try:
        # Проверяем и обновляем yt-dlp
        check_and_update_ytdlp()
        
        # Проверяем FFmpeg для аудио конвертации
        if audio_only:
            try:
                subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
            except (subprocess.CalledProcessError, FileNotFoundError):
                return {
                    'status': 'failed',
                    'error': 'FFmpeg не найден. Установите FFmpeg для конвертации аудио в MP3.',
                    'exc_type': 'FFmpegNotFound'
                }
        
        # Обновляем статус задачи
        self.update_state(state='PROGRESS', meta={'status': 'Начинаем загрузку...', 'progress': 0})
        
        # Проверяем и обновляем прокси если нужно
        if proxy_manager.should_update_proxies():
            asyncio.run(proxy_manager.update_working_proxies())
        
        # Получаем прокси
        proxy_url = proxy_manager.get_proxy_for_ytdlp()
        current_proxy = None
        
        # Извлекаем YouTube ID для имени файла
        youtube_id = extract_youtube_id(youtube_url)
        print(f"YouTube ID: {youtube_id}")
        
        # Проверяем, есть ли файл уже локально
        existing_file = None
        if audio_only:
            # Ищем MP3 файл
            mp3_file = f"{youtube_id}.mp3"
            mp3_path = os.path.join('assets', mp3_file)
            if os.path.exists(mp3_path):
                existing_file = mp3_file
        else:
            # Ищем видео файл (любое расширение)
            for file in os.listdir('assets'):
                if file.startswith(youtube_id) and not file.endswith('.mp3'):
                    existing_file = file
                    break
        
        if existing_file:
            file_path = os.path.join('assets', existing_file)
            file_size = os.path.getsize(file_path)
            download_type = "аудио" if audio_only else "видео"
            
            print(f"Файл уже существует локально: {existing_file}")
            
            # Получаем информацию о видео для кэшированного файла
            video_info = get_video_info(youtube_url)
            
            return {
                'status': 'completed',
                'progress': 100,
                'message': f'{download_type.capitalize()} найдено локально (пропущена загрузка)',
                'file_path': file_path,
                'file_name': existing_file,
                'file_size': file_size,
                'title': video_info['title'],
                'duration': video_info['duration'],
                'uploader': video_info['uploader'],
                'view_count': video_info['view_count'],
                'upload_date': video_info['upload_date'],
                'download_type': download_type,
                'youtube_id': youtube_id,
                'cached': True
            }
        
        # Настройки для yt-dlp
        if audio_only:
            # Настройки для загрузки только аудио в MP3
            ydl_opts = {
                'outtmpl': f'assets/{youtube_id}.%(ext)s',  # Используем YouTube ID
                'format': 'bestaudio/best',  # Лучшее аудио качество
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'progress_hooks': [update_progress],
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
                # Дополнительные настройки для обхода ограничений YouTube
                'extractor_args': {
                    'youtube': {
                        'skip': ['dash', 'hls'],
                        'player_client': ['android', 'web']
                    }
                }
            }
        else:
            # Настройки для загрузки видео
            ydl_opts = {
                'outtmpl': f'assets/{youtube_id}.%(ext)s',  # Используем YouTube ID
                'format': 'best[height<=720]',  # Максимум 720p
                'progress_hooks': [update_progress],
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
                # Дополнительные настройки для обхода ограничений YouTube
                'extractor_args': {
                    'youtube': {
                        'skip': ['dash', 'hls'],
                        'player_client': ['android', 'web']
                    }
                }
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
            
            download_type = "аудио" if audio_only else "видео"
            self.update_state(
                state='PROGRESS', 
                meta={
                    'status': f'Загружаем {download_type}: {video_title}',
                    'progress': 10,
                    'title': video_title,
                    'duration': video_duration
                }
            )
            
            # Загружаем видео или аудио
            ydl.download([youtube_url])
            
            # Ищем загруженный файл по YouTube ID
            downloaded_file = None
            
            for file in os.listdir('assets'):
                # Для аудио ищем .mp3 файлы с YouTube ID, для видео - любые файлы с YouTube ID
                if audio_only:
                    if file.startswith(youtube_id) and file.endswith('.mp3'):
                        downloaded_file = file
                        break
                else:
                    if file.startswith(youtube_id):
                        downloaded_file = file
                        break
            
            if downloaded_file:
                file_path = os.path.join('assets', downloaded_file)
                file_size = os.path.getsize(file_path)
                
                return {
                    'status': 'completed',
                    'progress': 100,
                    'message': f'{download_type.capitalize()} успешно загружено',
                    'file_path': file_path,
                    'file_name': downloaded_file,
                    'file_size': file_size,
                    'title': video_title,
                    'duration': video_duration,
                    'download_type': download_type,
                    'youtube_id': youtube_id,
                    'cached': False
                }
            else:
                # Если файл не найден, выводим список файлов для отладки
                files_in_assets = os.listdir('assets')
                print(f"Файлы в папке assets: {files_in_assets}")
                print(f"Ищем файл с префиксом: {youtube_id}")
                raise Exception(f"Файл не найден после загрузки. YouTube ID: {youtube_id}")
                
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

@celery_app.task
def update_proxies_task():
    """Задача для обновления списка прокси"""
    asyncio.run(proxy_manager.update_working_proxies())
    return f"Обновлено {len(proxy_manager.working_proxies)} рабочих прокси"


@celery_app.task
def update_ytdlp_task():
    """Задача для обновления yt-dlp"""
    try:
        check_and_update_ytdlp()
        return "yt-dlp обновлён успешно"
    except Exception as e:
        return f"Ошибка обновления yt-dlp: {str(e)}"
