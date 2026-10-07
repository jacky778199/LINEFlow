import asyncio
import logging

logger = logging.getLogger("lineflow.core.mutex")

class UILock:
    """UI 操作互斥鎖，防止發送指令與輪詢監聽互相干擾畫面"""
    def __init__(self):
        self._lock = asyncio.Lock()
        self._writers = 0
        self._changed = asyncio.Condition()

    async def acquire_background(self):
        async with self._changed:
            await self._changed.wait_for(lambda: not self._lock.locked() and not self._writers)
            await self._lock.acquire()

    async def _notify(self):
        async with self._changed:
            self._changed.notify_all()

    async def acquire(self):
        self._writers += 1
        try:
            await self._lock.acquire()
        finally:
            self._writers -= 1
            asyncio.create_task(self._notify())

    def release(self):
        self._lock.release()
        asyncio.create_task(self._notify())

    def locked(self) -> bool:
        return self._lock.locked()

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        self.release()
