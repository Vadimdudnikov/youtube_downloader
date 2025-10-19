#!/usr/bin/env python3
"""
Запуск Celery worker для обработки задач загрузки видео
"""

from app.celery_app import celery_app

if __name__ == '__main__':
    celery_app.start()
