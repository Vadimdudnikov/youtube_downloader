import os
import asyncio
import subprocess
import sys
import re
import copy
import json
import shutil
from celery import current_task
from app.celery_app import celery_app
from app.proxy_manager import proxy_manager
from app.config import settings
from app.rapidapi_service import RapidAPIService
import torch
import tempfile

import warnings

# Глобальное отключение стандартных предупреждений
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Торч + Торчаудио
warnings.filterwarnings("ignore", message=".*torchaudio._backend.list_audio_backends.*")
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")

# WhisperX / pyannote / alignment
warnings.filterwarnings("ignore", module="pyannote")
warnings.filterwarnings("ignore", message=".*pyannote.audio.*")
warnings.filterwarnings("ignore", message=".*alignment.*")

# Lightning spam
warnings.filterwarnings("ignore", message=".*Lightning automatically upgraded.*")
warnings.filterwarnings("ignore", module="pytorch_lightning")

# SpeechBrain
warnings.filterwarnings("ignore", module="speechbrain")

# HF transformers
warnings.filterwarnings("ignore", module="transformers")

# Кэш моделей больше не нужен при использовании CLI

# Настройка для совместимости с PyTorch 2.6+
# Патчим torch.load глобально для работы с WhisperX и его зависимостями
# (pyannote, speechbrain и т.д.)
_original_torch_load = torch.load

def _patched_torch_load(*args, **kwargs):
    """Патч для torch.load с отключением weights_only для совместимости"""
    kwargs['weights_only'] = False
    return _original_torch_load(*args, **kwargs)

# Применяем патч глобально
torch.load = _patched_torch_load

# Явно разрешаем загрузку Pyannote-чекпоинтов
# Это необходимо для работы WhisperX с PyTorch 2.6+
try:
    import omegaconf
    from omegaconf.listconfig import ListConfig
    torch.serialization.add_safe_globals([omegaconf.listconfig.ListConfig])
    print("✅ omegaconf.ListConfig добавлен в безопасные глобалы PyTorch")
except ImportError as e:
    print(f"⚠️ Не удалось добавить omegaconf.ListConfig в безопасные глобалы PyTorch: {e}")
    print("   Возможны ошибки при загрузке моделей, сохраненных с omegaconf.")
except Exception as e:
    print(f"⚠️ Ошибка при добавлении omegaconf.ListConfig: {e}")



def ensure_directories():
    """Создает необходимые директории если их нет"""
    assets_dir = "assets"
    video_dir = os.path.join(assets_dir, "video")
    srt_dir = os.path.join(assets_dir, "srt")
    
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(srt_dir, exist_ok=True)
    
    return video_dir, srt_dir


def get_video_info(youtube_url: str) -> dict:
    """Получаем информацию о видео через RapidAPI"""
    try:
        # Используем RapidAPI для получения информации о видео
        rapidapi = RapidAPIService()
        video_id = rapidapi.get_video_id_from_url(youtube_url)
        
        # Получаем информацию от RapidAPI с увеличенным таймаутом и несколькими попытками
        info = rapidapi.get_info_from_rapidapi(video_id, timeout=60, max_retries=3)
        
        return {
            'title': info.get('title', 'Unknown'),
            'duration': info.get('duration', 0),
            'uploader': info.get('uploader', 'Unknown'),
            'view_count': info.get('view_count', 0),
            'upload_date': info.get('upload_date', ''),
        }
    except Exception as e:
        print(f"Ошибка получения информации о видео: {e}")
        # Возвращаем значения по умолчанию, чтобы не блокировать выполнение задачи
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






