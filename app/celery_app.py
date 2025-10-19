from celery import Celery
import os

# Настройка Celery
celery_app = Celery(
    "youtube_download",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    include=["app.tasks"]
)

# Конфигурация Celery
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=30 * 60,  # 30 минут
    task_soft_time_limit=25 * 60,  # 25 минут
)

# Создание папки assets если её нет
assets_dir = "assets"
if not os.path.exists(assets_dir):
    os.makedirs(assets_dir)
