import logging
import time
from typing import Optional, Tuple
import uiautomator2 as u2
from .device import AndroidDeviceManager

logger = logging.getLogger("lineflow.adb.sender")

class LineSender:
    def __init__(self, device_mgr: AndroidDeviceManager):
        self.device_mgr = device_mgr

    @property
    def d(self) -> Optional[u2.Device]:
        return self.device_mgr.d

    def send_message(self, target: str, message: str) -> Tuple[bool, Optional[str]]:
        """
        搜尋欄直達法：
        1. 點擊頂部搜尋圖示
        2. 輸入 target (群組名或聯絡人)
        3. 點選搜尋結果首項進入聊天室
        4. 輸入文字並點擊發送鍵
        5. 連續按返回鍵回到聊天列表頂層
        """
        d = self.d
        if not d:
            return False, "Device not connected"

        try:
            self.device_mgr.ensure_app_running()

            # 步驟 1: 尋找並點擊搜尋按鈕
            search_btn = d(descriptionMatches="(?i)search|搜尋|檢索|Search")
            if not search_btn.exists(timeout=2):
                search_btn = d(resourceIdMatches=".*search.*")

            if search_btn.exists:
                search_btn.click()
                time.sleep(0.5)
            else:
                logger.warning("Search icon not immediately found, checking if search input already active...")

            # 步驟 2: 輸入 target
            search_input = d(className="android.widget.EditText")
            if not search_input.exists(timeout=2):
                self.device_mgr.safe_back_to_main(max_tries=1)
                return False, f"Could not find search input field for target: {target}"

            search_input.set_text(target)
            time.sleep(1.5) # 等待搜尋結果列表載入

            # 步驟 3: 點擊搜尋結果 (限定為 TextView，嚴格排除上方搜尋欄 EditText)
            target_item = d(className="android.widget.TextView", text=target)
            if not target_item.exists(timeout=2):
                target_item = d(className="android.widget.TextView", textContains=target)

            if not target_item.exists:
                # 找不到目標聊天室，安全返回
                self.device_mgr.safe_back_to_main(max_tries=2)
                return False, f"Chat target '{target}' not found in search results"

            target_item.click()
            time.sleep(1.5) # 等待進入對話視窗

            # 步驟 4: 找到對話輸入框並獲取焦點輸入文字
            msg_input = d(resourceIdMatches=".*(chat_ui_message_edit|chathistory_message_edit).*")
            if not msg_input.exists(timeout=2):
                # 再次嘗試點選 target 進入聊天視窗
                target_item = d(className="android.widget.TextView", text=target)
                if target_item.exists:
                    target_item.click()
                    time.sleep(1.5)
                    msg_input = d(resourceIdMatches=".*(chat_ui_message_edit|chathistory_message_edit).*")

            if not msg_input.exists:
                self.device_mgr.safe_back_to_main(max_tries=2)
                return False, f"Failed to enter chat room for '{target}' (chat edit box not found)"

            # 4.1 點擊輸入框取得焦點，激活輸入狀態
            msg_input.click()
            time.sleep(0.5)

            # 4.2 輸入文字 (優先使用 FastInputIME 支援繁體中文與各類符號)
            typed = False
            try:
                d.set_fastinput_ime(True)
                d.send_keys(message)
                time.sleep(0.5)
                typed = True
            except Exception as e:
                logger.warning(f"FastInputIME failed, falling back to set_text: {e}")

            # 若 send_keys 未成功或內容仍為空，使用 set_text 補強
            current_content = msg_input.get_text() or ""
            if not current_content or not typed:
                msg_input.set_text(message)
                time.sleep(0.5)

            # 步驟 5: 點擊發送按鈕 (嚴格限定 Send/傳送，避免誤觸語音錄音)
            send_btn = d(descriptionMatches="(?i)send|傳送|送信|Send")
            if not send_btn.exists(timeout=1.5):
                # 再次尋找具有發送意圖的按鈕
                send_btn = d(resourceIdMatches=".*chat_ui_send_button.*", descriptionMatches="(?i)send|傳送|送信|Send")

            if send_btn.exists:
                send_btn.click()
            else:
                logger.warning("Send button with 'Send' description not found, pressing Enter key")
                d.press("enter")

            time.sleep(0.5)
            logger.info(f"Successfully sent message to '{target}': {message[:30]}...")

            # 步驟 6: 安全返回主聊天列表 (穿透 SearchActivity 並回歸 Chats Tab)
            self.device_mgr.safe_back_to_main(max_tries=5)
            time.sleep(0.5)

            return True, None

        except Exception as e:
            logger.error(f"Exception during send_message to '{target}': {e}", exc_info=True)
            self.device_mgr.press_back(3)
            return False, str(e)
