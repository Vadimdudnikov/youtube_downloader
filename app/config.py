import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Основные настройки приложения
    app_name: str = "YouTube Download API"
    debug: bool = False
    
    # Настройки прокси webshare.io
    proxy_api_key: str = "wtrym7y6d7pc0yzz77cbbgb6okmyr8sbyg1vzgmo"  # Замените на ваш API ключ от webshare.io
    proxy_api_url: str = "https://proxy.webshare.io/api/v2/proxy/list/"
    proxy_api_params: dict = {
        "mode": "direct",
        "page": 1,
        "page_size": 25
    }
    
    # Настройки файлов
    upload_dir: str = "assets"
    cookies_file: str = "cookies.txt"  # Путь к файлу cookies
    max_file_size: int = 100 * 1024 * 1024  # 100MB
    proxy_storage_file: str = "proxies.json"  # Файл для сохранения прокси
    
    # Настройки прокси
    proxy_check_timeout: int = 5  # Таймаут проверки прокси в секундах
    proxy_check_url: str = "https://httpbin.org/ip"  # URL для проверки прокси
    
    class Config:
        case_sensitive = False


settings = Settings()
