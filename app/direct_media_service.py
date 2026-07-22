"""Скачивание прямых медиа-ссылок (mp4 и т.п.) и конвертация в MP3."""

import os
import logging
import subprocess
import tempfile
from typing import Optional

import requests

from app.media_utils import guess_extension_from_url

logger = logging.getLogger(__name__)


class DirectMediaService:
    """Сервис для скачивания прямых URL на видео/аудио файлы."""

    def __init__(self, timeout: int = 600, chunk_size: int = 1024 * 256):
        self.timeout = timeout
        self.chunk_size = chunk_size

    def download_file(self, url: str, output_path: str) -> str:
        """Скачивает файл по прямому URL в output_path."""
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        logger.info(f"Скачиваем прямой медиа-файл: {url}")
        with requests.get(url, stream=True, timeout=self.timeout) as response:
            response.raise_for_status()
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if chunk:
                        f.write(chunk)

        size = os.path.getsize(output_path)
        if size <= 0:
            raise RuntimeError(f"Скачанный файл пустой: {output_path}")

        logger.info(f"Файл сохранён: {output_path} ({size / 1024 / 1024:.2f} МБ)")
        return output_path

    def extract_audio_to_mp3(self, input_path: str, output_path: str, bitrate: str = "192k") -> str:
        """Извлекает аудиодорожку и сохраняет как MP3."""
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)

        cmd = [
            "ffmpeg", "-y",
            "-i", input_path,
            "-vn",
            "-c:a", "libmp3lame",
            "-b:a", bitrate,
            output_path,
        ]
        logger.info(f"Конвертация в MP3: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg ошибка: {result.stderr or result.stdout}")

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError(f"MP3 не создан или пустой: {output_path}")

        return output_path

    def download_media(
        self,
        url: str,
        video_path: str,
        audio_path: Optional[str] = None,
        audio_only: bool = False,
    ) -> dict:
        """
        Скачивает медиа по прямому URL.

        Returns:
            dict с путями и именами файлов
        """
        ext = guess_extension_from_url(url, default=".mp4")
        # Если целевой video_path без учёта реального расширения — используем как есть,
        # вызывающий код задаёт итоговые пути сам.
        if audio_only:
            if not audio_path:
                raise ValueError("audio_path обязателен при audio_only=True")

            # Для уже-аудио можно сохранить напрямую (если это mp3)
            if ext == ".mp3":
                self.download_file(url, audio_path)
                return {
                    "file_path": audio_path,
                    "file_name": os.path.basename(audio_path),
                    "download_type": "аудио",
                    "source_ext": ext,
                }

            # Скачиваем во временный файл, затем конвертируем в mp3
            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
                tmp_path = tmp.name
            try:
                self.download_file(url, tmp_path)
                self.extract_audio_to_mp3(tmp_path, audio_path)
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        pass

            return {
                "file_path": audio_path,
                "file_name": os.path.basename(audio_path),
                "download_type": "аудио",
                "source_ext": ext,
            }

        # Видео (или исходный файл) как есть
        # Если ожидаемый путь .mp4, а URL с другим расширением — всё равно кладём в video_path
        target = video_path
        if not target.lower().endswith(ext) and ext in {".mp4", ".webm", ".mkv", ".mov"}:
            # оставляем имя, которое передал вызывающий код (обычно {id}.mp4)
            pass

        self.download_file(url, target)
        return {
            "file_path": target,
            "file_name": os.path.basename(target),
            "download_type": "видео",
            "source_ext": ext,
        }
