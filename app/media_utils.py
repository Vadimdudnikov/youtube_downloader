"""Утилиты для определения типа медиа-URL и извлечения стабильного ID."""

import hashlib
import re
from typing import Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, unquote, urlunparse


YOUTUBE_HOSTS = (
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.youtu.be",
)

_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,32}$")


def is_youtube_url(url: str) -> bool:
    """Проверяет, является ли URL ссылкой на YouTube."""
    try:
        host = urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        normalized = {h[4:] if h.startswith("www.") else h for h in YOUTUBE_HOSTS}
        return host in normalized or host.endswith(".youtube.com")
    except Exception:
        return False


def extract_youtube_id(url: str) -> Optional[str]:
    """Извлекает YouTube ID из URL. Возвращает None, если это не YouTube."""
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)([a-zA-Z0-9_-]{11})",
        r"youtube\.com/v/([a-zA-Z0-9_-]{11})",
        r"youtube\.com/shorts/([a-zA-Z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    return None


def strip_query_video_id(url: str) -> Tuple[str, Optional[str]]:
    """
    Если в query есть video_id — убирает его из URL и возвращает значение.
    Остальные query-параметры сохраняются (важно для Я.Диска и т.п.).
    """
    parsed = urlparse(url)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    video_id = None
    kept = []
    for key, value in pairs:
        if key == "video_id" and video_id is None:
            candidate = (value or "").strip()
            video_id = candidate or None
            continue
        kept.append((key, value))

    clean_query = urlencode(kept, doseq=True)
    clean_url = urlunparse(parsed._replace(query=clean_query))
    return clean_url, video_id


def extract_media_id(url: str) -> str:
    """
    Стабильный ID для имени файла:
    - query video_id → его значение (если валидно)
    - YouTube → video id
    - playprofi/media → сегмент пути (например 20075a5c)
    - иначе → md5 от URL
    """
    _, query_video_id = strip_query_video_id(url)
    if query_video_id and _VIDEO_ID_RE.fullmatch(query_video_id):
        return query_video_id

    youtube_id = extract_youtube_id(url)
    if youtube_id:
        return youtube_id

    parsed = urlparse(url)
    path = unquote(parsed.path or "").strip("/")
    parts = [p for p in path.split("/") if p]

    # /uploads/39/20075a5c/original.mp4 → 20075a5c
    if len(parts) >= 2:
        candidate = parts[-2]
        if re.fullmatch(r"[a-zA-Z0-9_-]{6,32}", candidate):
            return candidate

    if parts:
        stem = parts[-1].rsplit(".", 1)[0]
        if re.fullmatch(r"[a-zA-Z0-9_-]{6,32}", stem):
            return stem

    return hashlib.md5(url.encode("utf-8")).hexdigest()[:12]


def resolve_media_ref(url: str) -> Tuple[str, str]:
    """
    Готовит URL к скачиванию и ID для имён файлов.
    Returns:
        (clean_url без query video_id, media_id)
    """
    clean_url, query_video_id = strip_query_video_id(url)
    if query_video_id and _VIDEO_ID_RE.fullmatch(query_video_id):
        return clean_url, query_video_id
    return clean_url, extract_media_id(clean_url)


def guess_extension_from_url(url: str, default: str = ".mp4") -> str:
    """Определяет расширение файла по URL."""
    path = unquote(urlparse(url).path or "")
    if "." in path.rsplit("/", 1)[-1]:
        ext = "." + path.rsplit(".", 1)[-1].lower()
        if re.fullmatch(r"\.[a-z0-9]{2,5}", ext):
            return ext
    return default
