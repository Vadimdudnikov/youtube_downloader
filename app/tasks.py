import os
import asyncio
import subprocess
import sys
import re
import copy
import json
from celery import current_task
from app.celery_app import celery_app
from app.proxy_manager import proxy_manager
from app.config import settings
import whisper
import torch


def ensure_directories():
    """Создает необходимые директории если их нет"""
    assets_dir = "assets"
    video_dir = os.path.join(assets_dir, "video")
    srt_dir = os.path.join(assets_dir, "srt")
    
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(srt_dir, exist_ok=True)
    
    return video_dir, srt_dir


def get_video_info(youtube_url: str) -> dict:
    """Получаем информацию о видео без загрузки"""
    try:
        ytdlp_base = get_ytdlp_path()
        if isinstance(ytdlp_base, list):
            cmd = ytdlp_base.copy()
        else:
            cmd = [ytdlp_base]
        
        cmd.extend([
            '--dump-json',
            '--skip-download',
            '--quiet',
            '--no-warnings',
            youtube_url
        ])
        
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            import json
            info = json.loads(result.stdout)
            return {
                'title': info.get('title', 'Unknown'),
                'duration': info.get('duration', 0),
                'uploader': info.get('uploader', 'Unknown'),
                'view_count': info.get('view_count', 0),
                'upload_date': info.get('upload_date', ''),
            }
        else:
            raise Exception(result.stderr or "Не удалось получить информацию о видео")
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


def is_authentication_error(error_message: str) -> bool:
    """Проверяем, является ли ошибка связанной с аутентификацией"""
    auth_keywords = [
        'sign in to confirm',
        'please sign in',
        'authentication required',
        'login required',
        'cookies',
        'age verification',
        'age-restricted',
        'private video',
        'members-only',
        'premium content',
        'subscription required'
    ]
    
    error_lower = error_message.lower()
    return any(keyword in error_lower for keyword in auth_keywords)


