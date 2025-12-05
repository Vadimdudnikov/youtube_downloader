#!/bin/bash

# Скрипт для запуска YouTube Download API и воркеров

set -e  # Остановка при ошибке

# Цвета для вывода
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Функция для вывода с цветом
print_status() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARNING]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Проверяем, что мы в правильной директории
if [ ! -f "app/celery_app.py" ]; then
    print_error "Скрипт должен быть запущен из корневой директории проекта"
    exit 1
fi

# Проверяем наличие Python
if ! command -v python &> /dev/null; then
    print_error "Python не найден. Установите Python 3.8+"
    exit 1
fi

# ✅ Устанавливаем системные зависимости только один раз
if [ ! -f ".deps_installed" ]; then
    print_status "Устанавливаем необходимые системные зависимости (Redis, FFmpeg, cuDNN)..."

    # Проверяем и добавляем репозиторий NVIDIA CUDA при необходимости (только для Linux)
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        if ! dpkg -l | grep -q cuda-keyring 2>/dev/null; then
            print_status "Добавляем репозиторий NVIDIA CUDA..."
            # Определяем версию Ubuntu для правильного репозитория
            UBUNTU_VERSION=$(lsb_release -rs 2>/dev/null || echo "22.04")
            if [ "$UBUNTU_VERSION" = "20.04" ]; then
                CUDA_REPO="ubuntu2004"
            elif [ "$UBUNTU_VERSION" = "22.04" ]; then
                CUDA_REPO="ubuntu2204"
            elif [ "$UBUNTU_VERSION" = "24.04" ]; then
                CUDA_REPO="ubuntu2404"
            else
                CUDA_REPO="ubuntu2204"  # По умолчанию
                print_warning "Неизвестная версия Ubuntu ${UBUNTU_VERSION}, используем ubuntu2204"
            fi

            CUDA_KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/${CUDA_REPO}/x86_64/cuda-keyring_1.1-1_all.deb"
            print_status "Скачиваем CUDA keyring для ${CUDA_REPO}..."
            if wget -q --spider "$CUDA_KEYRING_URL" 2>/dev/null; then
                wget -q "$CUDA_KEYRING_URL" -O /tmp/cuda-keyring.deb
                sudo dpkg -i /tmp/cuda-keyring.deb
                rm -f /tmp/cuda-keyring.deb
                print_success "Репозиторий NVIDIA CUDA добавлен"
            else
                print_warning "Не удалось скачать CUDA keyring, пропускаем..."
            fi
        else
            print_status "Репозиторий NVIDIA CUDA уже настроен"
        fi

        sudo apt-get update

        # Устанавливаем зависимости (пробуем установку с обработкой ошибок)
        set +e  # Временно отключаем остановку при ошибке
        sudo apt-get install -y redis-server ffmpeg libcudnn8 libcudnn8-dev --allow-change-held-packages
        INSTALL_STATUS=$?
        set -e  # Включаем обратно остановку при ошибке

        if [ $INSTALL_STATUS -ne 0 ]; then
            print_warning "Не удалось установить некоторые пакеты, проверяем cuDNN..."
            # Если libcudnn8 не установился, пробуем добавить репозиторий снова
            if ! dpkg -l | grep -q libcudnn8 2>/dev/null; then
                print_status "Пробуем добавить репозиторий CUDA ещё раз..."
                UBUNTU_VERSION=$(lsb_release -rs 2>/dev/null || echo "22.04")
                if [ "$UBUNTU_VERSION" = "20.04" ]; then
                    CUDA_REPO="ubuntu2004"
                elif [ "$UBUNTU_VERSION" = "22.04" ]; then
                    CUDA_REPO="ubuntu2204"
                elif [ "$UBUNTU_VERSION" = "24.04" ]; then
                    CUDA_REPO="ubuntu2404"
                else
                    CUDA_REPO="ubuntu2204"
                fi
                CUDA_KEYRING_URL="https://developer.download.nvidia.com/compute/cuda/repos/${CUDA_REPO}/x86_64/cuda-keyring_1.1-1_all.deb"
                wget -q "$CUDA_KEYRING_URL" -O /tmp/cuda-keyring.deb && sudo dpkg -i /tmp/cuda-keyring.deb && rm -f /tmp/cuda-keyring.deb
                sudo apt-get update
                sudo apt-get install -y libcudnn8 libcudnn8-dev --allow-change-held-packages
            fi
        fi

        echo 'export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH' >> ~/.bashrc
        export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH
    else
        # Для Windows/macOS просто проверяем наличие Redis и FFmpeg
        print_status "Проверяем наличие Redis и FFmpeg..."
        if ! command -v redis-server &> /dev/null; then
            print_warning "Redis не найден. Установите Redis:"
            print_warning "   Windows: choco install redis-64"
            print_warning "   macOS: brew install redis"
        fi
        if ! command -v ffmpeg &> /dev/null; then
            print_warning "FFmpeg не найден. Установите FFmpeg:"
            print_warning "   Windows: choco install ffmpeg"
            print_warning "   macOS: brew install ffmpeg"
        fi
    fi

    touch .deps_installed
    print_success "Зависимости установлены"
