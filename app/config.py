import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Основные настройки приложения
    app_name: str = "YouTube Download API"
    debug: bool = False
    
    # Настройки прокси
    proxy_api_key: str = "06b260fa67f492ccce35bef63474e800"  # Ваш API ключ
    proxy_api_url: str = "http://htmlweb.ru/json/proxy/get?country=RU&perpage=100&api_key="
    
    # Настройки файлов
    upload_dir: str = "assets"
    cookies_file: str = "cookies.txt"  # Путь к файлу cookies
    max_file_size: int = 100 * 1024 * 1024  # 100MB
    
    # Настройки прокси
    proxy_check_timeout: int = 5  # Таймаут проверки прокси в секундах
    proxy_check_url: str = "https://httpbin.org/ip"  # URL для проверки прокси
    
    class Config:
        case_sensitive = False


settings = Settings()
