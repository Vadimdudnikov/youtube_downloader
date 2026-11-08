from fastapi import APIRouter, HTTPException, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl, model_validator
import os
import re
from pathlib import Path

from app.tasks import download_video_task, create_srt_from_youtube_task, create_srt_openai_task, create_srt_elevenlabs_task
from app.config import settings
from typing import Optional

router = APIRouter()

_VIDEO_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{6,32}$")


def _get_assets_dir() -> Path:
    """Путь к папке assets. В Docker задайте UPLOAD_DIR=/путь/к/assets (абсолютный)."""
    raw = settings.upload_dir
    if os.path.isabs(raw):
        return Path(raw)
    return Path(__file__).resolve().parent.parent.parent / raw


_ASSETS_DIR = _get_assets_dir()


def _validate_video_id(video_id: str) -> str:
    video_id = (video_id or "").strip()
    if not _VIDEO_ID_RE.fullmatch(video_id):
        raise HTTPException(
            status_code=400,
            detail="Некорректный video_id. Ожидается 6–32 символа: a-z, A-Z, 0-9, _, -",
        )
    return video_id


class DownloadRequest(BaseModel):
    """youtube_url или url — YouTube либо прямая ссылка на медиа (playprofi и т.п.)."""
    youtube_url: Optional[HttpUrl] = None
    url: Optional[HttpUrl] = None
    audio_only: bool = False

    @model_validator(mode="after")
    def require_media_url(self):
        if not self.youtube_url and not self.url:
            raise ValueError("Укажите youtube_url или url")
        return self

    @property
    def media_url(self) -> str:
        return str(self.youtube_url or self.url)


class DownloadResponse(BaseModel):
    task_id: str
    youtube_url: str
    status: str
    message: str


class SRTRequest(BaseModel):
    youtube_url: Optional[HttpUrl] = None
    url: Optional[HttpUrl] = None
    model_size: Optional[str] = "medium"  # tiny, base, small, medium, large

    @model_validator(mode="after")
    def require_media_url(self):
        if not self.youtube_url and not self.url:
            raise ValueError("Укажите youtube_url или url")
        return self

    @property
    def media_url(self) -> str:
        return str(self.youtube_url or self.url)


class SRTResponse(BaseModel):
    task_id: str
    youtube_url: str
    status: str
    message: str


@router.post("/download", response_model=DownloadResponse)
async def download_video(request: DownloadRequest):
    """Загрузить видео/аудио: YouTube или прямая media-ссылка (например playprofi)."""
    try:
        media_url = request.media_url
        task = download_video_task.delay(media_url, request.audio_only)

        download_type = "аудио" if request.audio_only else "видео"
        return DownloadResponse(
            task_id=task.id,
            youtube_url=media_url,
            status="pending",
            message=f"Задача загрузки {download_type} создана"
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка создания задачи: {str(e)}")


@router.get("/status/{task_id}")
async def get_download_status(task_id: str):
    """Получить статус загрузки по task_id"""
    try:
        task = download_video_task.AsyncResult(task_id)
        
        if task.state == 'PENDING':
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'Ожидание...',
                'progress': 0
            }
        elif task.state == 'PROGRESS':
            # Проверяем, что task.info является словарем
            if isinstance(task.info, dict):
                info = task.info
            else:
                info = {}
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': info.get('status', 'Загружаем...'),
                'progress': info.get('progress', 0),
                'title': info.get('title'),
                'duration': info.get('duration')
            }
        elif task.state == 'SUCCESS':
            # Проверяем, что task.result является словарем
            if isinstance(task.result, dict):
                result = task.result
            else:
                result = {}
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'completed',
                'progress': 100,
                'message': result.get('message'),
                'file_name': result.get('file_name'),
                'file_size': result.get('file_size'),
                'title': result.get('title'),
                'duration': result.get('duration'),
                'download_url': f"/api/v1/download/file/{result.get('file_name')}" if result.get('file_name') else None
            }
        else:  # FAILURE
            # Проверяем, что task.info является словарем
            if isinstance(task.info, dict):
                error_info = task.info
            else:
                # Если task.info это исключение, извлекаем информацию из него
                error_info = {
                    'error': str(task.info) if task.info else 'Неизвестная ошибка',
                    'exc_type': type(task.info).__name__ if task.info else 'Unknown'
                }
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'error',
                'error': error_info.get('error', 'Неизвестная ошибка'),
                'exc_type': error_info.get('exc_type', 'Unknown')
            }
        
        return response
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка получения статуса: {str(e)}")


