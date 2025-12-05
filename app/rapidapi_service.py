"""
Сервис для скачивания YouTube аудио через RapidAPI
"""

import os
import time
import subprocess
import requests
from urllib.parse import urlparse, unquote
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class RapidAPIService:
    """Сервис для скачивания YouTube аудио через RapidAPI"""
    
    def __init__(self, api_key: str = None):
        self.api_key = api_key or "e7e30acb7emsh43a9ab3e385c352p1accbcjsn34a2c2ec6816"
        self.host = "youtube-video-fast-downloader-24-7.p.rapidapi.com"
        self.default_quality = "251"
        self.default_bitrate = "192k"
        
        if not self.api_key or self.api_key == "your_rapidapi_key_here":
            logger.warning("RAPIDAPI_KEY не настроен, используйте переменную окружения или обновите config.py")
            raise ValueError("RAPIDAPI_KEY не настроен. Установите ключ в config.py или переменной окружения RAPIDAPI_KEY")
    
    def get_video_id_from_url(self, url: str) -> str:
        """Извлекает ID видео из YouTube URL"""
        import re
        
        patterns = [
            r'(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([^&\n?#]+)',
            r'youtube\.com/v/([^&\n?#]+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1)
        
        # Если не найден, возможно это уже ID
        if len(url) == 11 and url.replace('-', '').replace('_', '').isalnum():
            return url
            
        raise ValueError(f"Не удалось извлечь ID видео из URL: {url}")
    
    def get_info_from_rapidapi(self, video_id: str, quality: str = None, timeout: int = 60, max_retries: int = 3) -> dict:
        """Получает информацию о файле от RapidAPI с повторными попытками"""
        quality = quality or self.default_quality
        url = f"https://{self.host}/download_audio/{video_id}"
        
        headers = {
            "x-rapidapi-host": self.host,
            "x-rapidapi-key": self.api_key,
            "Accept": "application/json",
        }
        
        logger.info(f"Запрашиваем информацию от RapidAPI для видео {video_id} (таймаут: {timeout}s, попыток: {max_retries})")
        
        last_exception = None
        
        for attempt in range(max_retries):
            try:
                logger.info(f"Попытка {attempt + 1}/{max_retries}")
                resp = requests.get(url, headers=headers, params={"quality": quality}, timeout=timeout)
                resp.raise_for_status()
                logger.info(f"Успешно получена информация от RapidAPI на попытке {attempt + 1}")
                return resp.json()
            except requests.RequestException as e:
                last_exception = e
                logger.warning(f"Попытка {attempt + 1}/{max_retries} неудачна: {e}")
                if attempt < max_retries - 1:
                    wait_time = (attempt + 1) * 2  # Экспоненциальная задержка: 2, 4, 6 секунд
                    logger.info(f"Ждем {wait_time} секунд перед следующей попыткой...")
                    time.sleep(wait_time)
        
        logger.error(f"Все {max_retries} попыток неудачны. Последняя ошибка: {last_exception}")
        raise last_exception
    
    def wait_for_file_url(self, file_url: str, max_wait: int = 600, referer: str = None) -> bool:
        """
        Ждет пока файл станет доступен по URL
        """
        start = time.time()
        attempt = 0
        
        logger.info(f"Ожидаем доступности файла: {file_url}")
        
        while True:
            attempt += 1
            headers = {}
            if referer:
                headers["Referer"] = referer
            
            try:
                # Сначала пробуем HEAD запрос
                r = requests.head(file_url, headers=headers, allow_redirects=True, timeout=10)
                
                if r.status_code == 200:
                    logger.info("Файл доступен!")
                    return True
                
                if r.status_code == 404:
                    elapsed = time.time() - start
                    if elapsed > max_wait:
                        logger.error("Превышено время ожидания файла")
                        return False
                    
                    wait = min(2 * attempt, 30)
                    logger.info(f"Файл не готов (404). Ждем {wait}s...")
                    time.sleep(wait)
                    continue
                
                # Для других статусов пробуем GET с Range
                if r.status_code in (403, 401):
                    logger.warning(f"HEAD вернул {r.status_code} - возможно нужны cookies/авторизация")
                    return False
                
                # Пробуем GET с Range 0-0
                r2 = requests.get(file_url, headers={**headers, "Range": "bytes=0-0"}, timeout=10, stream=True)
                
                if r2.status_code in (200, 206):
                    logger.info("Файл доступен!")
                    return True
                
                if r2.status_code == 404:
                    elapsed = time.time() - start
                    if elapsed > max_wait:
                        logger.error("Превышено время ожидания файла")
                        return False
                    time.sleep(min(2 * attempt, 30))
                    continue
                
                if r2.status_code in (403, 401):
                    logger.warning(f"GET вернул {r2.status_code} - нужны cookies/headers")
                    return False
                    
            except requests.RequestException as e:
                elapsed = time.time() - start
                if elapsed > max_wait:
                    logger.error(f"Таймаут ожидания файла: {e}")
                    return False
                time.sleep(min(2 * attempt, 15))
                continue
    
    def download_and_convert_to_mp3(self, file_url: str, output_path: str, bitrate: str = None, referer: str = None) -> str:
        """Скачивает файл и конвертирует в MP3 через ffmpeg"""
        import tempfile
        
        bitrate = bitrate or self.default_bitrate
        
        # Проверяем наличие ffmpeg
        if not self._check_ffmpeg():
            raise RuntimeError("ffmpeg не найден в PATH. Установите ffmpeg.")
        
        # Определяем формат файла по расширению URL
        file_ext = None
        if '.opus' in file_url.lower():
            file_ext = '.opus'
        elif '.ogg' in file_url.lower():
            file_ext = '.ogg'
        elif '.webm' in file_url.lower():
            file_ext = '.webm'
        
        # Если это opus/ogg/webm, сначала скачиваем файл полностью, затем конвертируем
        # Это необходимо для правильной обработки длительности при конвертации
        if file_ext in ['.opus', '.ogg', '.webm']:
            logger.info(f"Обнаружен формат {file_ext}, сначала скачиваем файл полностью на диск")
            
            # Создаем временный файл для скачивания
            temp_input = tempfile.NamedTemporaryFile(delete=False, suffix=file_ext)
            temp_input_path = temp_input.name
            temp_input.close()
            
            try:
                # Скачиваем файл полностью
                logger.info(f"Скачиваем файл: {file_url}")
                response = requests.get(file_url, stream=True, timeout=300, headers={"Referer": referer} if referer else {})
                response.raise_for_status()
                
                with open(temp_input_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                
                actual_size = os.path.getsize(temp_input_path)
                if actual_size == 0:
                    raise RuntimeError("Скачанный файл пустой")
                
                logger.info(f"Файл скачан: {temp_input_path} ({actual_size / 1024 / 1024:.2f} МБ)")
                
                # Теперь конвертируем в MP3
                return self._convert_local_file_to_mp3(temp_input_path, output_path, bitrate, file_ext)
                
            finally:
                # Удаляем временный файл
                if os.path.exists(temp_input_path):
                    try:
                        os.remove(temp_input_path)
                    except Exception as e:
                        logger.warning(f"Не удалось удалить временный файл: {e}")
        
        else:
            # Для других форматов (MP3, M4A) используем прямой поток из URL
            cmd = ["ffmpeg", "-y"]
            
            # Добавляем заголовки если нужно
            if referer:
                headers = f"Referer: {referer}\r\n"
                cmd += ["-headers", headers]
            
            cmd += [
                "-i", file_url,
                "-vn",  # Без видео
                "-c:a", "libmp3lame",
                "-b:a", bitrate,
                str(output_path)
            ]
            
            logger.info(f"Запускаем ffmpeg (поток из URL): {' '.join(cmd)}")
            
            try:
                result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
                logger.info(f"Конвертация завершена: {output_path}")
                return output_path
            except subprocess.CalledProcessError as e:
                logger.error(f"Ошибка ffmpeg: {e.stderr}")
                raise
    
    def _convert_local_file_to_mp3(self, input_path: str, output_path: str, bitrate: str, file_ext: str) -> str:
        """Конвертирует локальный файл в MP3"""
        # Создаем директорию для выходного файла если её нет
        output_dir = os.path.dirname(output_path)
        if output_dir and not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)
        
        cmd = ["ffmpeg", "-y"]
        
        # Специальные параметры для opus/ogg файлов
        if file_ext in ['.opus', '.ogg']:
            cmd.extend([
                "-i", input_path,
                "-vn",  # Без видео
                "-c:a", "libmp3lame",
                "-b:a", bitrate,
                "-ar", "48000",  # Сохраняем исходную частоту дискретизации opus
                "-avoid_negative_ts", "make_zero",  # Исправляем проблемы с временными метками
            ])
        elif file_ext == '.webm':
            cmd.extend([
                "-i", input_path,
                "-vn",  # Без видео
                "-c:a", "libmp3lame",
                "-b:a", bitrate,
            ])
        else:
            cmd.extend([
                "-i", input_path,
                "-vn",  # Без видео
                "-c:a", "libmp3lame",
                "-b:a", bitrate,
            ])
        
        cmd.append(str(output_path))
        
        logger.info(f"Запускаем ffmpeg (локальный файл): {' '.join(cmd)}")
        
        try:
            result = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=600)
            
            # Проверяем, что выходной файл создан и не пустой
            if not os.path.exists(output_path):
                raise RuntimeError(f"Выходной файл не был создан: {output_path}")
            
            output_size = os.path.getsize(output_path)
            if output_size == 0:
                raise RuntimeError(f"Выходной файл пустой: {output_path}")
            
            logger.info(f"Конвертация завершена: {output_path} ({output_size / 1024 / 1024:.2f} МБ)")
            return output_path
        except subprocess.TimeoutExpired:
            logger.error(f"Таймаут конвертации (превышено 600 секунд)")
            raise RuntimeError("Таймаут конвертации - файл слишком большой или повреждён")
        except subprocess.CalledProcessError as e:
            logger.error(f"Ошибка ffmpeg при конвертации: {e.stderr}")
            logger.error(f"Stdout: {e.stdout}")
            raise
    
    def _check_ffmpeg(self) -> bool:
        """Проверяет наличие ffmpeg в системе"""
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
    
    def download_youtube_audio(self, url: str, output_path: str, quality: str = None, bitrate: str = None) -> str:
        """
        Основной метод для скачивания YouTube аудио
        
        Args:
            url: YouTube URL или ID видео
            output_path: Путь для сохранения MP3 файла
            quality: Качество аудио (по умолчанию 251)
            bitrate: Битрейт для MP3 (по умолчанию 192k)
            
        Returns:
            str: Путь к скачанному файлу
        """
        try:
            # Извлекаем ID видео
            video_id = self.get_video_id_from_url(url)
            logger.info(f"ID видео: {video_id}")
            
            # Получаем информацию от RapidAPI
            info = self.get_info_from_rapidapi(video_id, quality, timeout=60, max_retries=3)
            
            file_url = info.get("file") or info.get("url")
            if not file_url:
                raise ValueError(f"RapidAPI вернул ответ без file/url: {info}")
            
            logger.info(f"URL файла: {file_url}")
            
            # Ждем пока файл станет доступен
            if not self.wait_for_file_url(file_url):
                raise RuntimeError("Файл не стал доступен в течение указанного времени")
            
            # Скачиваем и конвертируем в MP3
            return self.download_and_convert_to_mp3(file_url, output_path, bitrate)
            
        except Exception as e:
            logger.error(f"Ошибка скачивания YouTube аудио: {e}")
            raise