def validate_cookies_file(cookie_file: str) -> bool:
    """Проверяет формат файла cookies (Netscape format)"""
    try:
        if not os.path.exists(cookie_file):
            return False
        
        with open(cookie_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        valid_lines = 0
        for line_num, line in enumerate(lines, 1):
            line = line.strip()
            # Пропускаем пустые строки и комментарии
            if not line or line.startswith('#'):
                continue
            
            # Формат Netscape: domain, flag, path, secure, expiration, name, value
            # Все поля разделены табуляцией
            parts = line.split('\t')
            if len(parts) >= 7:
                valid_lines += 1
            elif len(parts) > 0:
                print(f"⚠️  Строка {line_num} в cookies файле имеет неправильный формат (ожидается 7 полей, найдено {len(parts)}): {line[:50]}...")
        
        if valid_lines == 0:
            print(f"❌ Файл cookies не содержит валидных записей в формате Netscape")
            return False
        
        print(f"✅ Файл cookies содержит {valid_lines} валидных записей")
        return True
    except Exception as e:
        print(f"❌ Ошибка при проверке файла cookies: {e}")
        return False


def download_with_multiple_clients(youtube_url: str, output_path: str, audio_only: bool = False,
                                   use_cookies: bool = False, cookies_path: str = None, 
                                   proxy_url: str = None) -> dict:
    """
    Пробует загрузить видео, перебирая все доступные клиенты YouTube
    """
    # Список всех доступных клиентов YouTube
    # Если есть cookies, пробуем сначала клиенты, которые их поддерживают
    if use_cookies and cookies_path and os.path.exists(cookies_path):
        clients_order = ['mobile', 'web', 'ios', 'mweb', 'android', 'tv_embedded', 'tv']
    else:
        # Если нет cookies, пробуем все клиенты, начиная с mobile
        clients_order = ['mobile', 'web', 'android', 'ios', 'tv_embedded', 'mweb', 'tv']
    
    last_error = None
    
    for client in clients_order:
        try:
            print(f"🔄 Пробуем клиент: {client}")
            result = download_with_retry(
                youtube_url=youtube_url,
                output_path=output_path,
                audio_only=audio_only,
                use_cookies=use_cookies,
                cookies_path=cookies_path,
                proxy_url=proxy_url,
                player_client=client
            )
            
            if result['success']:
                print(f"✅ Успешно загружено с клиентом: {client}")
                return result
            else:
                last_error = result['error']
                error_preview = str(result['error'])[:100] if result['error'] else "Неизвестная ошибка"
                print(f"❌ Клиент {client} не сработал: {error_preview}...")
                continue
                
        except Exception as e:
            last_error = str(e)
            print(f"❌ Ошибка с клиентом {client}: {str(e)[:100]}...")
            continue
    
    # Если все клиенты не сработали, возвращаем последнюю ошибку
    return {
        'success': False,
        'error': f"Все клиенты не сработали. Последняя ошибка: {last_error}",
        'error_type': 'AllClientsFailed'
    }


def get_ytdlp_path():
    """Определяет путь к yt-dlp"""
    # Пробуем найти yt-dlp в стандартных местах
    possible_paths = [
        "/usr/local/bin/yt-dlp",
        "/usr/bin/yt-dlp",
        "yt-dlp"  # В PATH
    ]
    
    for path in possible_paths:
        try:
            result = subprocess.run([path, '--version'], 
                                  capture_output=True, timeout=5)
            if result.returncode == 0:
                return path
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    
    # Если не нашли, используем через python модуль
    return [sys.executable, '-m', 'yt_dlp']


def download_with_retry(youtube_url: str, output_path: str, audio_only: bool = False, 
                        use_cookies: bool = False, cookies_path: str = None, 
                        proxy_url: str = None, player_client: str = 'mobile') -> dict:
    """Загружаем видео через командную строку yt-dlp"""
    try:
        # Определяем путь к yt-dlp
        ytdlp_base = get_ytdlp_path()
        if isinstance(ytdlp_base, list):
            cmd = ytdlp_base.copy()
        else:
            cmd = [ytdlp_base]
        
        # Базовые параметры
        cmd.extend([
            '--extractor-args', f'youtube:player_client={player_client},no_sabr=1',
            '--no-warnings',
            '--quiet',
        ])
        
        # Добавляем прокси если есть
        if proxy_url:
            cmd.extend(['--proxy', proxy_url])
        
        # Добавляем cookies если есть
        if use_cookies:
            cookie_file = cookies_path or settings.cookies_file
            if not os.path.isabs(cookie_file):
                cookie_file = os.path.join(os.getcwd(), cookie_file)
            
            if os.path.exists(cookie_file):
                # Проверяем формат файла
                is_valid = validate_cookies_file(cookie_file)
                cmd.extend(['--cookies', cookie_file])
                
                if is_valid:
                    print(f"✅ Используем cookies из файла: {cookie_file}")
                else:
                    print(f"⚠️  ВНИМАНИЕ: Файл cookies имеет неправильный формат!")
            else:
                print(f"⚠️  Файл cookies не найден: {cookie_file}")
        
        # Формат и выходной файл
        if audio_only:
            cmd.extend(['-f', 'bestaudio'])
            # Для аудио нужно будет конвертировать в MP3 через FFmpeg
            cmd.extend(['-x', '--audio-format', 'mp3', '--audio-quality', '192K'])
        else:
            cmd.extend(['-f', 'best[height<=720]'])
        
        cmd.extend(['-o', output_path])
        cmd.append(youtube_url)
        
        print(f"Выполняем команду: {' '.join(cmd[:10])}...")  # Показываем только начало команды
        
        # Запускаем команду
        process = subprocess.run(cmd, text=True, capture_output=True, timeout=600)
        
        if process.returncode == 0:
            # Получаем информацию о видео для возврата
            try:
                video_info = get_video_info(youtube_url)
                video_title = video_info.get('title', 'Unknown')
                video_duration = video_info.get('duration', 0)
            except Exception as e:
                print(f"Не удалось получить метаданные: {e}")
                video_title = 'Unknown'
                video_duration = 0
            
            return {
                'success': True,
                'title': video_title,
                'duration': video_duration
            }
        else:
            error_msg = process.stderr or process.stdout or "Неизвестная ошибка"
            return {
                'success': False,
                'error': error_msg,
                'error_type': 'YtDlpError'
            }
            
    except subprocess.TimeoutExpired:
        return {
            'success': False,
            'error': 'Таймаут при загрузке',
            'error_type': 'TimeoutError'
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
            'error_type': type(e).__name__
        }


def check_and_update_ytdlp():
    """Проверяем и обновляем yt-dlp до nightly-версии"""
    try:
        # Проверяем текущую версию
        result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'], 
                              capture_output=True, text=True, timeout=30)
        
        if result.returncode == 0:
            current_version = result.stdout.strip()
            print(f"Текущая версия yt-dlp: {current_version}")
        else:
            print(f"Предупреждение: не удалось проверить версию yt-dlp: {result.stderr}")
        
        # Проверяем наличие git
        git_check = subprocess.run(['git', '--version'], 
                                 capture_output=True, text=True, timeout=5)
        has_git = git_check.returncode == 0
        
        if has_git:
            # Обновляем до nightly-версии из git репозитория (самая свежая версия)
            print("Обновляем yt-dlp до nightly-версии из git репозитория...")
            update_result = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-U', '--no-deps', 
                 'git+https://github.com/yt-dlp/yt-dlp.git'],
                capture_output=True, text=True, timeout=120
            )
            
            if update_result.returncode == 0:
                print("✅ yt-dlp обновлён до nightly-версии из git успешно")
                # Проверяем новую версию
                new_result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'], 
                                         capture_output=True, text=True, timeout=30)
                if new_result.returncode == 0:
                    new_version = new_result.stdout.strip()
                    print(f"Новая версия yt-dlp (nightly): {new_version}")
                return
            else:
                print(f"⚠️  Ошибка обновления yt-dlp из git: {update_result.stderr}")
        else:
            print("⚠️  Git не найден, используем альтернативный способ обновления")
        
        # Альтернативный способ - через pre-release (если git недоступен или не сработал)
        print("Пробуем альтернативный способ обновления (pre-release)...")
        alt_update = subprocess.run(
            [sys.executable, '-m', 'pip', 'install', '-U', '--pre', 'yt-dlp'],
            capture_output=True, text=True, timeout=120
        )
        if alt_update.returncode == 0:
            print("✅ yt-dlp обновлён до pre-release версии")
            new_result = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'], 
                                     capture_output=True, text=True, timeout=30)
            if new_result.returncode == 0:
                new_version = new_result.stdout.strip()
                print(f"Новая версия yt-dlp (pre-release): {new_version}")
        else:
            print(f"❌ Не удалось обновить yt-dlp: {alt_update.stderr}")
            
    except subprocess.TimeoutExpired:
        print("⚠️  Таймаут при обновлении yt-dlp")
    except FileNotFoundError:
        # Git не установлен
        print("⚠️  Git не найден, пробуем обновить через pre-release...")
        try:
            alt_update = subprocess.run(
                [sys.executable, '-m', 'pip', 'install', '-U', '--pre', 'yt-dlp'],
                capture_output=True, text=True, timeout=120
            )
            if alt_update.returncode == 0:
                print("✅ yt-dlp обновлён до pre-release версии")
        except Exception as e:
            print(f"❌ Ошибка при обновлении yt-dlp: {e}")
    except Exception as e:
        print(f"❌ Ошибка при обновлении yt-dlp: {e}")


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
    
    # Инициализируем current_proxy до try блока, чтобы она точно была доступна в except
    current_proxy = None
    
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
        
        # Получаем прокси объект для отслеживания
        proxy_obj = None
        proxy_url = None
        # current_proxy уже инициализирована выше как None
        
        try:
            proxy_obj = proxy_manager.get_next_proxy()
            if proxy_obj:
                print(f"[PROXY] Получен прокси: IP={proxy_obj.get('ip')}, Port={proxy_obj.get('port')}, Country={proxy_obj.get('country')}, City={proxy_obj.get('city')}")
            else:
                print(f"[PROXY] Прокси не получен: список прокси пуст или недоступен")
        except Exception as proxy_error:
            print(f"[PROXY ERROR] Ошибка при получении прокси: {proxy_error}")
            import traceback
            print(f"[PROXY ERROR] Traceback: {traceback.format_exc()}")
            proxy_obj = None
        
        # Всегда устанавливаем current_proxy, даже если proxy_obj = None
        if proxy_obj:
            # Создаем URL прокси для yt-dlp
            if proxy_obj.get('username') and proxy_obj.get('password'):
                proxy_url = f"http://{proxy_obj['username']}:{proxy_obj['password']}@{proxy_obj['ip']}:{proxy_obj['port']}"
                print(f"[PROXY] Используем прокси с авторизацией: {proxy_obj['ip']}:{proxy_obj['port']}")
            else:
                proxy_url = f"http://{proxy_obj['ip']}:{proxy_obj['port']}"
                print(f"[PROXY] Используем прокси без авторизации: {proxy_obj['ip']}:{proxy_obj['port']}")
            current_proxy = proxy_obj
        else:
            # Убеждаемся, что current_proxy явно установлена в None если прокси нет
            current_proxy = None
            print(f"[PROXY] Прокси не используется для этой загрузки")
        
        # Убеждаемся, что папки существуют
        video_dir, srt_dir = ensure_directories()
        
        # Извлекаем YouTube ID для имени файла
        youtube_id = extract_youtube_id(youtube_url)
        print(f"YouTube ID: {youtube_id}")
        
        # Проверяем, есть ли файл уже локально
        existing_file = None
        if audio_only:
            # Ищем MP3 файл
            mp3_file = f"{youtube_id}.mp3"
            mp3_path = os.path.join(video_dir, mp3_file)
            if os.path.exists(mp3_path):
                existing_file = mp3_file
        else:
            # Ищем видео файл (любое расширение)
            for file in os.listdir(video_dir):
                if file.startswith(youtube_id) and not file.endswith('.mp3'):
                    existing_file = file
                    break
        
        if existing_file:
            file_path = os.path.join(video_dir, existing_file)
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
        
        # Определяем путь для выходного файла
        if audio_only:
            output_path = f'{video_dir}/{youtube_id}.%(ext)s'  # Будет конвертирован в MP3
        else:
            output_path = f'{video_dir}/{youtube_id}.%(ext)s'
        
        # Проверяем наличие cookies файла
        cookies_path = settings.cookies_file
        # Если путь относительный, делаем его абсолютным относительно корня проекта
        if not os.path.isabs(cookies_path):
            cookies_path = os.path.join(os.getcwd(), cookies_path)
        
        cookies_exist = os.path.exists(cookies_path)
        if cookies_exist:
            print(f"✅ Найден файл cookies: {cookies_path}")
            print(f"   Размер файла: {os.path.getsize(cookies_path)} байт")
        else:
            print(f"❌ Файл cookies не найден: {cookies_path}")
            print(f"   Текущая рабочая директория: {os.getcwd()}")
            # Пробуем найти файл в корне проекта
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            alt_cookies_path = os.path.join(project_root, "cookies.txt")
            if os.path.exists(alt_cookies_path):
                print(f"   Найден альтернативный путь: {alt_cookies_path}")
                cookies_path = alt_cookies_path
                cookies_exist = True
        
        # Формируем URL прокси если доступен
        proxy_url_str = None
        if proxy_url:
            print(f"Используем прокси: {proxy_url}")
            proxy_url_str = proxy_url
        
        download_type = "аудио" if audio_only else "видео"
        
        # Если cookies файл существует, используем его сразу
        if cookies_exist:
            self.update_state(
                state='PROGRESS', 
                meta={
                    'status': f'Начинаем загрузку {download_type} с cookies (пробуем все клиенты)...',
                    'progress': 5
                }
            )
            result = download_with_multiple_clients(
                youtube_url=youtube_url,
                output_path=output_path,
                audio_only=audio_only,
                use_cookies=True,
                cookies_path=cookies_path,
                proxy_url=proxy_url_str
            )
        else:
            # Первая попытка загрузки без куки
            self.update_state(
                state='PROGRESS', 
                meta={
                    'status': f'Начинаем загрузку {download_type} (пробуем все клиенты)...',
                    'progress': 5
                }
            )
            result = download_with_multiple_clients(
                youtube_url=youtube_url,
                output_path=output_path,
                audio_only=audio_only,
                use_cookies=False,
                cookies_path=None,
                proxy_url=proxy_url_str
            )
        
        # Если первая попытка не удалась и ошибка связана с аутентификацией, пробуем с куки (если еще не пробовали)
        if not result['success'] and is_authentication_error(result['error']) and not cookies_exist:
            print(f"Обнаружена ошибка аутентификации: {result['error']}")
            print("Пробуем повторную загрузку с куки...")
            
            # Пробуем найти cookies файл еще раз
            retry_cookies_path = cookies_path
            if not retry_cookies_path or not os.path.exists(retry_cookies_path):
                project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                retry_cookies_path = os.path.join(project_root, "cookies.txt")
            
            self.update_state(
                state='PROGRESS', 
                meta={
                    'status': f'Повторная попытка загрузки {download_type} с куки (пробуем все клиенты)...',
                    'progress': 10
                }
            )
            
            result = download_with_multiple_clients(
                youtube_url=youtube_url,
                output_path=output_path,
                audio_only=audio_only,
                use_cookies=True,
                cookies_path=retry_cookies_path,
                proxy_url=proxy_url_str
            )
        
        # Если обе попытки не удались, поднимаем исключение
        if not result['success']:
            raise Exception(result['error'])
        
        video_title = result['title']
        video_duration = result['duration']
        
        self.update_state(
            state='PROGRESS', 
            meta={
                'status': f'{download_type.capitalize()} загружено: {video_title}',
                'progress': 90,
                'title': video_title,
                'duration': video_duration
            }
        )
        
        # Ищем загруженный файл по YouTube ID
        downloaded_file = None
        
        for file in os.listdir(video_dir):
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
            file_path = os.path.join(video_dir, downloaded_file)
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
            files_in_video = os.listdir(video_dir)
            print(f"Файлы в папке video: {files_in_video}")
            print(f"Ищем файл с префиксом: {youtube_id}")
            raise Exception(f"Файл не найден после загрузки. YouTube ID: {youtube_id}")
                
    except Exception as e:
        # Если ошибка связана с прокси, помечаем его как нерабочий
        # Используем проверку на наличие переменной через locals() для безопасности
        try:
            if current_proxy is not None and ("proxy" in str(e).lower() or "connection" in str(e).lower()):
                proxy_manager.mark_proxy_failed(current_proxy)
                print(f"Прокси помечен как нерабочий из-за ошибки: {e}")
        except (UnboundLocalError, NameError):
            # Переменная current_proxy не была инициализирована
            pass
        
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


