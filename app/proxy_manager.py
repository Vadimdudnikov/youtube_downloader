import asyncio
import aiohttp
import json
import time
from typing import List, Dict, Optional
from app.config import settings
from app.celery_app import celery_app


class ProxyManager:
    def __init__(self):
        self.working_proxies: List[Dict] = []
        self.current_proxy_index = 0
        self.last_proxy_update = 0
        
    async def get_proxies_from_api(self) -> List[Dict]:
        """Получаем список прокси с API"""
        try:
            url = f"{settings.proxy_api_url}{settings.proxy_api_key}"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as response:
                    if response.status == 200:
                        data = await response.json()
                        
                        if 'error' in data:
                            print(f"Ошибка API прокси: {data['error']}")
                            return []
                        
                        # Парсим ответ в формате {"0": {...}, "1": {...}, ...}
                        proxies = []
                        for key, proxy_data in data.items():
                            if key.isdigit() and isinstance(proxy_data, dict):
                                # Парсим name в формате "ip:port"
                                name = proxy_data.get('name', '')
                                if ':' in name:
                                    ip, port = name.split(':', 1)
                                    proxy = {
                                        'ip': ip,
                                        'port': int(port),
                                        'type': proxy_data.get('type', 'HTTP'),
                                        'speed': proxy_data.get('speed', 0),
                                        'country': proxy_data.get('country', 'RU'),
                                        'work': proxy_data.get('work', 0)
                                    }
                                    # Добавляем прокси с work=1 (рабочие) или work=2 (проверяем сами)
                                    if proxy_data.get('work', 0) in [1, 2]:
                                        proxies.append(proxy)
                        
                        print(f"Получено {len(proxies)} прокси с API")
                        return proxies
                    else:
                        print(f"Ошибка получения прокси: {response.status}")
                        return []
        except Exception as e:
            print(f"Ошибка при запросе прокси: {e}")
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
        proxies = await self.get_proxies_from_api()
        
        if not proxies:
            print("Не удалось получить прокси с API")
            return
        
        self.working_proxies = await self.check_all_proxies(proxies)
        self.current_proxy_index = 0
        self.last_proxy_update = time.time()
        
        if self.working_proxies:
            print(f"Обновлено {len(self.working_proxies)} рабочих прокси")
        else:
            print("Не найдено рабочих прокси")
    
    def get_next_proxy(self) -> Optional[Dict]:
        """Получаем следующий рабочий прокси"""
        if not self.working_proxies:
            return None
        
        proxy = self.working_proxies[self.current_proxy_index]
        self.current_proxy_index = (self.current_proxy_index + 1) % len(self.working_proxies)
        return proxy
    
    def mark_proxy_failed(self, proxy: Dict):
        """Помечаем прокси как нерабочий и удаляем из списка"""
        if proxy in self.working_proxies:
            self.working_proxies.remove(proxy)
            print(f"Прокси {proxy.get('ip')}:{proxy.get('port')} помечен как нерабочий")
            
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
        
        if proxy.get('username') and proxy.get('password'):
            return f"http://{proxy['username']}:{proxy['password']}@{proxy['ip']}:{proxy['port']}"
        else:
            return f"http://{proxy['ip']}:{proxy['port']}"


# Глобальный экземпляр менеджера прокси
proxy_manager = ProxyManager()
