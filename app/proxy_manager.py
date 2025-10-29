import asyncio
import aiohttp
import json
import time
import os
from typing import List, Dict, Optional
from app.config import settings
from app.celery_app import celery_app


class ProxyManager:
    def __init__(self):
        self.working_proxies: List[Dict] = []
        self.current_proxy_index = 0
        self.last_proxy_update = 0
        self.proxy_storage_file = settings.proxy_storage_file
        
    async def get_proxies_from_api(self) -> List[Dict]:
        """Получаем список прокси с webshare.io API"""
        try:
            headers = {"Authorization": f"Token {settings.proxy_api_key}"}
            params = settings.proxy_api_params.copy()
            
            print(f"[PROXY DEBUG] Запрашиваем URL: {settings.proxy_api_url}")
            print(f"[PROXY DEBUG] Параметры: {params}")
            print(f"[PROXY DEBUG] API ключ: '{settings.proxy_api_key}'")
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    settings.proxy_api_url, 
                    headers=headers,
                    params=params,
                    timeout=10
                ) as response:
                    print(f"[PROXY DEBUG] Статус ответа: {response.status}")
                    
                    if response.status == 200:
                        data = await response.json()
                        print(f"[PROXY DEBUG] Получен ответ от API")
                        print(f"[PROXY DEBUG] Полный ответ API: {json.dumps(data, indent=2, ensure_ascii=False)}")
                        print(f"[PROXY DEBUG] Ключи в ответе: {list(data.keys())}")
                        
                        if 'error' in data:
                            print(f"[PROXY DEBUG] Ошибка API прокси: {data['error']}")
                            return []
                        
                        # Парсим ответ webshare.io API
                        proxies = []
                        results = data.get('results', [])
                        
                        for proxy_data in results:
                            proxy = {
                                'ip': proxy_data.get('proxy_address'),
                                'port': proxy_data.get('port'),
                                'username': proxy_data.get('username'),
                                'password': proxy_data.get('password'),
                                'country': proxy_data.get('country_code', 'US'),
                                'city': proxy_data.get('city'),
                                'isp': proxy_data.get('isp'),
                                'last_checked': proxy_data.get('last_checked'),
                                'valid': proxy_data.get('valid', True)
                            }
                            
                            # Добавляем только валидные прокси
                            if proxy['valid'] and proxy['ip'] and proxy['port']:
                                proxies.append(proxy)
                        
                        print(f"Получено {len(proxies)} прокси с webshare.io API")
                        return proxies
                    else:
                        print(f"[PROXY DEBUG] Ошибка HTTP {response.status}")
                        try:
                            error_data = await response.json()
                            print(f"[PROXY DEBUG] JSON ответ при ошибке: {json.dumps(error_data, indent=2, ensure_ascii=False)}")
                        except:
                            # Если не JSON, читаем как текст
                            response_text = await response.text()
                            print(f"[PROXY DEBUG] Полный текст ответа при ошибке: {response_text}")
                        return []
        except Exception as e:
            print(f"[PROXY DEBUG] Исключение при запросе прокси: {e}")
            import traceback
            print(f"[PROXY DEBUG] Traceback: {traceback.format_exc()}")
            return []
    
    def save_proxies_to_file(self, proxies: List[Dict]):
        """Сохраняем прокси в файл"""
        try:
            proxy_data = {
                'proxies': proxies,
                'saved_at': time.time(),
                'count': len(proxies)
            }
            
            with open(self.proxy_storage_file, 'w', encoding='utf-8') as f:
                json.dump(proxy_data, f, ensure_ascii=False, indent=2)
            
            print(f"Сохранено {len(proxies)} прокси в файл {self.proxy_storage_file}")
        except Exception as e:
            print(f"Ошибка при сохранении прокси в файл: {e}")
    
    def load_proxies_from_file(self) -> List[Dict]:
        """Загружаем прокси из файла"""
        try:
            if not os.path.exists(self.proxy_storage_file):
                print(f"Файл прокси {self.proxy_storage_file} не найден")
                return []
            
            with open(self.proxy_storage_file, 'r', encoding='utf-8') as f:
                proxy_data = json.load(f)
            
            proxies = proxy_data.get('proxies', [])
            saved_at = proxy_data.get('saved_at', 0)
            
            # Проверяем, не устарели ли прокси (старше 24 часов)
            if time.time() - saved_at > 24 * 3600:
                print("Сохранённые прокси устарели (старше 24 часов)")
                return []
            
            print(f"Загружено {len(proxies)} прокси из файла")
            return proxies
        except Exception as e:
            print(f"Ошибка при загрузке прокси из файла: {e}")
            return []
    
    async def check_proxy(self, proxy: Dict) -> bool:
        """Проверяем работоспособность прокси"""
        try:
            proxy_url = f"http://{proxy['ip']}:{proxy['port']}"
            proxy_auth = None
            
            if proxy.get('username') and proxy.get('password'):
                proxy_auth = aiohttp.BasicAuth(proxy['username'], proxy['password'])
            
            connector = aiohttp.TCPConnector()
            timeout = aiohttp.ClientTimeout(total=settings.proxy_check_timeout)
            
            async with aiohttp.ClientSession(
                connector=connector,
                timeout=timeout
            ) as session:
                async with session.get(
                    settings.proxy_check_url,
                    proxy=proxy_url,
                    proxy_auth=proxy_auth
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        print(f"Прокси {proxy['ip']}:{proxy['port']} работает. IP: {result.get('origin', 'unknown')}")
                        return True
                    return False
        except Exception as e:
            print(f"Прокси {proxy.get('ip', 'unknown')}:{proxy.get('port', 'unknown')} не работает: {e}")
            return False
    
    async def check_all_proxies(self, proxies: List[Dict]) -> List[Dict]:
        """Проверяем все прокси параллельно"""
        print(f"Проверяем {len(proxies)} прокси...")
        
        tasks = [self.check_proxy(proxy) for proxy in proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        working_proxies = []
        for i, result in enumerate(results):
            if result is True:
                working_proxies.append(proxies[i])
        
        print(f"Найдено {len(working_proxies)} рабочих прокси из {len(proxies)}")
        return working_proxies
    
    async def update_working_proxies(self):
        """Обновляем список рабочих прокси"""
        print("Обновляем список прокси...")
        
        # Сначала пытаемся загрузить сохранённые прокси
        saved_proxies = self.load_proxies_from_file()
        if saved_proxies:
            print(f"Используем {len(saved_proxies)} сохранённых прокси")
            self.working_proxies = saved_proxies
            self.current_proxy_index = 0
            self.last_proxy_update = time.time()
            return
        
        # Если сохранённых прокси нет, получаем новые с API
        print(f"[PROXY] Запрашиваем прокси с API...")
        print(f"[PROXY DEBUG] Настройки из config: API ключ='{settings.proxy_api_key[:10]}...', URL={settings.proxy_api_url}")
        proxies = await self.get_proxies_from_api()
        
        print(f"[PROXY] Получено с API: {len(proxies)} прокси")
        
        if not proxies:
            print("[PROXY ERROR] Не удалось получить прокси с API")
            return
        
        # Проверяем полученные прокси
        print(f"[PROXY] Начинаем проверку {len(proxies)} прокси на работоспособность...")
        working_proxies = await self.check_all_proxies(proxies)
        
        print(f"[PROXY] Результаты проверки: {len(working_proxies)} рабочих из {len(proxies)} проверенных")
        
        if working_proxies:
            # Сохраняем рабочие прокси в файл
            self.save_proxies_to_file(working_proxies)
            self.working_proxies = working_proxies
            self.current_proxy_index = 0
            self.last_proxy_update = time.time()
            print(f"[PROXY] Обновлено: {len(self.working_proxies)} рабочих прокси сохранено и готово к использованию")
        else:
            print("[PROXY ERROR] Не найдено рабочих прокси после проверки")
    
    def get_next_proxy(self) -> Optional[Dict]:
        """Получаем следующий рабочий прокси"""
        if not self.working_proxies:
            print(f"[PROXY] get_next_proxy: список прокси пуст (всего прокси: 0)")
            return None
        
        # Проверяем, что индекс валиден (на случай если список изменился)
        if self.current_proxy_index >= len(self.working_proxies):
            print(f"[PROXY] get_next_proxy: индекс {self.current_proxy_index} превышает длину списка {len(self.working_proxies)}, сбрасываем на 0")
            self.current_proxy_index = 0
        
        proxy = self.working_proxies[self.current_proxy_index]
        print(f"[PROXY] get_next_proxy: возвращаем прокси #{self.current_proxy_index} из {len(self.working_proxies)} (IP: {proxy.get('ip')}:{proxy.get('port')})")
        self.current_proxy_index = (self.current_proxy_index + 1) % len(self.working_proxies)
        return proxy
    
    def mark_proxy_failed(self, proxy: Dict):
        """Помечаем прокси как нерабочий и удаляем из списка"""
        if proxy in self.working_proxies:
            self.working_proxies.remove(proxy)
            print(f"Прокси {proxy.get('ip')}:{proxy.get('port')} помечен как нерабочий")
            
            # Обновляем сохранённый файл
            if self.working_proxies:
                self.save_proxies_to_file(self.working_proxies)
            
            # Если прокси закончились, обновляем список
            if not self.working_proxies:
                print("Все прокси закончились, обновляем список...")
                # Запускаем обновление асинхронно
                asyncio.create_task(self.update_working_proxies())
    
    def should_update_proxies(self) -> bool:
        """Проверяем, нужно ли обновить прокси"""
        return len(self.working_proxies) == 0
    
    def get_proxy_for_ytdlp(self) -> Optional[str]:
        """Получаем прокси в формате для yt-dlp"""
        proxy = self.get_next_proxy()
        if not proxy:
            return None
        
        # webshare.io прокси всегда требуют аутентификацию
        if proxy.get('username') and proxy.get('password'):
            return f"http://{proxy['username']}:{proxy['password']}@{proxy['ip']}:{proxy['port']}"
        else:
            return f"http://{proxy['ip']}:{proxy['port']}"


# Глобальный экземпляр менеджера прокси
proxy_manager = ProxyManager()
