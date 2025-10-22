# YouTube Download API

Простое FastAPI приложение для загрузки видео с YouTube с использованием Celery для асинхронной обработки, поддержкой прокси и cookies.

## Особенности

- ✅ Асинхронная загрузка через Celery
- ✅ Автоматическая проверка и ротация прокси
- ✅ Поддержка cookies для обхода ограничений
- ✅ Мониторинг статуса загрузки в реальном времени
- ✅ Автоматическое обновление списка прокси

## Установка

1. Установите зависимости:
```bash
pip install -r requirements.txt
```

2. Установите и запустите Redis:
```bash
# Windows (с Chocolatey)
choco install redis-64

# Или скачайте с https://redis.io/download
```

3. Запустите Redis сервер:
```bash
redis-server
```

4. Настройте конфигурацию:
```bash
# Скопируйте пример конфигурации
cp env_example.txt .env

# Отредактируйте .env файл и добавьте ваш API ключ для прокси
PROXY_API_KEY=ваш_api_ключ_здесь
```

5. (Опционально) Настройте cookies:
```bash
# Скопируйте пример файла cookies
cp cookies.txt.example cookies.txt

# Добавьте ваши cookies в файл cookies.txt
```

## Запуск

1. Запустите Celery worker в отдельном терминале:
```bash
celery -A app.celery_app worker --loglevel=info
```

2. Запустите FastAPI приложение:
```bash
python main.py
```

Приложение будет доступно по адресу: http://localhost:8000

## API Endpoints

### POST /api/v1/download
Запустить загрузку видео с YouTube
```json
{
  "youtube_url": "https://www.youtube.com/watch?v=VIDEO_ID"
}
```

Ответ:
```json
{
  "task_id": "uuid-task-id",
  "youtube_url": "https://www.youtube.com/watch?v=VIDEO_ID",
  "status": "pending",
  "message": "Задача загрузки создана"
}
```

### GET /api/v1/status/{task_id}
Получить статус загрузки

Ответы:
- **PENDING**: Задача в очереди
- **PROGRESS**: Загрузка в процессе (с прогрессом)
- **SUCCESS**: Загрузка завершена (с ссылкой на файл)
- **FAILURE**: Ошибка загрузки

### GET /api/v1/file/{filename}
Скачать загруженный файл

### GET /api/v1/list
Получить список всех загруженных файлов

### GET /api/v1/proxies/status
Получить статус прокси (количество рабочих, последнее обновление)

### POST /api/v1/proxies/update
Принудительно обновить список прокси

## Структура проекта

```
youtube_download/
├── main.py                 # FastAPI приложение
├── celery_worker.py        # Запуск Celery worker
├── requirements.txt        # Зависимости
├── env_example.txt         # Пример конфигурации
├── cookies.txt.example     # Пример файла cookies
├── assets/                 # Папка для загруженных файлов
└── app/
    ├── __init__.py
    ├── config.py           # Конфигурация приложения
    ├── celery_app.py       # Настройка Celery
    ├── proxy_manager.py    # Менеджер прокси
    ├── tasks.py            # Celery задачи
    └── routers/
        ├── __init__.py
        └── downloads.py    # API роутеры
```

## Настройка прокси

Приложение поддерживает интеграцию с webshare.io API:

### Webshare.io (рекомендуется)
1. Получите API ключ на [webshare.io](https://webshare.io/)
2. Скопируйте пример конфигурации: `cp env_webshare_example.txt .env`
3. Укажите ваш API ключ в файле `.env`:
   ```
   PROXY_API_KEY=ваш_webshare_api_ключ
   ```

### Логика работы с прокси
Приложение автоматически:
1. Получает список прокси с webshare.io API
2. Сохраняет их в файл `proxies.json` для повторного использования
3. Проверяет их работоспособность параллельно
4. Использует рабочие прокси по очереди
5. Помечает нерабочие прокси и удаляет их из списка
6. Автоматически обновляет список только когда все прокси перестают работать
7. Прокси сохраняются на 24 часа, после чего обновляются с API

## Использование

1. Отправьте POST запрос на `/api/v1/download` с URL видео
2. Получите `task_id` в ответе
3. Используйте `task_id` для проверки статуса через `/api/v1/status/{task_id}`
4. Когда статус станет `SUCCESS`, используйте `download_url` для скачивания файла

## Тестирование

Для проверки интеграции с webshare.io API запустите тестовый скрипт:

```bash
python test_proxy_integration.py
```

Скрипт проверит:
- Получение прокси с API
- Сохранение и загрузку прокси из файла
- Проверку работоспособности прокси
- Ротацию прокси
- Формат прокси для yt-dlp

## Мониторинг

- Проверяйте статус прокси через `/api/v1/proxies/status`
- Принудительно обновляйте прокси через `/api/v1/proxies/update`
- Следите за логами Celery worker для отладки
