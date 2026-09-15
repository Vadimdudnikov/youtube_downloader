"""Транскрипция через ElevenLabs Speech-to-Text (Scribe)."""

import os
import time
from typing import Dict, List, Optional

import requests

from app.config import settings
from app.openai_whisper_service import sentences_from_transcript


ELEVENLABS_STT_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def convert_elevenlabs_to_segments(payload: dict) -> List[Dict]:
    """
    ElevenLabs: {segments: [{text, start_time, end_time, words: [{text, start_time, end_time}]}]}
    Наш формат: [{start, end, text}] по предложениям.
    """
    words: List[Dict] = []
    texts: List[str] = []

    for segment in payload.get("segments") or []:
        piece = (segment.get("text") or "").strip()
        if piece:
            texts.append(piece)
        for item in segment.get("words") or []:
            token = (item.get("text") or item.get("word") or "")
            if not token or not token.strip():
                continue
            words.append({
                "word": token.strip(),
                "start": float(item.get("start_time", item.get("start", 0)) or 0),
                "end": float(item.get("end_time", item.get("end", 0)) or 0),
            })

    if not words:
        for item in payload.get("words") or []:
            token = (item.get("text") or item.get("word") or "")
            if not token or not token.strip():
                continue
            words.append({
                "word": token.strip(),
                "start": float(item.get("start_time", item.get("start", 0)) or 0),
                "end": float(item.get("end_time", item.get("end", 0)) or 0),
            })

    text = " ".join(texts).strip() or (payload.get("text") or "").strip()
    segments = sentences_from_transcript(text, words)
    return [
        {
            "start": round(float(s.get("start", 0)), 3),
            "end": round(float(s.get("end", 0)), 3),
            "text": (s.get("text") or "").strip(),
        }
        for s in segments
        if (s.get("text") or "").strip()
    ]


class ElevenLabsService:
    """Speech-to-text через ElevenLabs Scribe. Лимит файла большой — без нарезки как у OpenAI."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.elevenlabs_api_key
        self.model = model or settings.elevenlabs_model
        self.language_code = (settings.elevenlabs_language_code or "").strip() or None
        if not self.api_key:
            raise ValueError("ELEVENLABS_API_KEY не задан. Укажите ключ в .env.")

    def transcribe_audio(self, audio_path: str) -> List[Dict]:
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
