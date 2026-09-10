"""Транскрипция аудио через OpenAI Audio API (без изменения WhisperX)."""

import os
import re
import subprocess
import tempfile
import time
from typing import List, Dict, Optional, Tuple

import requests

from app.config import settings


OPENAI_TRANSCRIPTIONS_URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

_ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "vs", "etc", "eg", "ie",
    "т.д", "т.п", "ул", "г", "рис", "стр",
}
_SENTENCE_END = re.compile(r'[.!?…]["»”’)\]]*$')


def segments_to_srt(segments: List[Dict]) -> str:
    """Собирает SRT из сегментов {start, end, text}."""
    lines = []
    index = 1
    for segment in segments:
        text = (segment.get("text") or "").strip()
        if not text:
            continue
        start = float(segment.get("start") or 0)
        end = float(segment.get("end") or start)
        if end <= start:
            end = start + 0.5
        lines.append(str(index))
        lines.append(f"{_srt_ts(start)} --> {_srt_ts(end)}")
        lines.append(text)
        lines.append("")
        index += 1
    return "\n".join(lines)


def _srt_ts(seconds: float) -> str:
    total_ms = max(0, int(round(seconds * 1000)))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _join_words(words: List[Dict]) -> str:
    parts = []
    for item in words:
        token = (item.get("word") or item.get("text") or "")
        if not token:
            continue
        if parts and not token.startswith((" ", "\n")) and not parts[-1].endswith(" "):
            if token[0] in ",.;:!?…)]}":
                parts.append(token)
            else:
                parts.append(" " + token)
        else:
            parts.append(token)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _is_sentence_end(word: str, next_word: Optional[str]) -> bool:
    token = (word or "").strip()
    if not token or not _SENTENCE_END.search(token):
        return False
    core = re.sub(r'["»”’)\]]+$', "", token)
    if re.fullmatch(r"\d+\.", core):
        return False
    stem = core.rstrip(".…").lower().replace(".", "")
    if stem in _ABBREVIATIONS:
        return False
    if next_word:
        nxt = next_word.strip()
        if nxt and nxt[0].islower():
            return False
    return True


def group_words_into_sentences(words: List[Dict]) -> List[Dict]:
    """Группирует слова OpenAI в предложения. start/end — реальные word-таймкоды."""
    sentences: List[Dict] = []
    current: List[Dict] = []

    for index, item in enumerate(words):
        token = (item.get("word") or item.get("text") or "").strip()
        if not token:
            continue
        current.append(item)
        nxt = None
        if index + 1 < len(words):
            nxt = words[index + 1].get("word") or words[index + 1].get("text")
        if _is_sentence_end(token, nxt):
            text = _join_words(current)
            if text:
                sentences.append({
                    "start": float(current[0].get("start", 0)),
                    "end": float(current[-1].get("end", current[-1].get("start", 0))),
                    "text": text,
                })
            current = []

    if current:
        text = _join_words(current)
        if text:
            sentences.append({
                "start": float(current[0].get("start", 0)),
                "end": float(current[-1].get("end", current[-1].get("start", 0))),
                "text": text,
            })
    return sentences


