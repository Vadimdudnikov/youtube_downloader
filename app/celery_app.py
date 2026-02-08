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
    task_default_queue='youtube_download',
    task_queues={
        'youtube_download': {
            'exchange': 'youtube_download',
            'routing_key': 'youtube_download',
        },
        'transcription': {
            'exchange': 'transcription',
            'routing_key': 'transcription',
        },
        'no_vocals': {
            'exchange': 'no_vocals',
            'routing_key': 'no_vocals',
        },
    },
    task_routes={
        'app.tasks.transcribe_audio_task': {'queue': 'transcription'},
        'app.tasks.create_srt_from_youtube_task': {'queue': 'transcription'},
        'app.tasks.create_no_vocals_task': {'queue': 'no_vocals'},
    },
)

# Создание папок assets и подпапок если их нет
assets_dir = "assets"
video_dir = os.path.join(assets_dir, "video")
srt_dir = os.path.join(assets_dir, "srt")
nvoice_dir = os.path.join(assets_dir, "nvoice")

if not os.path.exists(assets_dir):
    os.makedirs(assets_dir)
if not os.path.exists(video_dir):
    os.makedirs(video_dir)
if not os.path.exists(srt_dir):
    os.makedirs(srt_dir)
if not os.path.exists(nvoice_dir):
    os.makedirs(nvoice_dir)
