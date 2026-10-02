#!/bin/bash

# CPU-режим: без CUDA/cuDNN/WhisperX. SRT через API, demucs на CPU.
#
# Runtime (venv, логи, PID) → /home/youtube_download
# Код и хранилище файлов   → /workspace (внешний volume)

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

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

# Каталог со скриптом = workspace (код + assets)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_DIR="${WORKSPACE_DIR:-$SCRIPT_DIR}"

# Runtime вне volume
RUNTIME_DIR="${RUNTIME_DIR:-/home/youtube_download}"
VENV_DIR="${RUNTIME_DIR}/venv"
LOG_DIR="${RUNTIME_DIR}/logs"
DEPS_MARKER="${RUNTIME_DIR}/.deps_installed_cpu"
ASSETS_DIR="${WORKSPACE_DIR}/assets"

if [ ! -f "${WORKSPACE_DIR}/app/celery_app.py" ]; then
    print_error "Не найден проект в ${WORKSPACE_DIR}"
    exit 1
fi

if ! command -v python3 &> /dev/null && ! command -v python &> /dev/null; then
    print_error "Python не найден. Установите Python 3.8+"
    exit 1
fi

PYTHON_BIN="$(command -v python3 || command -v python)"

print_status "Режим CPU: CUDA/cuDNN не устанавливаем, demucs → CPU, SRT через API"
print_status "Workspace (код/файлы): ${WORKSPACE_DIR}"
print_status "Runtime (venv/логи):   ${RUNTIME_DIR}"

mkdir -p "${RUNTIME_DIR}" "${LOG_DIR}"

# Системные зависимости без NVIDIA/CUDA
if [ ! -f "${DEPS_MARKER}" ]; then
    print_status "Устанавливаем системные зависимости (Redis, FFmpeg)..."

    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        apt-get update
        apt-get install -y redis-server ffmpeg
    else
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

    touch "${DEPS_MARKER}"
    print_success "Зависимости установлены (CPU)"
else
    print_status "CPU-зависимости уже установлены (пропускаем)"
fi

print_status "🚀 Запуск YouTube Download API (CPU)..."

if [ ! -d "${VENV_DIR}" ]; then
    print_status "Создаем виртуальное окружение в ${VENV_DIR}..."
    "${PYTHON_BIN}" -m venv "${VENV_DIR}"
fi

print_status "Активируем виртуальное окружение..."
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

print_status "Устанавливаем Python зависимости (requirements-cpu.txt)..."
pip install -r "${WORKSPACE_DIR}/requirements-cpu.txt" --quiet
# На случай, если в venv раньше ставили CUDA-сборку torch
pip install --force-reinstall --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    "torch==2.6.0" "torchaudio==2.6.0" --quiet

cd "${WORKSPACE_DIR}"

if [ ! -f "${WORKSPACE_DIR}/.env" ]; then
    if [ -f "${WORKSPACE_DIR}/env_example.txt" ]; then
        print_warning "Файл .env не найден. Создаем из примера..."
        cp "${WORKSPACE_DIR}/env_example.txt" "${WORKSPACE_DIR}/.env"
        print_warning "Отредактируйте .env: TRANSCRIPTION_PROVIDER=openai|elevenlabs и API ключи"
    fi
fi

if grep -Eqi '^[[:space:]]*TRANSCRIPTION_PROVIDER[[:space:]]*=[[:space:]]*whisperx' "${WORKSPACE_DIR}/.env" 2>/dev/null; then
    print_warning "TRANSCRIPTION_PROVIDER=whisperx: в CPU-режиме WhisperX не установлен."
    print_warning "Используйте openai или elevenlabs в .env"
fi

# Файлы только на volume; runtime — в /home
export UPLOAD_DIR="${ASSETS_DIR}"
export TMP_DIR="${ASSETS_DIR}/tmp"
export PYTHONPATH="${WORKSPACE_DIR}${PYTHONPATH:+:$PYTHONPATH}"

