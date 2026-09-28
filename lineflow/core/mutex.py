import asyncio
import logging

logger = logging.getLogger("lineflow.core.mutex")

class UILock:
    """UI 操作互斥鎖，防止發送指令與輪詢監聽互相干擾畫面"""
    def __init__(self):
        self._lock = asyncio.Lock()

    async def acquire(self):
        await self._lock.acquire()

    def release(self):
        self._lock.release()

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()