def format_timestamp(seconds: float) -> str:
    """Форматирует время в формат SRT (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def generate_srt_from_segments(segments: list, output_path: str) -> str:
    """Генерирует SRT файл из сегментов распознавания речи"""
    srt_content = []
    
    for i, segment in enumerate(segments, start=1):
        start_time = format_timestamp(segment['start'])
        end_time = format_timestamp(segment['end'])
        text = segment['text'].strip()
        
        srt_content.append(f"{i}\n{start_time} --> {end_time}\n{text}\n")
    
    srt_text = "\n".join(srt_content)
    
    # Сохраняем в файл
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(srt_text)
    
    return srt_text


@celery_app.task(bind=True)
def create_srt_task(self, youtube_url: str, model_size: str = "base", language: str = None):
    """
    Задача для создания SRT файла из аудио видео с YouTube
    
    Args:
        youtube_url: URL видео на YouTube
        model_size: Размер модели Whisper (tiny, base, small, medium, large)
        language: Язык для распознавания (None = автоопределение)
    """
    try:
        # Убеждаемся, что папки существуют
        video_dir, srt_dir = ensure_directories()
        
        # Извлекаем YouTube ID
        youtube_id = extract_youtube_id(youtube_url)
        print(f"Создание SRT для YouTube ID: {youtube_id}")
        
        # Проверяем, существует ли уже SRT файл - если да, сразу возвращаем его
        srt_file = f"{youtube_id}.srt"
        srt_path = os.path.join(srt_dir, srt_file)
        
        if os.path.exists(srt_path):
            self.update_state(
                state='PROGRESS',
                meta={'status': 'SRT файл уже существует', 'progress': 100}
            )
            
            file_size = os.path.getsize(srt_path)
            video_info = get_video_info(youtube_url)
            
            return {
                'status': 'completed',
                'progress': 100,
                'message': 'SRT файл уже существует',
                'file_path': srt_path,
                'file_name': srt_file,
                'file_size': file_size,
                'youtube_id': youtube_id,
                'title': video_info['title'],
                'duration': video_info['duration'],
                'cached': True
            }
        
        # Обновляем статус
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Проверяем наличие аудио...', 'progress': 0}
        )
        
        # Проверяем наличие аудио файла
        audio_file = f"{youtube_id}.mp3"
        audio_path = os.path.join(video_dir, audio_file)
        audio_exists = os.path.exists(audio_path)
        
        # Если аудио нет, скачиваем его
        if not audio_exists:
            self.update_state(
                state='PROGRESS',
                meta={'status': 'Аудио не найдено. Загружаем аудио...', 'progress': 10}
            )
            
            print(f"Аудио файл не найден. Загружаем аудио для {youtube_url}")
            
            # Запускаем задачу загрузки аудио синхронно
            download_result = download_video_task.apply(args=[youtube_url, True])
            
            if download_result.successful():
                result = download_result.result
                if isinstance(result, dict) and result.get('status') == 'failed':
                    raise Exception(f"Ошибка загрузки аудио: {result.get('error', 'Неизвестная ошибка')}")
            else:
                raise Exception(f"Ошибка загрузки аудио: {str(download_result.info)}")
            
            # Проверяем, что файл появился
            if not os.path.exists(audio_path):
                raise Exception("Аудио файл не был создан после загрузки")
            
            print(f"Аудио успешно загружено: {audio_file}")
        else:
            print(f"Используем существующий аудио файл: {audio_file}")
        
        # Загружаем модель Whisper
        self.update_state(
            state='PROGRESS',
            meta={'status': f'Загружаем модель Whisper ({model_size})...', 'progress': 20}
        )
        
        # Определяем устройство (GPU или CPU)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        if device == "cuda":
            print(f"Используем GPU: {torch.cuda.get_device_name(0)}")
            print(f"CUDA версия: {torch.version.cuda}")
        else:
            print("GPU не доступен, используем CPU")
        
        print(f"Загружаем модель Whisper: {model_size} на устройстве: {device}")
        model = whisper.load_model(model_size, device=device)
        
        # Распознаем речь
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Распознаем речь...', 'progress': 30}
        )
        
        print(f"Начинаем распознавание речи из файла: {audio_path}")
        
        # Параметры для распознавания
        transcribe_options = {
            'verbose': False,
            'task': 'transcribe',
        }
        
        if language:
            transcribe_options['language'] = language
        
        # Распознаем речь
        result = model.transcribe(audio_path, **transcribe_options)
        
        # Генерируем SRT файл
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Генерируем SRT файл...', 'progress': 90}
        )
        
        print(f"Генерируем SRT файл: {srt_path}")
        generate_srt_from_segments(result['segments'], srt_path)
        
        file_size = os.path.getsize(srt_path)
        
        # Получаем информацию о видео
        video_info = get_video_info(youtube_url)
        
        self.update_state(
            state='PROGRESS',
            meta={'status': 'SRT файл создан успешно', 'progress': 100}
        )
        
        return {
            'status': 'completed',
            'progress': 100,
            'message': 'SRT файл успешно создан',
            'file_path': srt_path,
            'file_name': srt_file,
            'file_size': file_size,
            'youtube_id': youtube_id,
            'title': video_info['title'],
            'duration': video_info['duration'],
            'cached': False,
            'audio_cached': audio_exists
        }
        
    except Exception as e:
        error_message = str(e)
        print(f"Ошибка создания SRT: {error_message}")
        
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка создания SRT',
                'error': error_message,
                'exc_type': type(e).__name__
            }
        )
        
        return {
            'status': 'failed',
            'error': error_message,
            'exc_type': type(e).__name__
        }
