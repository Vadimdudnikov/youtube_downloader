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
        
        # Сохраняем исходный путь к MP3 файлу для возможного удаления при ошибке
        original_mp3_path = None
        if audio_path.endswith('.mp3'):
            original_mp3_path = audio_path
        elif audio_path.endswith('.wav'):
            # Пытаемся найти соответствующий MP3 файл
            original_mp3_path = audio_path.replace('.wav', '.mp3')
            if not os.path.exists(original_mp3_path):
                original_mp3_path = None
        
        # Проверяем, есть ли mp3 файл, если есть - используем его, иначе wav
        audio_mp3_path = audio_path.replace('.wav', '.mp3') if audio_path.endswith('.wav') else audio_path
        audio_wav_path = audio_path.replace('.mp3', '.wav') if audio_path.endswith('.mp3') else audio_path
        
        if os.path.exists(audio_mp3_path) and audio_mp3_path != audio_path:
            audio_path = audio_mp3_path
            print(f"📁 Используем MP3 файл для транскрипции: {audio_path}")
            # Обновляем original_mp3_path, если нашли MP3
            if not original_mp3_path:
                original_mp3_path = audio_mp3_path
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
            # Сохраняем в папку srt (для совместимости с API)
            video_dir, srt_dir = ensure_directories()
            json_file = f"{task_id}.json"
            json_path = os.path.join(srt_dir, json_file)
            
            # Формируем JSON данные из сегментов
            json_data = []
            for segment in segments:
                json_data.append({
                    'start': segment.get('start', 0),
                    'end': segment.get('end', 0),
                    'text': segment.get('text', '').strip()
                })
            
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, ensure_ascii=False, indent=4)
            
            print(f"✅ Результат сохранен в: {json_path}")
        
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
            'segments_count': len(segments),
            'youtube_id': task_id if task_id else None
        }
        
        print(f"✅ Транскрипция завершена: {len(segments)} сегментов")
        return result
        
    except Exception as e:
        error_message = str(e)
        print(f"❌ Ошибка транскрипции: {error_message}")
        
        # Если ошибка связана с 0 сегментами, удаляем исходный MP3 файл
        if "0 сегментов" in error_message or "не смог распознать речь" in error_message:
            # Пытаемся найти и удалить исходный MP3 файл
            mp3_to_delete = None
            
            try:
                # Проверяем, был ли сохранен original_mp3_path
                if 'original_mp3_path' in locals() and original_mp3_path and os.path.exists(original_mp3_path):
                    mp3_to_delete = original_mp3_path
                elif 'audio_path' in locals():
                    # Пытаемся определить MP3 файл из audio_path
                    if audio_path.endswith('.mp3') and os.path.exists(audio_path):
                        mp3_to_delete = audio_path
                    elif audio_path.endswith('.wav'):
                        mp3_path = audio_path.replace('.wav', '.mp3')
                        if os.path.exists(mp3_path):
                            mp3_to_delete = mp3_path
            except (NameError, AttributeError):
                # Если переменные не определены, пытаемся использовать исходный audio_path из параметров
                # audio_path доступен как параметр функции
                if audio_path.endswith('.mp3') and os.path.exists(audio_path):
                    mp3_to_delete = audio_path
                elif audio_path.endswith('.wav'):
                    mp3_path = audio_path.replace('.wav', '.mp3')
                    if os.path.exists(mp3_path):
                        mp3_to_delete = mp3_path
            
            if mp3_to_delete:
                try:
                    os.remove(mp3_to_delete)
                    print(f"🗑️ Удален исходный MP3 файл: {mp3_to_delete}")
                except Exception as delete_error:
                    print(f"⚠️ Не удалось удалить MP3 файл {mp3_to_delete}: {delete_error}")
        
        # Обновляем статус задачи с ошибкой перед пробросом исключения
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка транскрипции',
                'error': error_message,
                'exc_type': type(e).__name__
            }
        )
        
        # Пробрасываем исключение дальше, чтобы задача считалась неуспешной (FAILURE)
        # Это позволит правильно обработать ошибку в вызывающем коде
        raise


@celery_app.task(bind=True)
def create_srt_from_youtube_task(self, youtube_url: str, model_size: str = "medium"):
    """
    Задача для создания JSON файла с субтитрами из YouTube URL
    Выполняет загрузку аудио (если нужно) и транскрипцию последовательно
    
    Args:
        youtube_url: URL видео на YouTube
        model_size: Размер модели WhisperX (tiny, base, small, medium, large)
    """
    try:
        # Убеждаемся, что папки существуют
        video_dir, srt_dir = ensure_directories()
        
        # Извлекаем YouTube ID
        youtube_id = extract_youtube_id(youtube_url)
        print(f"Создание JSON субтитров для YouTube ID: {youtube_id}")
        
        # Проверяем, существует ли уже JSON файл
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
            
            # Запускаем задачу загрузки аудио синхронно (внутри задачи)
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
        
        # Запускаем транскрипцию
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Начинаем транскрипцию...', 'progress': 50}
        )
        
        # Используем transcribe_audio_task для транскрипции
        transcription_result = transcribe_audio_task.apply(
            args=[audio_path, youtube_id, model_size]
        )
        
        if transcription_result.successful():
            result = transcription_result.result
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise Exception(f"Ошибка транскрипции: {result.get('error', 'Неизвестная ошибка')}")
            
            # Проверяем, что JSON файл создан
            if not os.path.exists(json_path):
                raise Exception("JSON файл не был создан после транскрипции")
            
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
        else:
            # Задача транскрипции завершилась с ошибкой
            # Извлекаем информацию об ошибке из task.info
            error_info = transcription_result.info
            if isinstance(error_info, dict):
                error_message = error_info.get('error', 'Неизвестная ошибка транскрипции')
            elif isinstance(error_info, Exception):
                error_message = str(error_info)
            else:
                error_message = str(error_info) if error_info else 'Неизвестная ошибка транскрипции'
            
            raise Exception(f"Ошибка транскрипции: {error_message}")
        
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
