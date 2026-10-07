import io
import logging
import subprocess
import time
from typing import Optional, Tuple
import uiautomator2 as u2

logger = logging.getLogger("lineflow.adb.device")

class AndroidDeviceManager:
    def __init__(self, serial: str, package_name: str = "jp.naver.line.android"):
        self.serial = serial
        self.package_name = package_name
        self.d: Optional[u2.Device] = None
        self._last_launch_time = 0.0

    def connect_adb(self) -> bool:
        """執行 adb connect"""
        try:
            cmd = ["adb", "connect", self.serial]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
            logger.info(f"adb connect {self.serial} result: {res.stdout.strip()}")
            return "connected" in res.stdout.lower() or "already" in res.stdout.lower()
        except Exception as e:
            logger.warning(f"Failed to run adb connect {self.serial}: {e}")
            return False

    def init_device(self) -> bool:
        """初始化 uiautomator2 連線"""
        try:
            self.connect_adb()
            time.sleep(1)
            self.d = u2.connect(self.serial)
            info = self.d.info
            logger.info(f"Connected to device {self.serial}, screen: {info.get('displayWidth')}x{info.get('displayHeight')}")
            return True
        except Exception as e:
            logger.error(f"Error connecting to uiautomator2 on {self.serial}: {e}")
            self.d = None
            return False

    def is_alive(self) -> bool:
        if not self.d:
            return False
        try:
            return bool(self.d.info)
        except Exception:
            return False

    def ensure_connected(self) -> bool:
        if not self.is_alive():
            logger.info(f"Device {self.serial} not alive, reconnecting...")
            return self.init_device()
        return True

    def start_line(self):
        """透過 am start 原生 Intent 啟動 LINE，避免 monkey 指令造成崩潰與意外關閉"""
        if not self.ensure_connected():
            return
        try:
            self.d.shell(["am", "start", "-n", f"{self.package_name}/.activity.SplashActivity"])
            time.sleep(1.5)
        except Exception as e:
            logger.warning(f"Error launching line via am start: {e}")

    def ensure_app_running(self):
        """確保 LINE 在前景執行，若跳出則重新啟動 (具備冷卻防抖)"""
        if not self.ensure_connected():
            return
        try:
            current_app = self.d.app_current()
            pkg = current_app.get("package")
            if pkg != self.package_name:
                now = time.time()
                # 至少冷卻 15 秒才嘗試再次啟動，避免使用者操作或初次登入時被頻繁中斷
                if now - self._last_launch_time > 15.0:
                    self._last_launch_time = now
                    logger.info(f"LINE not in foreground (currently {pkg}), launching {self.package_name}...")
                    self.start_line()
        except Exception as e:
            logger.error(f"Failed to check/start app {self.package_name}: {e}")

    def restart_app(self):
        """重新啟動 LINE"""
        if not self.ensure_connected():
            return
        try:
            logger.info(f"Restarting {self.package_name} on {self.serial}...")
            self.d.app_stop(self.package_name)
            time.sleep(1)
            self.start_line()
            time.sleep(2)
        except Exception as e:
            logger.error(f"Error restarting app: {e}")

    def safe_back_to_main(self, max_tries: int = 5) -> bool:
        """Return only after verifying the Chats tab and main search bar."""
        if not self.d:
            return False
        for attempt in range(max_tries + 1):
            try:
                curr = self.d.app_current()
                if curr.get("package") != self.package_name:
                    return False
                tabs = self.d(resourceId=self.package_name + ":id/bnb_button_clickable_area",
                              descriptionMatches="(?i)(Chats|聊天|トーク).*" )
                if tabs.exists:
                    tabs.click()
                    time.sleep(.3)
                    if (self.d(resourceId=self.package_name + ":id/main_tab_search_bar").exists
                            and not self.d(resourceIdMatches=".*(chat_ui_message_edit|chathistory_message_edit).*").exists):
                        return True
                    return False
                if attempt == max_tries:
                    break
                self.d.press("back")
                time.sleep(0.5)
            except Exception as exc:
                logger.warning("Failed to return to Chats: %s", exc)
                break
        logger.warning("Could not verify Chats page after %d back attempts", max_tries)
        return False

    def press_back(self, times: int = 1):
        """安全按返回鍵 (若已在 MainActivity 則自動忽略，防止跳出 App)"""
        self.safe_back_to_main(max_tries=times)

    def take_screenshot(self, format: str = "jpeg", quality: int = 80) -> Tuple[Optional[bytes], Optional[Tuple[int, int]], Optional[str]]:
        """
        截取目前裝置螢幕畫面 (不改變或干擾目前 UI 狀態)
        :param format: "jpeg" 或 "png" (預設 "jpeg")
        :param quality: JPEG 圖片品質 1-100 (預設 80，僅在 format="jpeg" 時生效)
        :return: (image_bytes, (width, height), error_message)
        """
        if not self.ensure_connected():
            return None, None, f"Device {self.serial} is not connected"
        if not self.d:
            return None, None, f"Device connection to {self.serial} is not initialized"

        try:
            img = self.d.screenshot()
            width, height = img.size
            buf = io.BytesIO()
            fmt = "PNG" if format.lower() == "png" else "JPEG"
            if fmt == "JPEG":
                if img.mode in ("RGBA", "P"):
                    img = img.convert("RGB")
                img.save(buf, format=fmt, quality=max(1, min(quality, 100)))
            else:
                img.save(buf, format=fmt)
            return buf.getvalue(), (width, height), None
        except Exception as e:
            logger.error(f"Error taking screenshot on {self.serial}: {e}")
            return None, None, str(e)