@celery_app.task(bind=True)
def download_video_task(self, youtube_url: str, audio_only: bool = False):
    """
    Задача для загрузки аудио с YouTube через RapidAPI
    """
    try:
        # RapidAPI поддерживает только аудио
        if not audio_only:
            return {
                'status': 'failed',
                'error': 'RapidAPI поддерживает только загрузку аудио. Используйте audio_only=True.',
                'exc_type': 'UnsupportedOperation'
            }
        
        # Проверяем FFmpeg для аудио конвертации
        try:
            subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            return {
                'status': 'failed',
                'error': 'FFmpeg не найден. Установите FFmpeg для конвертации аудио в MP3.',
                'exc_type': 'FFmpegNotFound'
            }
        
        # Обновляем статус задачи
        self.update_state(state='PROGRESS', meta={'status': 'Начинаем загрузку через RapidAPI...', 'progress': 0})
        
        # Убеждаемся, что папки существуют
        video_dir, srt_dir = ensure_directories()
        
        # Извлекаем YouTube ID для имени файла
        youtube_id = extract_youtube_id(youtube_url)
        print(f"YouTube ID: {youtube_id}")
        
        # Проверяем, есть ли файл уже локально
        mp3_file = f"{youtube_id}.mp3"
        mp3_path = os.path.join(video_dir, mp3_file)
        
        if os.path.exists(mp3_path):
            file_size = os.path.getsize(mp3_path)
            print(f"Файл уже существует локально: {mp3_file}")
            
            return {
                'status': 'completed',
                'progress': 100,
                'message': 'Аудио найдено локально (пропущена загрузка)',
                'file_path': mp3_path,
                'file_name': mp3_file,
                'file_size': file_size,
                'download_type': 'аудио',
                'youtube_id': youtube_id,
                'cached': True
            }
        
        # Инициализируем RapidAPI сервис
        self.update_state(state='PROGRESS', meta={'status': 'Подключаемся к RapidAPI...', 'progress': 10})
        rapidapi = RapidAPIService()
        
        # Скачиваем аудио через RapidAPI
        self.update_state(state='PROGRESS', meta={'status': 'Скачиваем аудио через RapidAPI...', 'progress': 20})
        print(f"Начинаем загрузку аудио через RapidAPI для {youtube_url}")
        
        downloaded_path = rapidapi.download_youtube_audio(
            url=youtube_url,
            output_path=mp3_path
        )
        
        if not os.path.exists(downloaded_path):
            raise Exception(f"Файл не был создан после загрузки: {downloaded_path}")
        
        file_size = os.path.getsize(downloaded_path)
        print(f"✅ Аудио успешно загружено: {mp3_file} ({file_size / 1024 / 1024:.2f} МБ)")
        
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Загрузка завершена', 'progress': 100}
        )
        
        return {
            'status': 'completed',
            'progress': 100,
            'message': 'Аудио успешно загружено через RapidAPI',
            'file_path': downloaded_path,
            'file_name': mp3_file,
            'file_size': file_size,
            'download_type': 'аудио',
            'youtube_id': youtube_id,
            'cached': False
        }
                
    except Exception as e:
        error_message = str(e)
        print(f"Ошибка загрузки через RapidAPI: {error_message}")
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка загрузки', 
                'error': error_message,
                'exc_type': type(e).__name__
            }
        )
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