else
    print_status "Зависимости уже установлены (пропускаем установку)"
fi

print_status "🚀 Запуск YouTube Download API..."

# Проверяем наличие виртуального окружения
if [ ! -d "venv" ]; then
    print_status "Создаем виртуальное окружение..."
    python -m venv venv
fi

# Активируем виртуальное окружение
print_status "Активируем виртуальное окружение..."
source venv/bin/activate 2>/dev/null || source venv/Scripts/activate 2>/dev/null

# Устанавливаем Python зависимости
print_status "Устанавливаем Python зависимости..."
pip install -r requirements.txt --quiet

# Проверяем наличие .env файла
if [ ! -f ".env" ]; then
    if [ -f "env_example.txt" ]; then
        print_warning "Файл .env не найден. Создаем из примера..."
        cp env_example.txt .env
        print_warning "Отредактируйте файл .env и добавьте ваш API ключ для прокси"
    fi
fi

# Создаём директорию для логов если её нет
mkdir -p logs

# Создаем необходимые директории
print_status "Создаем необходимые директории..."
mkdir -p assets/video
mkdir -p assets/srt
mkdir -p assets/tmp

# Проверяем наличие Redis
print_status "Проверяем Redis..."
if ! redis-cli ping > /dev/null 2>&1; then
    print_warning "Redis не запущен. Запускаем Redis..."
    redis-server --daemonize yes 2>/dev/null || redis-server --service-start 2>/dev/null || true
    sleep 2
    if ! redis-cli ping > /dev/null 2>&1; then
        print_error "Не удалось запустить Redis. Убедитесь, что Redis установлен."
        exit 1
    fi
fi
print_success "Redis работает"

# Функция для запуска воркера
start_worker() {
    local queue_name=$1
    local worker_name=$2
    local log_file="logs/${queue_name}_worker.log"

    print_status "Запускаем воркер ${worker_name} (очередь: ${queue_name})..."

    # Запускаем воркер в фоне
    celery -A app.celery_app:celery_app worker \
        --loglevel=info \
        --queues=${queue_name} \
        --hostname=${worker_name}@%h \
        --concurrency=1 \
        --logfile=${log_file} \
        --pidfile=logs/${queue_name}_worker.pid \
        > /dev/null 2>&1 &

    # Ждем создания PID файла с таймаутом
    local timeout=30
    local count=0
    while [ $count -lt $timeout ]; do
        if [ -f "logs/${queue_name}_worker.pid" ]; then
            local pid=$(cat logs/${queue_name}_worker.pid)
            if kill -0 $pid 2>/dev/null; then
                print_success "Воркер ${worker_name} запущен (PID: ${pid})"
                return 0
            else
                print_warning "PID файл создан, но процесс не найден, ждем..."
            fi
        fi
        sleep 1
        count=$((count + 1))
    done

    # Если PID файл так и не создался
    if [ ! -f "logs/${queue_name}_worker.pid" ]; then
        print_error "Не удалось запустить воркер ${worker_name} (PID файл не создан за $timeout секунд)"
        # Проверяем логи на ошибки
        if [ -f "$log_file" ]; then
            print_error "Последние строки лога:"
            tail -5 "$log_file" | while read line; do
                print_error "  $line"
            done
        fi
        return 1
    fi
}

# Функция для остановки воркера
stop_worker() {
    local queue_name=$1
    local pid_file="logs/${queue_name}_worker.pid"

    if [ -f "$pid_file" ]; then
        local pid=$(cat "$pid_file")
        print_status "Останавливаем воркер ${queue_name} (PID: ${pid})..."
        kill $pid 2>/dev/null || true
        rm -f "$pid_file"
        print_success "Воркер ${queue_name} остановлен"
    fi
}

# Функция для очистки при выходе
cleanup() {
    print_status "Останавливаем все воркеры..."
    stop_worker "youtube_download"
    stop_worker "transcription"

    if [ ! -z "$API_PID" ]; then
        print_status "Останавливаем API (PID: $API_PID)..."
        kill $API_PID 2>/dev/null || true
    fi

    print_success "Все сервисы остановлены"
    exit 0
}

# Устанавливаем обработчик сигналов для корректного завершения
trap cleanup SIGINT SIGTERM

# Запускаем воркеры
print_status "Запускаем воркеры Celery..."
start_worker "youtube_download" "download_worker"
start_worker "transcription" "transcription_worker"

print_success "Все воркеры запущены"

# Запускаем API
print_status "Запускаем FastAPI сервер..."
uvicorn main:app \
    --host 0.0.0.0 \
    --port 3000 \
    --log-level info \
    --access-log \
    > logs/api.log 2>&1 &

API_PID=$!
sleep 2

# Проверяем, что API запустился
if kill -0 $API_PID 2>/dev/null; then
    print_success "API запущен (PID: $API_PID)"
    print_success "API доступен по адресу: http://localhost:3000"
    print_success "Документация API: http://localhost:3000/docs"
else
    print_error "Не удалось запустить API"
    cleanup
    exit 1
fi

print_success "🎉 YouTube Download API полностью запущен!"
print_status "Для остановки нажмите Ctrl+C"
echo ""

# Ждём сигнала завершения
wait
