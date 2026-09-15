"""Транскрипция через ElevenLabs Speech-to-Text (Scribe)."""

import os
import re
import time
from typing import List, Optional

import requests

from app.config import settings


ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"
_STRONG_END = re.compile(r'[.!?…]["»”’)\]]*$')
_ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "prof", "vs", "etc", "т.д", "т.п"}


def _el_time(item: dict, *keys: str) -> float:
    for key in keys:
        if item.get(key) is not None:
            return float(item[key])
    return 0.0


def _flatten_elevenlabs_words(payload: dict) -> List[dict]:
    raw = []
    for segment in payload.get("segments") or []:
        raw.extend(segment.get("words") or [])
    if not raw:
        raw = payload.get("words") or []

    words = []
    for item in raw:
        token = item.get("text") if "text" in item else item.get("word")
        if token is None:
            continue
        words.append({
            "text": token,
            "start": _el_time(item, "start_time", "start"),
            "end": _el_time(item, "end_time", "end"),
        })
    return words


def _is_real_word(token: str) -> bool:
    return bool((token or "").strip())


def _next_real(words: List[dict], index: int):
    for j in range(index + 1, len(words)):
        if _is_real_word(words[j]["text"]):
            return words[j]
    return None


def _should_split(token: str, gap: Optional[float], next_token: str) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    if gap is None:
        return True

    core = re.sub(r'["»”’)\]]+$', "", token)
    stem = core.rstrip(".…").lower().replace(".", "")
    strong = bool(_STRONG_END.search(token)) and not re.fullmatch(r"\d+\.", core) and stem not in _ABBREVIATIONS
    colon = token.endswith(":") or token.endswith(";")

    # Точка/вопрос/восклицание — граница фразы, как в original
    if strong:
        return True
    if colon and gap >= 0.25:
        return True
    if gap >= 0.55:
        return True
    return False


def convert_elevenlabs_to_segments(payload: dict) -> List[dict]:
    """
    Режет ElevenLabs JSON в наш список {start, end, text}.
    Текст и тайминг берутся из words, границы — пунктуация + пауза, не слепой split большого text.
    """
    words = _flatten_elevenlabs_words(payload)
    if not words:
        text = (payload.get("text") or "").strip()
        return [{"start": 0.0, "end": 0.0, "text": text}] if text else []

    segments: List[dict] = []
    buf: List[dict] = []

    def flush():
        if not buf:
            return
        text = "".join(item["text"] for item in buf).strip()
        real = [item for item in buf if _is_real_word(item["text"])]
        if not text or not real:
            buf.clear()
            return
        segments.append({
            "start": round(float(real[0]["start"]), 3),
            "end": round(float(real[-1]["end"]), 3),
            "text": re.sub(r"\s+", " ", text),
        })
        buf.clear()

    for i, word in enumerate(words):
        buf.append(word)
        if not _is_real_word(word["text"]):
            continue
        nxt = _next_real(words, i)
        gap = None if nxt is None else max(0.0, nxt["start"] - word["end"])
        next_token = "" if nxt is None else nxt["text"]
        if _should_split(word["text"], gap, next_token):
            flush()

    flush()
    return segments


class ElevenLabsService:
    """Speech-to-text через ElevenLabs Scribe. Лимит файла большой — без нарезки как у OpenAI."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.elevenlabs_api_key
        self.model = model or settings.elevenlabs_model
        self.language_code = (settings.elevenlabs_language_code or "").strip() or None
        if not self.api_key:
            raise ValueError("ELEVENLABS_API_KEY не задан. Укажите ключ в .env.")

    def transcribe_audio(self, audio_path: str) -> List[dict]:
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Аудио файл не найден: {audio_path}")

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        print(f"🎤 ElevenLabs транскрипция: {audio_path} ({size_mb:.1f} МБ), модель={self.model}")

        payload = self._transcribe_file(audio_path)
        segments = convert_elevenlabs_to_segments(payload)
        print(f"✅ ElevenLabs: {len(segments)} предложений")
        return segments

    def _transcribe_file(self, audio_path: str) -> dict:
        data = {
            "model_id": self.model,
            "timestamps_granularity": "word",
            "tag_audio_events": "false",
        }
        if self.language_code:
            data["language_code"] = self.language_code

        last_error = None
        for attempt in range(1, 6):
            with open(audio_path, "rb") as audio_file:
                response = requests.post(
                    ELEVENLABS_STT_URL,
                    headers={"xi-api-key": self.api_key},
                    files={"file": (os.path.basename(audio_path), audio_file, "application/octet-stream")},
                    data=data,
                    timeout=3600,
                )

            if response.status_code < 400:
                return response.json()

            last_error = f"ElevenLabs API ошибка {response.status_code}: {response.text}"
            retryable = response.status_code in (408, 409, 429, 500, 502, 503, 504)
            if not retryable or attempt == 5:
                raise RuntimeError(last_error)

            wait = min(2 ** attempt, 30)
            print(f"    {last_error[:200]} — повтор {attempt}/5 через {wait}s")
            time.sleep(wait)

        raise RuntimeError(last_error or "ElevenLabs API ошибка")