@router.get("/file/{filename:path}")
async def download_file(
    filename: str,
    no_vocals: Optional[bool] = Query(False, description="Скачать версию без голоса (из nvoice)")
):
    """Скачать загруженный файл (video, srt, nvoice). Для MP3 с тем же именем используйте ?no_vocals=true для инструментала."""
    # Если query попал в path (прокси/клиент: path = "file.mp3?no_vocals=true") — вытаскиваем имя и флаг
    if "?" in filename:
        name_part, query_part = filename.split("?", 1)
        filename = name_part.strip()
        q = query_part.lower()
        if "no_vocals=true" in q or "no_vocals=1" in q:
            no_vocals = True
    else:
        filename = filename.strip()
    if not filename:
        raise HTTPException(status_code=400, detail="Не указано имя файла")
    video_path = _ASSETS_DIR / "video" / filename
    srt_path = _ASSETS_DIR / "srt" / filename
    nvoice_path = _ASSETS_DIR / "nvoice" / filename

    if no_vocals:
        # Только версия без голоса — не отдаём базовый mp3
        file_path = nvoice_path if nvoice_path.exists() else None
    else:
        file_path = (
            video_path if video_path.exists() else
            srt_path if srt_path.exists() else
            nvoice_path if nvoice_path.exists() else
            None
        )
    
    if not file_path:
        paths_checked = [
            str(_ASSETS_DIR / "nvoice" / filename),
            str(_ASSETS_DIR / "video" / filename),
            str(_ASSETS_DIR / "srt" / filename),
        ]
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Файл не найден",
                "filename": filename,
                "assets_dir": str(_ASSETS_DIR),
                "paths_checked": paths_checked,
                "hint": "В Docker задайте переменную окружения UPLOAD_DIR=/путь/к/папке/assets (абсолютный путь, где лежат video, srt, nvoice).",
            },
        )
    
    return FileResponse(
        path=str(file_path),
        filename=filename,
        media_type='application/octet-stream'
    )


@router.get("/list")
async def list_downloads():
    """Получить список загруженных файлов"""
    try:
        files = []
        video_dir = _ASSETS_DIR / "video"
        srt_dir = _ASSETS_DIR / "srt"
        nvoice_dir = _ASSETS_DIR / "nvoice"
        
        # Собираем файлы из папки video
        if video_dir.exists():
            for filename in os.listdir(video_dir):
                file_path = video_dir / filename
                if file_path.is_file():
                    file_size = file_path.stat().st_size
                    files.append({
                        "filename": filename,
                        "size": file_size,
                        "type": "video" if not filename.endswith('.mp3') else "audio",
                        "download_url": f"/api/v1/download/file/{filename}"
                    })
        
        # Собираем файлы из папки srt
        if srt_dir.exists():
            for filename in os.listdir(srt_dir):
                file_path = srt_dir / filename
                if file_path.is_file():
                    file_size = file_path.stat().st_size
                    files.append({
                        "filename": filename,
                        "size": file_size,
                        "type": "json" if filename.endswith('.json') else "srt",
                        "download_url": f"/api/v1/download/file/{filename}"
                    })
        
        # Собираем файлы из папки nvoice (аудио без голоса)
        if nvoice_dir.exists():
            for filename in os.listdir(nvoice_dir):
                file_path = nvoice_dir / filename
                if file_path.is_file():
                    file_size = file_path.stat().st_size
                    files.append({
                        "filename": filename,
                        "size": file_size,
                        "type": "no_vocals",
                        "download_url": f"/api/v1/download/file/{filename}?no_vocals=true"
                    })
        
        return {
            "files": files,
            "total": len(files)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка получения списка: {str(e)}")


class NoVocalsUploadResponse(BaseModel):
    video_id: str
    file_name: str
    file_size: int
    replaced: bool
    message: str
    download_url: str


@router.post("/nvoice", response_model=NoVocalsUploadResponse)
async def upload_no_vocals(
    video_id: str = Form(..., description="ID видео (имя файла без расширения)"),
    file: UploadFile = File(..., description="Аудиофайл без голоса (mp3)"),
):
    """Загрузить no_vocals в assets/nvoice/{video_id}.mp3. Если файл уже есть — заменить."""
    video_id = _validate_video_id(video_id)

    nvoice_dir = _ASSETS_DIR / "nvoice"
    nvoice_dir.mkdir(parents=True, exist_ok=True)

    file_name = f"{video_id}.mp3"
    dest_path = nvoice_dir / file_name
    tmp_path = nvoice_dir / f".{video_id}.mp3.upload"
    replaced = dest_path.exists()
    written = 0

    try:
        with tmp_path.open("wb") as out:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > settings.max_file_size:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Файл слишком большой. Максимум: {settings.max_file_size} байт",
                    )
                out.write(chunk)

        if written == 0:
            raise HTTPException(status_code=400, detail="Пустой файл")

        tmp_path.replace(dest_path)
    except HTTPException:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Ошибка сохранения файла: {str(e)}")
    finally:
        await file.close()

    return NoVocalsUploadResponse(
        video_id=video_id,
        file_name=file_name,
        file_size=written,
        replaced=replaced,
        message="Файл no_vocals заменён" if replaced else "Файл no_vocals загружен",
        download_url=f"/api/v1/file/{file_name}?no_vocals=true",
    )


