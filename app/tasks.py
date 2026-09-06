import os
import subprocess
import json
from app.celery_app import celery_app
from app.config import settings
from app.rapidapi_service import RapidAPIService
from app.whisperx_service import WhisperXService
from app.direct_media_service import DirectMediaService
from app.openai_whisper_service import OpenAIWhisperService, segments_to_srt
from app.media_utils import extract_media_id, is_youtube_url

import warnings

# Глобальное отключение стандартных предупреждений
warnings.filterwarnings("ignore", category=UserWarning)
warnings.filterwarnings("ignore", category=FutureWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Обратная совместимость для импортов из routers
extract_youtube_id = extract_media_id


def ensure_directories():
    """Создает необходимые директории если их нет"""
    assets_dir = "assets"
    video_dir = os.path.join(assets_dir, "video")
    srt_dir = os.path.join(assets_dir, "srt")
    nvoice_dir = os.path.join(assets_dir, "nvoice")
    
    os.makedirs(video_dir, exist_ok=True)
    os.makedirs(srt_dir, exist_ok=True)
    os.makedirs(nvoice_dir, exist_ok=True)
    
    return video_dir, srt_dir, nvoice_dir


def _ensure_ffmpeg():
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        raise RuntimeError('FFmpeg не найден. Установите FFmpeg для конвертации аудио в MP3.')


@celery_app.task(bind=True)
def download_video_task(self, youtube_url: str, audio_only: bool = False):
    """
    Задача для загрузки медиа в MP3 (стандартная схема):
    - YouTube → RapidAPI
    - Прямые ссылки (mp4 playprofi и т.п.) → скачать → конвертировать в MP3 → no_vocals
    """
    try:
        media_url = youtube_url
        media_id = extract_media_id(media_url)
        print(f"Media ID: {media_id} | YouTube={is_youtube_url(media_url)}")

        video_dir, srt_dir, _ = ensure_directories()
        mp3_file = f"{media_id}.mp3"
        mp3_path = os.path.join(video_dir, mp3_file)

        # ---------- Прямые медиа-ссылки (mp4 и т.п.) → всегда в MP3 ----------
        if not is_youtube_url(media_url):
            _ensure_ffmpeg()
            self.update_state(
                state='PROGRESS',
                meta={'status': 'Начинаем загрузку прямой ссылки...', 'progress': 0}
            )

            if os.path.exists(mp3_path):
                file_size = os.path.getsize(mp3_path)
                print(f"Файл уже существует локально: {mp3_file}")
                create_no_vocals_task.delay(mp3_path)
                return {
                    'status': 'completed',
                    'progress': 100,
                    'message': 'Аудио найдено локально (пропущена загрузка)',
                    'file_path': mp3_path,
                    'file_name': mp3_file,
                    'file_size': file_size,
                    'download_type': 'аудио',
                    'youtube_id': media_id,
                    'cached': True,
                    'source': 'direct',
                }

            self.update_state(
                state='PROGRESS',
                meta={'status': 'Скачиваем mp4 и конвертируем в MP3...', 'progress': 20}
            )
            service = DirectMediaService()
            result = service.download_media(
                url=media_url,
                video_path=os.path.join(video_dir, f"{media_id}.mp4"),
                audio_path=mp3_path,
                audio_only=True,
            )
            file_size = os.path.getsize(result['file_path'])
            print(f"✅ Аудио успешно загружено: {mp3_file} ({file_size / 1024 / 1024:.2f} МБ)")

            self.update_state(
                state='PROGRESS',
                meta={'status': 'Загрузка завершена', 'progress': 100}
            )
            create_no_vocals_task.delay(result['file_path'])
            return {
                'status': 'completed',
                'progress': 100,
                'message': 'Аудио успешно загружено по прямой ссылке (mp4→mp3)',
                'file_path': result['file_path'],
                'file_name': result['file_name'],
                'file_size': file_size,
                'download_type': 'аудио',
                'youtube_id': media_id,
                'cached': False,
                'source': 'direct',
            }

        # ---------- YouTube через RapidAPI (только аудио) ----------
        if not audio_only:
            return {
                'status': 'failed',
                'error': 'Для YouTube поддерживается только загрузка аудио. Используйте audio_only=True.',
                'exc_type': 'UnsupportedOperation'
            }

        _ensure_ffmpeg()

        self.update_state(state='PROGRESS', meta={'status': 'Начинаем загрузку через RapidAPI...', 'progress': 0})

        if os.path.exists(mp3_path):
            file_size = os.path.getsize(mp3_path)
            print(f"Файл уже существует локально: {mp3_file}")
            create_no_vocals_task.delay(mp3_path)
            return {
                'status': 'completed',
                'progress': 100,
                'message': 'Аудио найдено локально (пропущена загрузка)',
                'file_path': mp3_path,
                'file_name': mp3_file,
                'file_size': file_size,
                'download_type': 'аудио',
                'youtube_id': media_id,
                'cached': True,
                'source': 'youtube',
            }

        self.update_state(state='PROGRESS', meta={'status': 'Подключаемся к RapidAPI...', 'progress': 10})
        rapidapi = RapidAPIService()

        self.update_state(state='PROGRESS', meta={'status': 'Скачиваем аудио через RapidAPI...', 'progress': 20})
        print(f"Начинаем загрузку аудио через RapidAPI для {media_url}")

        downloaded_path = rapidapi.download_youtube_audio(
            url=media_url,
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
        create_no_vocals_task.delay(downloaded_path)
        return {
            'status': 'completed',
            'progress': 100,
            'message': 'Аудио успешно загружено через RapidAPI',
            'file_path': downloaded_path,
            'file_name': mp3_file,
            'file_size': file_size,
            'download_type': 'аудио',
            'youtube_id': media_id,
            'cached': False,
            'source': 'youtube',
        }

    except Exception as e:
        error_message = str(e)
        print(f"Ошибка загрузки: {error_message}")
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


@celery_app.task(bind=True, time_limit=90 * 60, soft_time_limit=85 * 60)
def create_no_vocals_task(self, mp3_path: str):
    """
    Создаёт аудио без голоса (инструментал) из MP3 через Demucs и сохраняет в папку nvoice
    с тем же именем файла. Вызывается после успешного скачивания ролика.
    """
    import tempfile
    import shutil
    try:
        if not os.path.exists(mp3_path):
            raise FileNotFoundError(f"Аудио файл не найден: {mp3_path}")

        video_dir, srt_dir, nvoice_dir = ensure_directories()
        base_name = os.path.splitext(os.path.basename(mp3_path))[0]
        out_mp3_name = f"{base_name}.mp3"
        out_mp3_path = os.path.join(nvoice_dir, out_mp3_name)

        if os.path.exists(out_mp3_path):
            self.update_state(state='PROGRESS', meta={'status': 'Файл без голоса уже существует', 'progress': 100})
            return {
                'status': 'completed',
                'message': 'Аудио без голоса уже создано',
                'file_path': out_mp3_path,
                'file_name': out_mp3_name,
                'cached': True
            }

        self.update_state(state='PROGRESS', meta={'status': 'Запуск Demucs...', 'progress': 10})

        with tempfile.TemporaryDirectory(prefix="demucs_") as tmp_dir:
            # demucs --two-stems=vocals создаёт no_vocals.wav и vocals.wav
            cmd = [
                "demucs", "--two-stems=vocals", "-o", tmp_dir, mp3_path
            ]
            self.update_state(state='PROGRESS', meta={'status': 'Разделение источников (Demucs)...', 'progress': 20})
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if proc.returncode != 0:
                raise RuntimeError(f"Demucs ошибка: {proc.stderr or proc.stdout}")

            # Структура: tmp_dir/htdemucs/<base_name>/no_vocals.wav
            model_subdir = "htdemucs"
            track_subdir = base_name
            no_vocals_wav = os.path.join(tmp_dir, model_subdir, track_subdir, "no_vocals.wav")
            if not os.path.exists(no_vocals_wav):
                # попробовать другие имена (например, с суффиксом из-за точки в имени)
                for d in os.listdir(os.path.join(tmp_dir, model_subdir)):
                    candidate = os.path.join(tmp_dir, model_subdir, d, "no_vocals.wav")
                    if os.path.exists(candidate):
                        no_vocals_wav = candidate
                        break
                else:
                    raise FileNotFoundError(f"Demucs не создал no_vocals.wav в {tmp_dir}")

            self.update_state(state='PROGRESS', meta={'status': 'Конвертация в MP3...', 'progress': 90})
            # Конвертируем WAV в MP3 в nvoice
            conv = subprocess.run([
                "ffmpeg", "-y", "-i", no_vocals_wav, "-acodec", "libmp3lame", "-q:a", "2", out_mp3_path
            ], capture_output=True, text=True, timeout=600)
            if conv.returncode != 0:
                raise RuntimeError(f"FFmpeg ошибка: {conv.stderr or conv.stdout}")

        self.update_state(state='PROGRESS', meta={'status': 'Готово', 'progress': 100})
        return {
            'status': 'completed',
            'message': 'Аудио без голоса создано',
            'file_path': out_mp3_path,
            'file_name': out_mp3_name,
            'cached': False
        }
    except Exception as e:
        error_message = str(e)
        print(f"Ошибка create_no_vocals: {error_message}")
        self.update_state(state='FAILURE', meta={'status': 'Ошибка', 'error': error_message, 'exc_type': type(e).__name__})
        return {'status': 'failed', 'error': error_message, 'exc_type': type(e).__name__}


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
            video_dir, srt_dir, _ = ensure_directories()
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
    Задача для создания JSON файла с субтитрами из медиа URL
    (YouTube или прямая ссылка на видео, например playprofi).
    Выполняет загрузку аудио (если нужно) и транскрипцию последовательно.

    Args:
        youtube_url: URL видео (YouTube или прямой media URL)
        model_size: Размер модели WhisperX (tiny, base, small, medium, large)
    """
    try:
        video_dir, srt_dir, _ = ensure_directories()

        media_url = youtube_url
        media_id = extract_media_id(media_url)
        print(f"Создание JSON субтитров для Media ID: {media_id} (source={'youtube' if is_youtube_url(media_url) else 'direct'})")

        json_file = f"{media_id}.json"
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
                'youtube_id': media_id,
                'cached': True
            }

        audio_file = f"{media_id}.mp3"
        audio_path = os.path.join(video_dir, audio_file)
        audio_exists = os.path.exists(audio_path)

        # Если есть только видео (прямая ссылка скачана как mp4) — извлекаем аудио
        video_path = os.path.join(video_dir, f"{media_id}.mp4")
        if not audio_exists and os.path.exists(video_path):
            self.update_state(
                state='PROGRESS',
                meta={'status': 'Извлекаем аудио из локального видео...', 'progress': 15}
            )
            _ensure_ffmpeg()
            DirectMediaService().extract_audio_to_mp3(video_path, audio_path)
            audio_exists = True
            print(f"Аудио извлечено из локального видео: {audio_file}")

        if not audio_exists:
            self.update_state(
                state='PROGRESS',
                meta={'status': 'Аудио не найдено. Загружаем аудио...', 'progress': 10}
            )

            print(f"Аудио файл не найден. Загружаем аудио для {media_url}")

            download_result = download_video_task.apply(args=[media_url, True])

            if download_result.successful():
                result = download_result.result
                if isinstance(result, dict) and result.get('status') == 'failed':
                    raise Exception(f"Ошибка загрузки аудио: {result.get('error', 'Неизвестная ошибка')}")
            else:
                raise Exception(f"Ошибка загрузки аудио: {str(download_result.info)}")

            if not os.path.exists(audio_path):
                raise Exception("Аудио файл не был создан после загрузки")

            print(f"Аудио успешно загружено: {audio_file}")
        else:
            print(f"Используем существующий аудио файл: {audio_file}")

        self.update_state(
            state='PROGRESS',
            meta={'status': 'Начинаем транскрипцию...', 'progress': 50}
        )

        transcription_result = transcribe_audio_task.apply(
            args=[audio_path, media_id, model_size]
        )

        if transcription_result.successful():
            result = transcription_result.result
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise Exception(f"Ошибка транскрипции: {result.get('error', 'Неизвестная ошибка')}")

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
                'youtube_id': media_id,
                'cached': False,
                'audio_cached': audio_exists
            }
        else:
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


def _ensure_audio_for_media(self, media_url: str, media_id: str, video_dir: str):
    """Скачивает/извлекает mp3 для медиа URL. Не меняет старую WhisperX-задачу."""
    audio_file = f"{media_id}.mp3"
    audio_path = os.path.join(video_dir, audio_file)
    audio_exists = os.path.exists(audio_path)

    video_path = os.path.join(video_dir, f"{media_id}.mp4")
    if not audio_exists and os.path.exists(video_path):
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Извлекаем аудио из локального видео...', 'progress': 15}
        )
        _ensure_ffmpeg()
        DirectMediaService().extract_audio_to_mp3(video_path, audio_path)
        audio_exists = True
        print(f"Аудио извлечено из локального видео: {audio_file}")

    if not audio_exists:
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Аудио не найдено. Загружаем аудио...', 'progress': 10}
        )
        print(f"Аудио файл не найден. Загружаем аудио для {media_url}")
        download_result = download_video_task.apply(args=[media_url, True])
        if download_result.successful():
            result = download_result.result
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise Exception(f"Ошибка загрузки аудио: {result.get('error', 'Неизвестная ошибка')}")
        else:
            raise Exception(f"Ошибка загрузки аудио: {str(download_result.info)}")
        if not os.path.exists(audio_path):
            raise Exception("Аудио файл не был создан после загрузки")
        print(f"Аудио успешно загружено: {audio_file}")
    else:
        print(f"Используем существующий аудио файл: {audio_file}")

    return audio_path, audio_exists


@celery_app.task(bind=True, time_limit=4 * 60 * 60, soft_time_limit=3 * 60 * 60 + 50 * 60)
def transcribe_audio_openai_task(self, audio_path: str, task_id: str = None):
    """Транскрипция через OpenAI API. Сохраняет JSON и SRT в assets/srt."""
    try:
        print(f"🎤 OpenAI транскрипция: {audio_path}")
        self.update_state(
            state='PROGRESS',
            meta={'status': 'Отправляем аудио в OpenAI...', 'progress': 20}
        )

        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Аудио файл не найден: {audio_path}")

        service = OpenAIWhisperService()
        segments = service.transcribe_audio(audio_path)

        if not segments:
            raise Exception("OpenAI не смог распознать речь в аудио файле (0 сегментов)")

        if task_id:
            _, srt_dir, _ = ensure_directories()
            json_path = os.path.join(srt_dir, f"{task_id}.json")
            srt_path = os.path.join(srt_dir, f"{task_id}.srt")

            json_data = [
                {
                    'start': segment.get('start', 0),
                    'end': segment.get('end', 0),
                    'text': segment.get('text', '').strip(),
                }
                for segment in segments
            ]
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump(json_data, f, ensure_ascii=False, indent=4)
            with open(srt_path, 'w', encoding='utf-8') as f:
                f.write(segments_to_srt(json_data))
            print(f"✅ OpenAI результат: {json_path}, {srt_path}")

        self.update_state(
            state='PROGRESS',
            meta={'status': f'Транскрипция завершена: {len(segments)} сегментов', 'progress': 100}
        )
        return {
            'status': 'success',
            'segments': segments,
            'message': f'Транскрипция OpenAI завершена: {len(segments)} сегментов',
            'segments_count': len(segments),
            'youtube_id': task_id if task_id else None,
            'provider': 'openai',
            'model': settings.openai_transcription_model,
            'file_name': f"{task_id}.json" if task_id else None,
            'srt_file_name': f"{task_id}.srt" if task_id else None,
        }
    except Exception as e:
        error_message = str(e)
        print(f"❌ Ошибка OpenAI транскрипции: {error_message}")
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка транскрипции OpenAI',
                'error': error_message,
                'exc_type': type(e).__name__,
            }
        )
        raise


@celery_app.task(bind=True, time_limit=4 * 60 * 60, soft_time_limit=3 * 60 * 60 + 50 * 60)
def create_srt_openai_task(self, youtube_url: str):
    """Создаёт JSON+SRT через OpenAI API (скачивание как в старой схеме)."""
    try:
        video_dir, srt_dir, _ = ensure_directories()
        media_url = youtube_url
        media_id = extract_media_id(media_url)
        print(f"OpenAI SRT для Media ID: {media_id}")

        json_file = f"{media_id}.json"
        srt_file = f"{media_id}.srt"
        json_path = os.path.join(srt_dir, json_file)
        srt_path = os.path.join(srt_dir, srt_file)

        if os.path.exists(json_path) and os.path.exists(srt_path):
            file_size = os.path.getsize(json_path)
            return {
                'status': 'completed',
                'progress': 100,
                'message': 'JSON и SRT уже существуют',
                'file_path': json_path,
                'file_name': json_file,
                'srt_file_name': srt_file,
                'file_size': file_size,
                'youtube_id': media_id,
                'cached': True,
                'provider': 'openai',
            }

        audio_path, audio_exists = _ensure_audio_for_media(self, media_url, media_id, video_dir)

        self.update_state(
            state='PROGRESS',
            meta={'status': 'Начинаем транскрипцию через OpenAI...', 'progress': 50}
        )

        transcription_result = transcribe_audio_openai_task.apply(args=[audio_path, media_id])
        if transcription_result.successful():
            result = transcription_result.result or {}
            if isinstance(result, dict) and result.get('status') == 'failed':
                raise Exception(f"Ошибка транскрипции: {result.get('error', 'Неизвестная ошибка')}")
            if not os.path.exists(json_path):
                raise Exception("JSON файл не был создан после транскрипции OpenAI")

            file_size = os.path.getsize(json_path)
            return {
                'status': 'completed',
                'progress': 100,
                'message': result.get('message', 'JSON и SRT успешно созданы через OpenAI'),
                'file_path': json_path,
                'file_name': json_file,
                'srt_file_name': srt_file if os.path.exists(srt_path) else None,
                'file_size': file_size,
                'youtube_id': media_id,
                'segments_count': result.get('segments_count'),
                'cached': False,
                'audio_cached': audio_exists,
                'provider': 'openai',
                'model': settings.openai_transcription_model,
            }

        error_info = transcription_result.info
        if isinstance(error_info, dict):
            error_message = error_info.get('error', 'Неизвестная ошибка транскрипции')
        else:
            error_message = str(error_info) if error_info else 'Неизвестная ошибка транскрипции'
        raise Exception(f"Ошибка транскрипции: {error_message}")

    except Exception as e:
        error_message = str(e)
        print(f"Ошибка создания SRT через OpenAI: {error_message}")
        self.update_state(
            state='FAILURE',
            meta={
                'status': 'Ошибка создания SRT через OpenAI',
                'error': error_message,
                'exc_type': type(e).__name__,
            }
        )
        return {
            'status': 'failed',
            'error': error_message,
            'exc_type': type(e).__name__,
        }
