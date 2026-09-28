import logging
import subprocess
import time
from typing import Optional
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

    def safe_back_to_main(self, max_tries: int = 5):
        """安全返回主畫面，一旦到達 MainActivity 即停止按 Back，絕對不退出 App 到桌面"""
        if not self.d:
            return
        for _ in range(max_tries):
            try:
                curr = self.d.app_current()
                pkg = curr.get("package", "")
                act = curr.get("activity", "")
                # 若已經在 MainActivity，停止按 Back 避免跳出桌面
                if pkg == self.package_name and "MainActivity" in act:
                    break
                self.d.press("back")
                time.sleep(0.5)
            except Exception:
                break

        # 確保回到聊天列表標籤頁 (Chats Tab)
        try:
            chats_tab = self.d(descriptionMatches="(?i)chats|聊天|トーク")
            if chats_tab.exists(timeout=0.5):
                chats_tab.click()
        except Exception:
            pass

    def press_back(self, times: int = 1):
        """安全按返回鍵 (若已在 MainActivity 則自動忽略，防止跳出 App)"""
        self.safe_back_to_main(max_tries=times)