class DeleteVideoFilesResponse(BaseModel):
    video_id: str
    deleted: list[str]
    total: int
    message: str


@router.delete("/files/{video_id}", response_model=DeleteVideoFilesResponse)
async def delete_video_files(video_id: str):
    """Удалить все файлы, связанные с video_id (video, srt, nvoice)."""
    video_id = _validate_video_id(video_id)

    dirs = (
        _ASSETS_DIR / "video",
        _ASSETS_DIR / "srt",
        _ASSETS_DIR / "nvoice",
    )
    # Типичные расширения + любой другой {video_id}.*
    known_names = {
        f"{video_id}.mp3",
        f"{video_id}.mp4",
        f"{video_id}.wav",
        f"{video_id}.json",
        f"{video_id}.srt",
    }

    deleted: list[str] = []
    try:
        for folder in dirs:
            if not folder.exists():
                continue
            for path in folder.iterdir():
                if not path.is_file():
                    continue
                if path.name in known_names or path.stem == video_id:
                    rel = f"{folder.name}/{path.name}"
                    path.unlink()
                    deleted.append(rel)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка удаления файлов: {str(e)}")

    return DeleteVideoFilesResponse(
        video_id=video_id,
        deleted=deleted,
        total=len(deleted),
        message="Файлы удалены" if deleted else "Файлы не найдены",
    )


def _srt_provider() -> str:
    return (settings.transcription_provider or "").strip().lower()


