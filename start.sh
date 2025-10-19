#!/bin/bash

# YouTube Download API - Скрипт запуска
# Автор: Assistant
# Описание: Запускает Redis, Celery worker и FastAPI приложение

echo "🚀 Запуск YouTube Download API..."

# Проверяем наличие Python
if ! command -v python &> /dev/null; then
    echo "❌ Python не найден. Установите Python 3.8+"
    exit 1
fi

# Проверяем наличие Redis
if ! command -v redis-server &> /dev/null; then
    echo "❌ Redis не найден. Установите Redis:"
    echo "   Windows: choco install redis-64"
    echo "   Ubuntu: sudo apt install redis-server"
    echo "   macOS: brew install redis"
    exit 1
fi

# Проверяем наличие виртуального окружения
if [ ! -d "venv_download" ]; then
    echo "📦 Создаем виртуальное окружение..."
    python -m venv venv_download
fi

# Активируем виртуальное окружение
echo "🔧 Активируем виртуальное окружение..."
source venv_download/bin/activate 2>/dev/null || venv_download\Scripts\activate 2>/dev/null

# Устанавливаем зависимости
echo "📥 Устанавливаем зависимости..."
pip install -r requirements.txt

# Проверяем наличие .env файла
if [ ! -f ".env" ]; then
    echo "⚠️  Файл .env не найден. Создаем из примера..."
    cp env_example.txt .env
    echo "📝 Отредактируйте файл .env и добавьте ваш API ключ для прокси"
    echo "   PROXY_API_KEY=ваш_api_ключ_здесь"
fi

# Создаем папку assets если её нет
if [ ! -d "assets" ]; then
    echo "📁 Создаем папку assets..."
    mkdir assets
fi

# Проверяем запущен ли Redis
echo "🔍 Проверяем Redis..."
if ! redis-cli ping &> /dev/null; then
    echo "🔄 Запускаем Redis..."
    redis-server --daemonize yes
    sleep 2
    
    # Проверяем что Redis запустился
    if ! redis-cli ping &> /dev/null; then
        echo "❌ Не удалось запустить Redis"
        exit 1
    fi
    echo "✅ Redis запущен"
else
    echo "✅ Redis уже запущен"
fi

# Функция для остановки всех процессов
cleanup() {
    echo ""
    echo "🛑 Останавливаем все процессы..."
    kill $CELERY_PID 2>/dev/null
    kill $FASTAPI_PID 2>/dev/null
    echo "✅ Все процессы остановлены"
    exit 0
}

# Устанавливаем обработчик сигналов
trap cleanup SIGINT SIGTERM

# Запускаем Celery worker в фоне
echo "🔄 Запускаем Celery worker..."
celery -A app.celery_app worker --loglevel=info &
CELERY_PID=$!

# Ждем немного чтобы Celery запустился
sleep 3

# Запускаем FastAPI приложение в фоне
echo "🔄 Запускаем FastAPI приложение..."
python main.py &
FASTAPI_PID=$!

# Ждем немного чтобы FastAPI запустился
sleep 3

echo ""
echo "🎉 YouTube Download API запущен!"
echo ""
echo "📊 Статус сервисов:"
echo "   • Redis: ✅ Запущен"
echo "   • Celery Worker: ✅ Запущен (PID: $CELERY_PID)"
echo "   • FastAPI: ✅ Запущен (PID: $FASTAPI_PID)"
echo ""
echo "🌐 Доступные URL:"
echo "   • API: http://localhost:8000"
echo "   • Документация: http://localhost:8000/docs"
echo "   • Альтернативная документация: http://localhost:8000/redoc"
echo ""
echo "📋 Полезные команды:"
echo "   • Проверить статус прокси: curl http://localhost:8000/api/v1/proxies/status"
echo "   • Обновить прокси: curl -X POST http://localhost:8000/api/v1/proxies/update"
echo "   • Список файлов: curl http://localhost:8000/api/v1/list"
echo ""
echo "⏹️  Для остановки нажмите Ctrl+C"
echo ""

# Ждем завершения процессов
wait $CELERY_PID $FASTAPI_PID
