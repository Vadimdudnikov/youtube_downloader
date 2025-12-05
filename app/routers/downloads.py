from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
import os

from app.tasks import download_video_task

router = APIRouter()


class DownloadRequest(BaseModel):
    youtube_url: HttpUrl
    audio_only: bool = False


class DownloadResponse(BaseModel):
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


@router.get("/file/{filename}")
async def download_file(filename: str):
    """Скачать загруженный файл"""
    # Ищем файл в папках video и srt
    video_path = os.path.join("assets", "video", filename)
    srt_path = os.path.join("assets", "srt", filename)
    
    file_path = None
    if os.path.exists(video_path):
        file_path = video_path
    elif os.path.exists(srt_path):
        file_path = srt_path
    
    if not file_path:
        raise HTTPException(status_code=404, detail="Файл не найден")
    
    return FileResponse(
        path=file_path,
        filename=filename,
        media_type='application/octet-stream'
    )


@router.get("/list")
async def list_downloads():
    """Получить список загруженных файлов"""
    try:
        files = []
        video_dir = os.path.join("assets", "video")
        srt_dir = os.path.join("assets", "srt")
        
        # Собираем файлы из папки video
        if os.path.exists(video_dir):
            for filename in os.listdir(video_dir):
                file_path = os.path.join(video_dir, filename)
                if os.path.isfile(file_path):
                    file_size = os.path.getsize(file_path)
                    files.append({
                        "filename": filename,
                        "size": file_size,
                        "type": "video" if not filename.endswith('.mp3') else "audio",
                        "download_url": f"/api/v1/download/file/{filename}"
                    })
        
        # Собираем файлы из папки srt
        if os.path.exists(srt_dir):
            for filename in os.listdir(srt_dir):
                file_path = os.path.join(srt_dir, filename)
                if os.path.isfile(file_path):
                    file_size = os.path.getsize(file_path)
                    files.append({
                        "filename": filename,
                        "size": file_size,
                        "type": "srt",
                        "download_url": f"/api/v1/download/file/{filename}"
                    })
        
        return {
            "files": files,
            "total": len(files)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка получения списка: {str(e)}")


