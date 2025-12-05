#!/bin/bash

# YouTube Download API - Скрипт остановки
# Описание: Останавливает все процессы (Redis, Celery workers, FastAPI)

echo "🛑 Останавливаем YouTube Download API..."

# Функция для остановки процессов по имени
stop_processes() {
    local process_name=$1
    local pids=$(pgrep -f "$process_name")
    
    if [ -z "$pids" ]; then
        echo "   • $process_name: не запущен"
        return 0
    fi
    
    echo "   • $process_name: найдено процессов: $(echo $pids | wc -w)"
    for pid in $pids; do
        echo "     Останавливаем процесс PID: $pid"
        kill $pid 2>/dev/null
    done
    
    # Ждем немного и проверяем
    sleep 2
    
    # Если процессы еще живы, убиваем принудительно
    remaining_pids=$(pgrep -f "$process_name")
    if [ ! -z "$remaining_pids" ]; then
        echo "     Принудительная остановка процессов..."
        for pid in $remaining_pids; do
            kill -9 $pid 2>/dev/null
        done
    fi
    
    echo "   ✅ $process_name: остановлен"
}

# Останавливаем Celery workers
echo "🔄 Останавливаем Celery workers..."
stop_processes "celery.*worker"
stop_processes "celery.*app.celery_app"

# Останавливаем FastAPI (uvicorn)
echo "🔄 Останавливаем FastAPI..."
stop_processes "uvicorn.*main:app"

# Останавливаем Redis (опционально, только если запущен нашим скриптом)
echo "🔄 Проверяем Redis..."
redis_pid=$(pgrep -f "redis-server.*daemonize")
if [ ! -z "$redis_pid" ]; then
    echo "   • Redis: найден процесс PID: $redis_pid"
    echo "   ⚠️  Redis запущен в daemon режиме. Останавливаем..."
    kill $redis_pid 2>/dev/null
    sleep 1
    if pgrep -f "redis-server.*daemonize" > /dev/null; then
        kill -9 $redis_pid 2>/dev/null
    fi
    echo "   ✅ Redis: остановлен"
else
    echo "   • Redis: не запущен нашим скриптом (может быть запущен системно)"
fi

# Дополнительная очистка - убиваем все процессы python связанные с проектом
echo "🔄 Очищаем оставшиеся процессы Python..."
project_pids=$(pgrep -f "python.*youtube_download|python.*main.py|python.*celery_worker")
if [ ! -z "$project_pids" ]; then
    echo "   • Найдено процессов Python: $(echo $project_pids | wc -w)"
    for pid in $project_pids; do
        echo "     Останавливаем процесс PID: $pid"
        kill $pid 2>/dev/null
    done
    sleep 1
    # Принудительная остановка если нужно
    remaining=$(pgrep -f "python.*youtube_download|python.*main.py|python.*celery_worker")
    if [ ! -z "$remaining" ]; then
        for pid in $remaining; do
            kill -9 $pid 2>/dev/null
        done
    fi
    echo "   ✅ Процессы Python: остановлены"
else
    echo "   • Процессы Python: не найдены"
fi

echo ""
echo "✅ Все процессы остановлены!"
echo ""
echo "📊 Статус процессов:"
echo "   • Celery Workers: $(pgrep -f 'celery.*worker' | wc -l) процессов"
echo "   • FastAPI (uvicorn): $(pgrep -f 'uvicorn.*main:app' | wc -l) процессов"
echo "   • Redis: $(pgrep -f 'redis-server' | wc -l) процессов"
echo ""

