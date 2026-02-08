from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
import os
from pathlib import Path

from app.tasks import download_video_task, transcribe_audio_task, extract_youtube_id, create_srt_from_youtube_task
from app.config import settings
from typing import Optional

router = APIRouter()


def _get_assets_dir() -> Path:
    """Путь к папке assets. В Docker задайте UPLOAD_DIR=/путь/к/assets (абсолютный)."""
    raw = settings.upload_dir
    if os.path.isabs(raw):
        return Path(raw)
    return Path(__file__).resolve().parent.parent.parent / raw


_ASSETS_DIR = _get_assets_dir()


class DownloadRequest(BaseModel):
    youtube_url: HttpUrl
    audio_only: bool = False


class DownloadResponse(BaseModel):
    task_id: str
    youtube_url: str
    status: str
    message: str


class SRTRequest(BaseModel):
    youtube_url: HttpUrl
    model_size: Optional[str] = "medium"  # tiny, base, small, medium, large


class SRTResponse(BaseModel):
    task_id: str
    youtube_url: str
    status: str
    message: str


@router.post("/download", response_model=DownloadResponse)
async def download_video(request: DownloadRequest):
    """Загрузить видео или аудио с YouTube"""
    try:
        # Отправляем задачу в Celery
        task = download_video_task.delay(str(request.youtube_url), request.audio_only)
        
        download_type = "аудио" if request.audio_only else "видео"
        return DownloadResponse(
            task_id=task.id,
            youtube_url=str(request.youtube_url),
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
    # На случай если query попал в path (прокси/клиент): оставляем только имя файла
    filename = filename.split("?")[0].strip() or filename
    if not filename:
        raise HTTPException(status_code=400, detail="Не указано имя файла")
    video_path = _ASSETS_DIR / "video" / filename
    srt_path = _ASSETS_DIR / "srt" / filename
    nvoice_path = _ASSETS_DIR / "nvoice" / filename
    
    if no_vocals and nvoice_path.exists():
        file_path = nvoice_path
    elif video_path.exists():
        file_path = video_path
    elif srt_path.exists():
        file_path = srt_path
    elif nvoice_path.exists():
        file_path = nvoice_path
    else:
        file_path = None
    
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


@router.post("/srt", response_model=SRTResponse)
async def create_srt(request: SRTRequest):
    """Создать JSON файл с субтитрами для видео с YouTube"""
    try:
        # Валидация размера модели
        valid_models = ["tiny", "base", "small", "medium", "large"]
        if request.model_size not in valid_models:
            raise HTTPException(
                status_code=400,
                detail=f"Неверный размер модели. Доступные: {', '.join(valid_models)}"
            )
        
        # Запускаем задачу в фоне (она сама загрузит аудио и выполнит транскрипцию)
        task = create_srt_from_youtube_task.delay(
            str(request.youtube_url),
            model_size=request.model_size
        )
        
        return SRTResponse(
            task_id=task.id,
            youtube_url=str(request.youtube_url),
            status="pending",
            message="Задача создания JSON файла создана"
        )
    except HTTPException:
        raise
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
            json_file = f"{youtube_id}.json"
            json_path = _ASSETS_DIR / "srt" / json_file
            
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
                'download_url': f"/api/v1/download/file/{json_file}" if json_path.exists() else None
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