@router.post("/srt", response_model=SRTResponse)
async def create_srt(request: SRTRequest):
    """Создать субтитры. Провайдер: TRANSCRIPTION_PROVIDER=whisperx|openai|elevenlabs."""
    try:
        media_url = request.media_url
        provider = _srt_provider()

        if provider == "openai":
            task = create_srt_openai_task.delay(media_url)
            return SRTResponse(
                task_id=task.id,
                youtube_url=media_url,
                status="pending",
                message="Задача создания SRT через OpenAI создана"
            )

        if provider == "elevenlabs":
            task = create_srt_elevenlabs_task.delay(media_url)
            return SRTResponse(
                task_id=task.id,
                youtube_url=media_url,
                status="pending",
                message="Задача создания SRT через ElevenLabs создана"
            )

        valid_models = ["tiny", "base", "small", "medium", "large"]
        if request.model_size not in valid_models:
            raise HTTPException(
                status_code=400,
                detail=f"Неверный размер модели. Доступные: {', '.join(valid_models)}"
            )

        task = create_srt_from_youtube_task.delay(
            media_url,
            model_size=request.model_size
        )

        return SRTResponse(
            task_id=task.id,
            youtube_url=media_url,
            status="pending",
            message="Задача создания JSON файла создана"
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка создания задачи: {str(e)}")


@router.post("/srt/openai", response_model=SRTResponse)
async def create_srt_openai(request: SRTRequest):
    """Создать JSON+SRT через OpenAI API (независимо от TRANSCRIPTION_PROVIDER)."""
    try:
        media_url = request.media_url
        task = create_srt_openai_task.delay(media_url)
        return SRTResponse(
            task_id=task.id,
            youtube_url=media_url,
            status="pending",
            message="Задача создания SRT через OpenAI создана"
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка создания задачи: {str(e)}")


@router.get("/srt/status/{task_id}")
async def get_srt_status(task_id: str):
    """Получить статус создания JSON файла по task_id"""
    try:
        task = create_srt_from_youtube_task.AsyncResult(task_id)
        
        if task.state == 'PENDING':
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'Ожидание...',
                'progress': 0
            }
        elif task.state == 'PROGRESS':
            # Проверяем, что task.info является словарем
            if isinstance(task.info, dict):
                info = task.info
            else:
                info = {}
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': info.get('status', 'Обрабатываем...'),
                'progress': info.get('progress', 0)
            }
        elif task.state == 'SUCCESS':
            # Проверяем, что task.result является словарем
            if isinstance(task.result, dict):
                result = task.result
            else:
                result = {}
            
            # Получаем YouTube ID из task_id или из результата
            youtube_id = result.get('youtube_id', task_id)
            json_file = result.get('file_name') or f"{youtube_id}.json"
            srt_file = result.get('srt_file_name') or f"{youtube_id}.srt"
            json_path = _ASSETS_DIR / "srt" / json_file
            srt_path = _ASSETS_DIR / "srt" / srt_file
            
            # Если файл существует, добавляем информацию о нем
            file_size = None
            if json_path.exists():
                file_size = json_path.stat().st_size
            
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'completed',
                'progress': 100,
                'message': result.get('message'),
                'file_name': json_file if json_path.exists() else None,
                'file_size': file_size,
                'segments_count': result.get('segments_count'),
                'download_url': f"/api/v1/download/file/{json_file}" if json_path.exists() else None,
                'srt_file_name': srt_file if srt_path.exists() else None,
                'srt_download_url': f"/api/v1/download/file/{srt_file}" if srt_path.exists() else None,
                'provider': result.get('provider'),
                'model': result.get('model'),
            }
        else:  # FAILURE
            # Проверяем, что task.info является словарем
            if isinstance(task.info, dict):
                error_info = task.info
            else:
                # Если task.info это исключение, извлекаем информацию из него
                error_info = {
                    'error': str(task.info) if task.info else 'Неизвестная ошибка',
                    'exc_type': type(task.info).__name__ if task.info else 'Unknown'
                }
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'error',
                'error': error_info.get('error', 'Неизвестная ошибка'),
                'exc_type': error_info.get('exc_type', 'Unknown')
            }
        
        return response
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка получения статуса: {str(e)}")


@router.get("/srt/openai/status/{task_id}")
async def get_srt_openai_status(task_id: str):
    """Статус задачи OpenAI SRT (тот же формат, что /srt/status)."""
    return await get_srt_status(task_id)


@router.post("/srt/elevenlabs", response_model=SRTResponse)
async def create_srt_elevenlabs(request: SRTRequest):
    """Создать JSON+SRT через ElevenLabs API (независимо от TRANSCRIPTION_PROVIDER)."""
    try:
        media_url = request.media_url
        task = create_srt_elevenlabs_task.delay(media_url)
        return SRTResponse(
            task_id=task.id,
            youtube_url=media_url,
            status="pending",
            message="Задача создания SRT через ElevenLabs создана"
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка создания задачи: {str(e)}")


@router.get("/srt/elevenlabs/status/{task_id}")
async def get_srt_elevenlabs_status(task_id: str):
    """Статус задачи ElevenLabs SRT."""
    return await get_srt_status(task_id)