def format_timestamp(seconds: float) -> str:
    """Форматирует время в формат SRT (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def generate_json_from_segments(segments: list, output_path: str) -> str:
    """Генерирует JSON файл из сегментов распознавания речи"""
    # Формируем массив объектов
    json_data = []
    
    for segment in segments:
        json_data.append({
            'start': segment['start'],
            'end': segment['end'],
            'text': segment['text'].strip()
        })
    
    # Сохраняем в файл
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=4)
    
    return json.dumps(json_data, ensure_ascii=False, indent=4)


@celery_app.task(bind=True)
def create_srt_task(self, youtube_url: str, model_size: str = "medium"):
    """
    Задача для создания SRT файла из аудио видео с YouTube
    
    Args:
        youtube_url: URL видео на YouTube
        model_size: Размер модели Whisper (tiny, base, small, medium, large)
    """
    try:
        # Убеждаемся, что папки существуют
        video_dir, srt_dir = ensure_directories()
        
        # Извлекаем YouTube ID
        youtube_id = extract_youtube_id(youtube_url)
        print(f"Создание JSON субтитров для YouTube ID: {youtube_id}")
        
        # Проверяем, существует ли уже JSON файл - если да, сразу возвращаем его
        json_file = f"{youtube_id}.json"
        json_path = os.path.join(srt_dir, json_file)
        
        if os.path.exists(json_path):
            self.update_state(
                state='PROGRESS',
                meta={'status': 'JSON файл уже существует', 'progress': 100}
            )
            
            file_size = os.path.getsize(json_path)
            
            return {
                'status': 'completed',
                'progress': 100,
                'message': 'JSON файл уже существует',
                'file_path': json_path,
                'file_name': json_file,
                'file_size': file_size,
                'youtube_id': youtube_id,
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
        
        # Определяем устройство (GPU или CPU)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        
        if device == "cuda":
            print(f"Используем GPU: {torch.cuda.get_device_name(0)}")
            print(f"CUDA версия: {torch.version.cuda}")
        else:
            print("GPU не доступен, используем CPU")
        
        # Распознаем речь с WhisperX через командную строку
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Распознаем речь...', 'progress': 40}
        )
        
        print(f"Начинаем распознавание речи из файла: {audio_path}")
        print("Начинаем транскрибацию с WhisperX через CLI...")
        
        # Создаем временную директорию для вывода WhisperX
        temp_dir = tempfile.mkdtemp()
        try:
            cmd = [
                sys.executable, "-m", "whisperx",
                audio_path,
                "--model", model_size,
                "--device", device,
                "--output_dir", temp_dir,
                "--output_format", "json"
            ]

            # Добавляем compute_type если нужно
            if compute_type:
                cmd.extend(["--compute_type", compute_type])
            
            print(f"Выполняем команду: {' '.join(cmd)}")
            
            # Запускаем WhisperX
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode != 0:
                error_msg = result.stderr or result.stdout
                print(f"Ошибка WhisperX: {error_msg}")
                raise Exception(f"Ошибка транскрибации WhisperX: {error_msg}")
            
            print(f"WhisperX завершился успешно")
            print(f"Вывод: {result.stdout[:500]}")  # Первые 500 символов
            
            # Ищем созданный JSON файл
            # WhisperX создает файл с именем как у аудио файла, но с расширением .json
            audio_basename = os.path.splitext(os.path.basename(audio_path))[0]
            whisperx_json = os.path.join(temp_dir, f"{audio_basename}.json")
            
            if not os.path.exists(whisperx_json):
                # Пробуем найти любой JSON файл в директории
                json_files = [f for f in os.listdir(temp_dir) if f.endswith('.json')]
                if json_files:
                    whisperx_json = os.path.join(temp_dir, json_files[0])
                else:
                    raise Exception(f"JSON файл не найден в {temp_dir}")
            
            # Читаем результат из JSON файла
            with open(whisperx_json, 'r', encoding='utf-8') as f:
                whisperx_result = json.load(f)
            
            print(f"JSON файл прочитан: {whisperx_json}")
            
            # Извлекаем segments из результата WhisperX
            # WhisperX может вернуть либо список segments, либо объект с ключом "segments"
            segments_list = []
            if isinstance(whisperx_result, list):
                # Если это список segments
                segments_list = whisperx_result
            elif isinstance(whisperx_result, dict):
                # Если это объект с ключом "segments"
                if 'segments' in whisperx_result:
                    segments_list = whisperx_result['segments']
                else:
                    # Если segments на верхнем уровне
                    segments_list = [whisperx_result] if 'start' in whisperx_result else []
            
            # Преобразуем segments в нужный формат
            formatted_segments = []
            for segment in segments_list:
                formatted_segments.append({
                    'start': segment.get('start', 0.0),
                    'end': segment.get('end', 0.0),
                    'text': segment.get('text', '').strip()
                })
            
            print(f"Транскрибация завершена. Найдено сегментов: {len(formatted_segments)}")
            
        finally:
            # Удаляем временную директорию
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)
                print(f"Временная директория удалена: {temp_dir}")
        
        # Генерируем JSON файл в нужном формате
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Генерируем JSON файл...', 'progress': 90}
        )
        
        print(f"Генерируем JSON файл: {json_path}")
        generate_json_from_segments(formatted_segments, json_path)
        
        file_size = os.path.getsize(json_path)
        
        self.update_state(
            state='PROGRESS',
            meta={'status': 'JSON файл создан успешно', 'progress': 100}
        )
        
        return {
            'status': 'completed',
            'progress': 100,
            'message': 'JSON файл успешно создан',
            'file_path': json_path,
            'file_name': json_file,
            'file_size': file_size,
            'youtube_id': youtube_id,
            'cached': False,
            'audio_cached': audio_exists
        }
        
    except Exception as e:
        error_message = str(e)
        print(f"Ошибка создания JSON: {error_message}")
        
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка создания JSON',
                'error': error_message,
                'exc_type': type(e).__name__
            }
        )
        
        return {
            'status': 'failed',
            'error': error_message,
            'exc_type': type(e).__name__
        }