class OpenAIWhisperService:
    """
    Транскрипция через OpenAI.

    Длинные файлы режутся на чанки (лимит 25 МБ).
    whisper-1 отдаёт слова с таймкодами — из них собираются предложения.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.openai_transcription_model
        self.chunk_seconds = max(60, int(settings.openai_chunk_duration_minutes * 60))
        self.overlap_seconds = max(0, int(settings.openai_chunk_overlap_seconds))
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY не задан. Укажите ключ в .env или config.")

    def transcribe_audio(self, audio_path: str) -> List[Dict]:
        if not os.path.exists(audio_path):
            raise FileNotFoundError(f"Аудио файл не найден: {audio_path}")

        size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        duration = self._probe_duration(audio_path)
        print(
            f"🎤 OpenAI транскрипция: {audio_path} "
            f"({size_mb:.1f} МБ, {duration:.1f}s), модель={self.model}"
        )

        if duration <= self.chunk_seconds and os.path.getsize(audio_path) <= MAX_UPLOAD_BYTES:
            _, words = self._transcribe_file(audio_path)
        else:
            print(
                f"  Длинное аудио — режем на чанки по {self.chunk_seconds}s "
                f"(overlap {self.overlap_seconds}s)"
            )
            _, words = self._transcribe_by_duration(audio_path, duration)

        if not words:
            raise RuntimeError(
                "OpenAI не вернул word-таймкоды. Для нарезки по предложениям нужен whisper-1."
            )

        segments = group_words_into_sentences(words)
        segments = [
            {
                "start": round(float(s.get("start", 0)), 3),
                "end": round(float(s.get("end", 0)), 3),
                "text": (s.get("text") or "").strip(),
            }
            for s in segments
            if (s.get("text") or "").strip()
        ]
        print(f"✅ OpenAI: {len(segments)} предложений из {len(words)} слов")
        return segments

    def _probe_duration(self, audio_path: str) -> float:
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            audio_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        try:
            duration = float((result.stdout or "").strip())
            if duration > 0:
                return duration
        except ValueError:
            pass
        raise RuntimeError(f"Не удалось определить длительность аудио: {result.stderr or result.stdout}")

    def _transcribe_by_duration(self, audio_path: str, duration: float) -> Tuple[List[Dict], List[Dict]]:
        all_segments: List[Dict] = []
        all_words: List[Dict] = []
        step = max(30, self.chunk_seconds - self.overlap_seconds)
        starts = []
        cursor = 0.0
        while cursor < duration:
            starts.append(cursor)
            next_cursor = cursor + step
            if next_cursor >= duration:
                break
            cursor = next_cursor

        total = len(starts)
        for index, start in enumerate(starts, start=1):
            length = min(self.chunk_seconds, max(0.5, duration - start))
            print(f"  Чанк {index}/{total}: {start:.1f}s + {length:.1f}s")
            chunk_segments, chunk_words = self._transcribe_window(audio_path, start, length)
            for segment in chunk_segments:
                abs_start = float(segment.get("start", 0)) + start
                abs_end = float(segment.get("end", 0)) + start
                if start > 0 and abs_start < start + self.overlap_seconds:
                    continue
                segment["start"] = abs_start
                segment["end"] = abs_end
                all_segments.append(segment)
            for word in chunk_words:
                abs_start = float(word.get("start", 0)) + start
                if start > 0 and abs_start < start + self.overlap_seconds:
                    continue
                word["start"] = abs_start
                word["end"] = float(word.get("end", 0)) + start
                all_words.append(word)
        return all_segments, all_words

    def _transcribe_window(self, audio_path: str, start: float, length: float) -> Tuple[List[Dict], List[Dict]]:
        if length <= 1:
            return [], []

        chunk_path = self._extract_chunk(audio_path, start, length)
        try:
            size = os.path.getsize(chunk_path)
            if size <= MAX_UPLOAD_BYTES:
                return self._transcribe_file(chunk_path)

            print(f"    Чанк {size / (1024 * 1024):.1f} МБ > 24 МБ — делим пополам")
        finally:
            if os.path.exists(chunk_path):
                os.remove(chunk_path)

        mid = length / 2
        left_segments, left_words = self._transcribe_window(audio_path, start, mid)
        right_segments, right_words = self._transcribe_window(audio_path, start + mid, length - mid)
        for segment in right_segments:
            segment["start"] = float(segment.get("start", 0)) + mid
            segment["end"] = float(segment.get("end", 0)) + mid
        for word in right_words:
            word["start"] = float(word.get("start", 0)) + mid
            word["end"] = float(word.get("end", 0)) + mid
        return left_segments + right_segments, left_words + right_words

    def _extract_chunk(self, audio_path: str, start: float, length: float) -> str:
        fd, chunk_path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}",
            "-t", f"{length:.3f}",
            "-i", audio_path,
            "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "libmp3lame", "-b:a", "64k",
            chunk_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0 or not os.path.exists(chunk_path) or os.path.getsize(chunk_path) < 256:
            if os.path.exists(chunk_path):
                os.remove(chunk_path)
            raise RuntimeError(f"Не удалось вырезать чанк @ {start}s: {result.stderr or result.stdout}")
        return chunk_path

    def _request_fields(self) -> dict:
        model = (self.model or "").lower()
        if model.endswith("diarize") or "diarize" in model:
            return {
                "model": self.model,
                "response_format": "diarized_json",
                "chunking_strategy": "auto",
            }
        if model == "whisper-1":
            return {
                "model": self.model,
                "response_format": "verbose_json",
                "timestamp_granularities[]": "word",
            }
        return {
            "model": self.model,
            "response_format": "json",
        }

    def _transcribe_file(self, audio_path: str) -> Tuple[List[Dict], List[Dict]]:
        fields = self._request_fields()
        last_error = None
        for attempt in range(1, 6):
            with open(audio_path, "rb") as audio_file:
                response = requests.post(
                    OPENAI_TRANSCRIPTIONS_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    files={"file": (os.path.basename(audio_path), audio_file, "application/octet-stream")},
                    data=fields,
                    timeout=600,
                )

            if response.status_code < 400:
                return self._extract_payload(response.json())

            last_error = f"OpenAI API ошибка {response.status_code}: {response.text}"
            retryable = response.status_code in (408, 409, 429, 500, 502, 503, 504)
            if not retryable or attempt == 5:
                raise RuntimeError(last_error)

            wait = min(2 ** attempt, 30)
            print(f"    {last_error[:200]} — повтор {attempt}/5 через {wait}s")
            time.sleep(wait)

        raise RuntimeError(last_error or "OpenAI API ошибка")

    def _extract_payload(self, payload: dict) -> Tuple[List[Dict], List[Dict]]:
        raw = payload.get("segments") or payload.get("utterances") or []
        segments = []
        for item in raw:
            text = (item.get("text") or item.get("transcript") or "").strip()
            if not text:
                continue
            segments.append({
                "start": item.get("start", 0),
                "end": item.get("end", item.get("start", 0)),
                "text": text,
            })

        words = []
        for item in payload.get("words") or []:
            token = (item.get("word") or item.get("text") or "").strip()
            if not token:
                continue
            words.append({
                "word": token,
                "start": item.get("start", 0),
                "end": item.get("end", item.get("start", 0)),
            })

        if not segments:
            text = (payload.get("text") or "").strip()
            if text:
                segments = [{"start": 0, "end": 0, "text": text}]
        return segments, words
