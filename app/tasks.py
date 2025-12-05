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
from faster_whisper import WhisperModel
import torch

import warnings

# Глобальное отключение стандартных предупреждений
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Торч + Торчаудио
warnings.filterwarnings("ignore", message=".*torchaudio._backend.list_audio_backends.*")
warnings.filterwarnings("ignore", message=".*TensorFloat-32.*")

# faster-whisper warnings
warnings.filterwarnings("ignore", message=".*faster_whisper.*")

# Lightning spam
warnings.filterwarnings("ignore", message=".*Lightning automatically upgraded.*")
warnings.filterwarnings("ignore", module="pytorch_lightning")

# SpeechBrain
warnings.filterwarnings("ignore", module="speechbrain")

# HF transformers
warnings.filterwarnings("ignore", module="transformers")

# Глобальный кэш для моделей faster-whisper (чтобы не загружать каждый раз)
_whisper_models_cache = {}



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
        
        # Загружаем модель faster-whisper
        self.update_state(
            state='PROGRESS',
            meta={'status': f'Загружаем модель faster-whisper ({model_size})...', 'progress': 20}
        )
        
        # Определяем устройство (GPU или CPU)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        compute_type = "float16" if device == "cuda" else "int8"
        
        if device == "cuda":
            print(f"Используем GPU: {torch.cuda.get_device_name(0)}")
            print(f"CUDA версия: {torch.version.cuda}")
            print(f"Compute type: {compute_type} (FP16)")
            # Оптимизируем для GPU
            torch.backends.cudnn.benchmark = True
        else:
            print("GPU не доступен, используем CPU")
            print(f"Compute type: {compute_type} (int8)")
        
        # Кэшируем модель faster-whisper (загружаем только один раз)
        cache_key = f"{model_size}_{device}_{compute_type}"
        if cache_key not in _whisper_models_cache:
            print(f"Загружаем модель faster-whisper: {model_size} на устройстве: {device} (первая загрузка, будет кэширована)")
            self.update_state(
                state='PROGRESS',
                meta={'status': f'Загружаем модель faster-whisper ({model_size})...', 'progress': 20}
            )
            # Загружаем модель faster-whisper
            model = WhisperModel(
                model_size,
                device=device,
                compute_type=compute_type
            )
            
            _whisper_models_cache[cache_key] = model
            print(f"✅ Модель faster-whisper загружена и закэширована")
        else:
            print(f"✅ Используем закэшированную модель faster-whisper: {model_size} на {device}")
            model = _whisper_models_cache[cache_key]
        
        # Распознаем речь
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Распознаем речь...', 'progress': 40}
        )
        
        print(f"Начинаем распознавание речи из файла: {audio_path}")
        print("Начинаем транскрибацию с faster-whisper...")
        
        # Транскрибируем аудио
        segments_generator, info = model.transcribe(
            audio_path,
            beam_size=5,
            language=None,  # Автоопределение языка
            task="transcribe",
            vad_filter=True,  # Используем встроенный VAD фильтр
            vad_parameters=dict(min_silence_duration_ms=500)  # Минимальная длительность тишины
        )
        
        print(f"Транскрибация завершена. Язык: {info.language}")
        
        # Извлекаем segments и разбиваем на предложения
        segments_list = []
        for segment in segments_generator:
            text = segment.text.strip()
            if not text:
                continue
            
            # Разбиваем текст на предложения по знакам препинания
            import re
            sentences = re.split(r'([.!?]+)', text)
            
            # Объединяем знаки препинания с предыдущим предложением
            sentence_parts = []
            for i in range(0, len(sentences) - 1, 2):
                if i + 1 < len(sentences):
                    sentence = (sentences[i] + sentences[i + 1]).strip()
                    if sentence:
                        sentence_parts.append(sentence)
                else:
                    if sentences[i].strip():
                        sentence_parts.append(sentences[i].strip())
            
            # Если не удалось разбить на предложения, используем весь текст
            if not sentence_parts:
                sentence_parts = [text] if text else []
            
            # Распределяем время между предложениями пропорционально их длине
            if len(sentence_parts) > 1:
                total_chars = sum(len(s) for s in sentence_parts)
                current_time = segment.start
                duration = segment.end - segment.start
                
                for i, sentence in enumerate(sentence_parts):
                    if i == len(sentence_parts) - 1:
                        # Последнее предложение заканчивается в segment.end
                        seg_end = segment.end
                    else:
                        # Пропорционально длине текста
                        seg_duration = (len(sentence) / total_chars) * duration
                        seg_end = current_time + seg_duration
                    
                    segments_list.append({
                        'start': current_time,
                        'end': seg_end,
                        'text': sentence
                    })
                    
                    current_time = seg_end
            else:
                # Если одно предложение или не удалось разбить
                segments_list.append({
                    'start': segment.start,
                    'end': segment.end,
                    'text': text
                })
        
        print(f"Транскрибация завершена. Найдено сегментов: {len(segments_list)}")
        
        # Генерируем JSON файл
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Генерируем JSON файл...', 'progress': 90}
        )
        
        print(f"Генерируем JSON файл: {json_path}")
        generate_json_from_segments(segments_list, json_path)
        
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
