# Настройка YouTube Download API

## 🔑 Настройка API ключа для прокси

### Проблема: "Неверный символ в api_key!"

Для работы с прокси нужно получить API ключ с сайта htmlweb.ru:

1. **Зарегистрируйтесь на сайте:** https://htmlweb.ru/
2. **Получите API ключ** в личном кабинете
3. **Настройте .env файл:**

```bash
# Скопируйте пример конфигурации
cp env_example.txt .env

# Отредактируйте .env файл
nano .env
```

4. **Добавьте ваш API ключ:**
```env
PROXY_API_KEY=ваш_реальный_api_ключ_здесь
```

### Пример правильного API ключа:
```env
PROXY_API_KEY=abc123def456ghi789jkl012mno345pqr678stu901vwx234yz
```

## 🍪 Настройка Cookies (рекомендуется)

Для обхода ограничений YouTube:

1. **Скопируйте пример файла cookies:**
```bash
cp cookies.txt.example cookies.txt
```

2. **Получите cookies из браузера:**
   - Откройте YouTube в браузере
   - Войдите в аккаунт
   - Используйте расширение для экспорта cookies (например, "Get cookies.txt")
   - Сохраните в формате Netscape в файл `cookies.txt`

3. **Пример содержимого cookies.txt:**
```
# Netscape HTTP Cookie File
.youtube.com	TRUE	/	FALSE	1234567890	VISITOR_INFO1_LIVE	your_visitor_info
.youtube.com	TRUE	/	FALSE	1234567890	PREF	your_preferences
```

## 🚀 Запуск приложения

После настройки API ключа:

```bash
# Запустите приложение
./start.sh
```

## 🔧 Решение проблем

### YouTube блокирует запросы (HTTP 403)
- ✅ Настройте cookies файл
- ✅ Используйте рабочие прокси
- ✅ Обновите yt-dlp: `pip install --upgrade yt-dlp`

### Прокси не работают
- ✅ Проверьте API ключ
- ✅ Убедитесь что API ключ активен
- ✅ Проверьте баланс на htmlweb.ru

### Ошибки Celery
- ✅ Перезапустите Redis: `redis-server`
- ✅ Перезапустите Celery worker
- ✅ Проверьте логи: `celery -A app.celery_app worker --loglevel=debug`

## 📊 Мониторинг

Проверьте статус системы:

```bash
# Статус прокси
curl http://localhost:8000/api/v1/proxies/status

# Обновить прокси
curl -X POST http://localhost:8000/api/v1/proxies/update

# Список загруженных файлов
curl http://localhost:8000/api/v1/list
```
