import asyncio
import logging
import threading
from typing import Callable, Optional, Tuple
from ..adb.device import AndroidDeviceManager
from ..adb.sender import LineSender
from ..adb.group_watcher import GroupWatcher
from ..db.group_sync import GroupSyncStore
from ..db.sync_status import SyncStatusStore
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
        on_new_message: Optional[Callable[[dict], None]] = None,
        on_sync_event=None
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
        self.group_store = GroupSyncStore(repository.db, instance_id)
        self.sync_status = SyncStatusStore(repository.db, instance_id)
        self.on_sync_event = on_sync_event
        self._monitor_task = None
        self._client_pending = 0
        self._interrupt_scan = threading.Event()
        self.group_watcher = GroupWatcher(self.device_mgr, self.group_store, on_new_message,
                                          self._interrupt_scan.is_set)
        self._running = False
        self._poll_task: Optional[asyncio.Task] = None

    async def start(self):
        """啟動引擎與輪詢任務"""
        if self._running:
            return
        self._running = True
        self.sync_status.reconcile(self.group_store.get()["groups"])
        self._monitor_task = asyncio.create_task(self._monitor_loop())
        if not self._client_pending:
            self._interrupt_scan.clear()
        logger.info(f"Starting LineFlowEngine for '{self.instance_id}' on {self.config.adb_serial}...")
        # 於背景線程初始化 ADB 連線
        await asyncio.to_thread(self.device_mgr.init_device)
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def stop(self):
        """停止輪詢任務"""
        self._running = False
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        logger.info(f"Stopped LineFlowEngine for '{self.instance_id}'")

    async def _monitor_loop(self):
        # Independent of UI lock / scan completion: detects a stuck worker too.
        while self._running:
            try:
                self.sync_status.reconcile(self.group_store.get()["groups"])
                for event in self.sync_status.evaluate():
                    logger.warning("%s: %s (%s)", event["type"], event["data"]["chat_name"],
                                   event["data"]["alert_level"])
                    if self.on_sync_event:
                        await asyncio.wait_for(self.on_sync_event(event), timeout=5)
            except Exception:
                logger.exception("Sync monitor failed for %s", self.instance_id)
            await asyncio.sleep(5)

    async def _poll_loop(self):
        """Round-robin subscriptions; writers always get the next UI turn."""
        index = 0
        while self._running:
            try:
                await self.ui_lock.acquire_background()
                try:
                    subscription = self.group_store.get()
                    groups = subscription["groups"]
                    if groups:
                        name = groups[index % len(groups)]
                        index += 1
                        self.sync_status.reconcile(groups)
                        self.sync_status.begin(name)
                        task = asyncio.create_task(asyncio.to_thread(
                            self.group_watcher.scan, name, subscription["revision"]))
                        try:
                            result = await asyncio.shield(task)
                            if result["outcome"] == "failed":
                                logger.warning("Scan failed for %s: stage=%s reason=%s duration=%s",
                                               name, result["stage"], result["reason"], result["duration_sec"])
                            if self.group_store.get()["revision"] == subscription["revision"]:
                                self.sync_status.finish(name, result)
                            else:
                                self.sync_status.finish(name, dict(result, outcome="cancelled", reason="stale_subscription"))
                        except asyncio.CancelledError:
                            self._interrupt_scan.set()
                            await task
                            raise
                finally:
                    self.ui_lock.release()
            except Exception as e:
                logger.error(f"Error in poll loop [{self.instance_id}]: {e}", exc_info=True)

            await asyncio.sleep(max(10.0, self.config.poll_interval_sec))

    async def send_message(self, target: str, message: str) -> tuple[bool, Optional[str]]:
        """
        接收來自 WebSocket 的發送回覆請求
        具備排隊與 UI 互斥鎖，避免與輪詢監聽互相搶佔畫面
        """
        self._client_pending += 1
        self._interrupt_scan.set()
        try:
            async with self.ui_lock:
                task = asyncio.create_task(asyncio.to_thread(self.sender.send_message, target, message))
                try:
                    return await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
        finally:
            self._client_pending -= 1
            if not self._client_pending and self._running:
                self._interrupt_scan.clear()

    async def take_screenshot(self, format: str = "jpeg", quality: int = 80) -> Tuple[Optional[bytes], Optional[Tuple[int, int]], Optional[str]]:
        """
        截取目前裝置畫面
        不佔用 UI 鎖，以利外部隨時監控或除錯畫面狀態
        """
        return await asyncio.to_thread(self.device_mgr.take_screenshot, format, quality)
