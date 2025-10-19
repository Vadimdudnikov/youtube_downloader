from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
from typing import Optional
import os

from app.tasks import download_video_task, update_proxies_task
from app.proxy_manager import proxy_manager

router = APIRouter()


class DownloadRequest(BaseModel):
    youtube_url: HttpUrl


class DownloadResponse(BaseModel):
    task_id: str
    youtube_url: str
    status: str
    message: str


@router.post("/download", response_model=DownloadResponse)
async def download_video(request: DownloadRequest):
    """Загрузить видео с YouTube"""
    try:
        # Отправляем задачу в Celery
        task = download_video_task.delay(str(request.youtube_url))
        
        return DownloadResponse(
            task_id=task.id,
            youtube_url=str(request.youtube_url),
            status="pending",
            message="Задача загрузки создана"
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
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': task.info.get('status', 'Загружаем...'),
                'progress': task.info.get('progress', 0),
                'title': task.info.get('title'),
                'duration': task.info.get('duration')
            }
        elif task.state == 'SUCCESS':
            result = task.result
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
                'download_url': f"/api/v1/download/file/{result.get('file_name')}"
            }
        else:  # FAILURE
            response = {
                'task_id': task_id,
                'state': task.state,
                'status': 'error',
                'error': str(task.info)
            }
        
        return response
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка получения статуса: {str(e)}")


@router.get("/file/{filename}")
async def download_file(filename: str):
    """Скачать загруженный файл"""
    file_path = os.path.join("assets", filename)
    
    if not os.path.exists(file_path):
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
        if not os.path.exists("assets"):
            return {"files": [], "total": 0}
        
        files = []
        for filename in os.listdir("assets"):
            file_path = os.path.join("assets", filename)
            if os.path.isfile(file_path):
                file_size = os.path.getsize(file_path)
                files.append({
                    "filename": filename,
                    "size": file_size,
                    "download_url": f"/api/v1/download/file/{filename}"
                })
        
        return {
            "files": files,
            "total": len(files)
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка получения списка: {str(e)}")


@router.get("/proxies/status")
async def get_proxy_status():
    """Получить статус прокси"""
    return {
        "working_proxies_count": len(proxy_manager.working_proxies),
        "current_proxy_index": proxy_manager.current_proxy_index,
        "last_update": proxy_manager.last_proxy_update,
        "should_update": proxy_manager.should_update_proxies()
    }


@router.post("/proxies/update")
async def update_proxies():
    """Принудительно обновить список прокси"""
    try:
        task = update_proxies_task.delay()
        return {
            "task_id": task.id,
            "message": "Задача обновления прокси запущена"
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Ошибка обновления прокси: {str(e)}")