print_status "Создаем директории хранения в ${ASSETS_DIR}..."
mkdir -p "${ASSETS_DIR}/video" "${ASSETS_DIR}/srt" "${ASSETS_DIR}/nvoice" "${ASSETS_DIR}/tmp"

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

start_worker() {
    local queue_name=$1
    local worker_name=$2
    local log_file="${LOG_DIR}/${queue_name}_worker.log"
    local pid_file="${LOG_DIR}/${queue_name}_worker.pid"

    print_status "Запускаем воркер ${worker_name} (очередь: ${queue_name})..."

    cd "${WORKSPACE_DIR}"
    celery -A app.celery_app:celery_app worker \
        --loglevel=info \
        --queues="${queue_name}" \
        --hostname="${worker_name}@%h" \
        --concurrency=1 \
        --logfile="${log_file}" \
        --pidfile="${pid_file}" \
        > /dev/null 2>&1 &

    local timeout=30
    local count=0
    while [ $count -lt $timeout ]; do
        if [ -f "${pid_file}" ]; then
            local pid
            pid=$(cat "${pid_file}")
            if kill -0 "$pid" 2>/dev/null; then
                print_success "Воркер ${worker_name} запущен (PID: ${pid})"
                return 0
            else
                print_warning "PID файл создан, но процесс не найден, ждем..."
            fi
        fi
        sleep 1
        count=$((count + 1))
    done

    if [ ! -f "${pid_file}" ]; then
        print_error "Не удалось запустить воркер ${worker_name} (PID файл не создан за $timeout секунд)"
        if [ -f "${log_file}" ]; then
            print_error "Последние строки лога:"
            tail -5 "${log_file}" | while read -r line; do
                print_error "  $line"
            done
        fi
        return 1
    fi
}

stop_worker() {
    local queue_name=$1
    local pid_file="${LOG_DIR}/${queue_name}_worker.pid"

    if [ -f "${pid_file}" ]; then
        local pid
        pid=$(cat "${pid_file}")
        print_status "Останавливаем воркер ${queue_name} (PID: ${pid})..."
        kill "$pid" 2>/dev/null || true
        rm -f "${pid_file}"
        print_success "Воркер ${queue_name} остановлен"
    fi
}

cleanup() {
    print_status "Останавливаем все воркеры..."
    stop_worker "youtube_download"
    stop_worker "transcription"
    stop_worker "no_vocals"

    if [ -n "${API_PID:-}" ]; then
        print_status "Останавливаем API (PID: $API_PID)..."
        kill "$API_PID" 2>/dev/null || true
    fi

    print_success "Все сервисы остановлены"
    exit 0
}

trap cleanup SIGINT SIGTERM

print_status "Запускаем воркеры Celery..."
start_worker "youtube_download" "download_worker"
start_worker "transcription" "transcription_worker"
start_worker "no_vocals" "no_vocals_worker"

print_success "Все воркеры запущены"

print_status "Запускаем FastAPI сервер..."
cd "${WORKSPACE_DIR}"
uvicorn main:app \
    --host 0.0.0.0 \
    --port 3000 \
    --log-level info \
    --access-log \
    > "${LOG_DIR}/api.log" 2>&1 &
API_PID=$!
echo "${API_PID}" > "${LOG_DIR}/api.pid"
sleep 2

if kill -0 "$API_PID" 2>/dev/null; then
    print_success "API запущен (PID: $API_PID)"
    print_success "API доступен по адресу: http://localhost:3000"
    print_success "Документация API: http://localhost:3000/docs"
    print_status "Логи: ${LOG_DIR}"
    print_status "Файлы: ${ASSETS_DIR}"
else
    print_error "Не удалось запустить API"
    if [ -f "${LOG_DIR}/api.log" ]; then
        tail -20 "${LOG_DIR}/api.log"
    fi
    cleanup
    exit 1
fi

print_success "🎉 YouTube Download API запущен в CPU-режиме!"
print_status "Для остановки нажмите Ctrl+C"
echo ""

wait "$API_PID"
