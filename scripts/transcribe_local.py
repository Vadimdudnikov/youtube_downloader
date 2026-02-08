import argparse
import json
import os
import sys
from pathlib import Path


def _project_root() -> Path:
    # scripts/transcribe_local.py -> project root
    return Path(__file__).resolve().parents[1]


# Позволяет запускать скрипт напрямую: python scripts/transcribe_local.py ...
sys.path.insert(0, str(_project_root()))

from app.whisperx_service import WhisperXService  # noqa: E402


def seconds_to_srt_time(seconds: float) -> str:
    if seconds is None:
        seconds = 0.0
    if seconds < 0:
        seconds = 0.0
    ms_total = int(round(seconds * 1000))
    hours = ms_total // 3_600_000
    ms_total -= hours * 3_600_000
    minutes = ms_total // 60_000
    ms_total -= minutes * 60_000
    secs = ms_total // 1000
    ms = ms_total - secs * 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def normalize_segments(transcription_result):
    if isinstance(transcription_result, dict):
        segments = transcription_result.get("segments", [])
    elif isinstance(transcription_result, list):
        segments = transcription_result
    else:
        segments = []

    normalized = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        normalized.append(
            {
                "start": float(seg.get("start") or 0.0),
                "end": float(seg.get("end") or 0.0),
                "text": text,
            }
        )
    return normalized


def segments_to_plain_text(segments) -> str:
    # Каждую реплику — с новой строки (удобнее читать и диффать)
    return "\n".join(seg["text"] for seg in segments).strip() + "\n"


def segments_to_srt(segments) -> str:
    lines = []
    for i, seg in enumerate(segments, start=1):
        start = seconds_to_srt_time(seg.get("start", 0.0))
        end = seconds_to_srt_time(seg.get("end", 0.0))
        text = (seg.get("text") or "").strip()
        lines.append(str(i))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")  # пустая строка между блоками
    return "\n".join(lines).rstrip() + "\n"


def main():
    parser = argparse.ArgumentParser(
        description="Тестовый скрипт: транскрибация локального аудиофайла (mp3/wav/...) в текст через WhisperX."
    )
    parser.add_argument("audio_path", help="Путь к локальному аудиофайлу (например, .mp3)")
    parser.add_argument(
        "--model",
        default="medium",
        choices=["tiny", "base", "small", "medium", "large"],
        help="Размер модели WhisperX",
    )
    parser.add_argument(
        "--device",
        default=None,
        choices=["cpu", "cuda"],
        help="Устройство для инференса (если не задано — авто)",
    )
    parser.add_argument("--out-txt", default=None, help="Куда сохранить текст (.txt)")
    parser.add_argument("--out-json", default=None, help="Куда сохранить сегменты (.json)")
    parser.add_argument("--out-srt", default=None, help="Куда сохранить субтитры (.srt)")
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="Не печатать текст в консоль (только сохранить файлы)",
    )

    args = parser.parse_args()

    audio_path = Path(args.audio_path).expanduser().resolve()
    if not audio_path.exists():
        raise FileNotFoundError(f"Файл не найден: {audio_path}")

    service = WhisperXService(model_size=args.model, device=args.device)
    transcription_result = service.transcribe_audio(str(audio_path))
    segments = normalize_segments(transcription_result)

    if not segments:
        raise RuntimeError(
            "WhisperX вернул 0 сегментов. Возможные причины: слишком тихо/нет речи/повреждённый файл."
        )

    base_out = audio_path.with_suffix("")

    out_txt = Path(args.out_txt).expanduser().resolve() if args.out_txt else base_out.with_suffix(".txt")
    out_json = Path(args.out_json).expanduser().resolve() if args.out_json else None
    out_srt = Path(args.out_srt).expanduser().resolve() if args.out_srt else None

    plain_text = segments_to_plain_text(segments)

    if not args.no_print:
        print(plain_text, end="")

    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text(plain_text, encoding="utf-8")
    print(f"✅ TXT сохранён: {out_txt}")

    if out_json:
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"✅ JSON сохранён: {out_json}")

    if out_srt:
        out_srt.parent.mkdir(parents=True, exist_ok=True)
        out_srt.write_text(segments_to_srt(segments), encoding="utf-8")
        print(f"✅ SRT сохранён: {out_srt}")


if __name__ == "__main__":
    main()



