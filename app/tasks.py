import os
import subprocess
import re
import json
from app.celery_app import celery_app
from app.config import settings
from app.rapidapi_service import RapidAPIService
from app.whisperx_service import WhisperXService

import warnings

# Глобальное отключение стандартных предупреждений
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)


def ensure_directories():
    """Создает необходимые директории если их нет"""
    assets_dir = "assets"
    video_dir = os.path.join(assets_dir, "video")
    srt_dir = os.path.join(assets_dir, "srt")
    
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(srt_dir, exist_ok=True)
    
    return video_dir, srt_dir


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


@celery_app.task(bind=True)
def transcribe_audio_task(self, audio_path: str, task_id: str = None, model_size: str = None):
    """
    Задача для транскрипции аудио с использованием WhisperXService
    
    Args:
        audio_path: Путь к аудио файлу для транскрипции
        task_id: Идентификатор задачи (опционально)
        model_size: Размер модели WhisperX (tiny, base, small, medium, large). По умолчанию из config
        
    Returns:
        dict: Результат транскрипции с сегментами
    """
    try:
        print(f"🎤 Начинаем транскрипцию аудио: {audio_path}")
        if task_id:
            print(f"  Task ID: {task_id}")
        
        # Обновляем статус задачи
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Инициализация транскрипции...', 'progress': 0}
        )
        
        # Проверяем существование файла
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Аудио файл не найден: {audio_path}")
        
        # Проверяем, есть ли mp3 файл, если есть - используем его, иначе wav
        audio_mp3_path = audio_path.replace('.wav', '.mp3') if audio_path.endswith('.wav') else audio_path
        audio_wav_path = audio_path.replace('.mp3', '.wav') if audio_path.endswith('.mp3') else audio_path
        
        if os.path.exists(audio_mp3_path) and audio_mp3_path != audio_path:
            audio_path = audio_mp3_path
            print(f"📁 Используем MP3 файл для транскрипции: {audio_path}")
        elif os.path.exists(audio_wav_path) and audio_wav_path != audio_path:
            audio_path = audio_wav_path
            print(f"📁 Используем WAV файл для транскрипции: {audio_path}")
        
        # Обновляем статус
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Создаем сервис транскрипции...', 'progress': 10}
        )
        
        # Создаём сервис транскрипции и выполняем транскрипцию
        transcription_service = WhisperXService(model_size=model_size)
        
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Выполняем транскрипцию...', 'progress': 20}
        )
        
        transcription_result = transcription_service.transcribe_audio(audio_path)
        
        # Проверяем результат транскрипции
        if isinstance(transcription_result, dict):
            segments = transcription_result.get('segments', [])
        else:
            segments = transcription_result if isinstance(transcription_result, list) else []
        
        # Если сегментов нет - это ошибка
        if not segments or len(segments) == 0:
            error_msg = f"WhisperX не смог распознать речь в аудио файле (0 сегментов). Возможные причины: тихий звук, фоновый шум, поврежденный файл"
            print(f"❌ {error_msg}")
            raise Exception(error_msg)
        
        # Если указан task_id, сохраняем результат в JSON файл
        if task_id:
            # Убеждаемся, что базовая директория существует
            os.makedirs(settings.tmp_dir, exist_ok=True)
            task_dir = os.path.join(settings.tmp_dir, task_id)
            os.makedirs(task_dir, exist_ok=True)
            
            original_json_path = os.path.join(task_dir, 'original.json')
            with open(original_json_path, 'w', encoding='utf-8') as f:
                json.dump(transcription_result, f, ensure_ascii=False, indent=4)
            
            print(f"✅ Результат сохранен в: {original_json_path}")
        
        # Обновляем статус
        self.update_state(
            state='PROGRESS',
            meta={'status': f'Транскрипция завершена: {len(segments)} сегментов', 'progress': 100}
        )
        
        # Создаём словарь с результатом
        message = f'Транскрипция завершена: {len(segments)} сегментов'
        
        result = {
            'status': 'success',
            'segments': segments,
            'message': message,
            'segments_count': len(segments)
        }
        
        print(f"✅ Транскрипция завершена: {len(segments)} сегментов")
        return result
        
    except Exception as e:
        error_message = str(e)
        print(f"❌ Ошибка транскрипции: {error_message}")
        
        # Обновляем статус задачи с ошибкой
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка транскрипции',
                'error': error_message,
                'exc_type': type(e).__name__
            }
        )
        
        return {
            'status': 'failed',
            'error': error_message,
            'exc_type': type(e).__name__
        }
