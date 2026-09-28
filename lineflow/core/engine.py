import asyncio
import logging
from typing import Callable, Optional
from ..adb.device import AndroidDeviceManager
from ..adb.sender import LineSender
from ..adb.watcher import LineWatcher
from ..config import InstanceConfig
from ..db.repository import MessageRepository
from .mutex import UILock

logger = logging.getLogger("lineflow.core.engine")

class LineFlowEngine:
    def __init__(
        self,
        instance_id: str,
        config: InstanceConfig,
        repository: MessageRepository,
        on_new_message: Optional[Callable[[dict], None]] = None
    ):
        self.instance_id = instance_id
        self.config = config
        self.repository = repository
        self.ui_lock = UILock()
        self.device_mgr = AndroidDeviceManager(
            serial=config.adb_serial,
            package_name=config.line_package
        )
        self.sender = LineSender(self.device_mgr)
        self.watcher = LineWatcher(
            instance_id=instance_id,
            device_mgr=self.device_mgr,
            repository=repository,
            on_new_message_callback=on_new_message
        )
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    async def start(self):
        """啟動引擎與輪詢任務"""
        if self._running:
            return
        self._running = True
        logger.info(f"Starting LineFlowEngine for '{self.instance_id}' on {self.config.adb_serial}...")
        # 於背景線程初始化 ADB 連線
        await asyncio.to_thread(self.device_mgr.init_device)
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        """停止輪詢任務"""
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info(f"Stopped LineFlowEngine for '{self.instance_id}'")

    async def _poll_loop(self):
        """輪詢主迴圈：定期監聽未讀紅點"""
        while self._running:
            try:
                # 取得 UI 鎖後再進行介面輪詢
                async with self.ui_lock:
                    await asyncio.to_thread(self.watcher.scan_and_process_unread)
            except Exception as e:
                logger.error(f"Error in poll loop [{self.instance_id}]: {e}", exc_info=True)

            await asyncio.sleep(self.config.poll_interval_sec)

    async def send_message(self, target: str, message: str) -> tuple[bool, Optional[str]]:
        """
        接收來自 WebSocket 的發送回覆請求
        具備排隊與 UI 互斥鎖，避免與輪詢監聽互相搶佔畫面
        """
        async with self.ui_lock:
            logger.info(f"Acquired UI Lock to send message to '{target}'")
            success, err = await asyncio.to_thread(self.sender.send_message, target, message)
            return success, err
