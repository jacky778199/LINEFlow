import logging
import re
import time
from typing import Optional, Tuple
import uiautomator2 as u2
from .device import AndroidDeviceManager
from .group_watcher import find_chat_result

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
        1. 確保回到主聊天列表 (Chats Tab)
        2. 點擊頂部主搜尋欄
        3. 輸入 target (群組名或聯絡人)
        4. 點選搜尋結果首項進入聊天室
        5. 輸入文字並點擊發送鍵
        6. UI 驗證發送結果
        7. 連續按返回鍵回到聊天列表頂層
        """
        d = self.d
        if not d:
            return False, "Device not connected"

        try:
            self.device_mgr.ensure_app_running()

            # 步驟 1: 確保回到聊天主分頁 (Chats Tab)，絕不能在聊天室內部直接點擊搜尋
            if not self.device_mgr.safe_back_to_main(max_tries=5):
                return False, "Failed to navigate back to Chats tab"

            # 步驟 2: 點擊主搜尋列 (main_tab_search_bar)
            pkg = self.device_mgr.package_name
            search_bar = d(resourceId=pkg + ":id/main_tab_search_bar")
            if not search_bar.exists:
                search_bar = d(resourceIdMatches=".*main_tab_search.*")
            if not search_bar.exists:
                search_bar = d(descriptionMatches="(?i)search|搜尋|檢索|Search")

            if not search_bar.exists:
                return False, "Main search bar not found on Chats tab"

            search_bar.click()
            time.sleep(0.5)

            # 步驟 3: 尋找全域搜尋輸入框 (嚴格排除任何聊天室訊息輸入框)
            if d(resourceIdMatches=".*(chat_ui_message_edit|chathistory_message_edit).*").exists:
                self.device_mgr.safe_back_to_main(max_tries=2)
                return False, "Aborted search: currently inside conversation"

            search_input = d(className="android.widget.EditText", resourceId=pkg + ":id/input_text")
            if not search_input.exists:
                search_input = d(className="android.widget.EditText")
                if search_input.exists:
                    res_name = search_input.info.get("resourceName") or ""
                    if any(k in res_name for k in ("chat_ui_message_edit", "chathistory_message_edit")):
                        self.device_mgr.safe_back_to_main(max_tries=2)
                        return False, "Aborted search: matched chat edit box instead of search input"

            if not search_input.exists:
                self.device_mgr.safe_back_to_main(max_tries=2)
                return False, f"Could not find search input field for target: {target}"

            search_input.set_text(target)
            time.sleep(1.0) # 等待搜尋結果列表載入

            # 步驟 4: 點擊搜尋結果 (優先精準點選 Chats 分類首項)
            point = None
            for _ in range(6):
                point = find_chat_result(d.dump_hierarchy(), target)
                if point:
                    break
                time.sleep(0.3)

            if point:
                d.click(*point)
            else:
                target_item = d(className="android.widget.TextView", text=target)
                if not target_item.exists(timeout=1):
                    target_item = d(className="android.widget.TextView", textContains=target)

                if not target_item.exists:
                    self.device_mgr.safe_back_to_main(max_tries=2)
                    return False, f"Chat target '{target}' not found in search results"

                target_item.click()

            time.sleep(1.5) # 等待進入對話視窗

            # 步驟 5: 找到對話輸入框並驗證目標
            msg_input = d(className="android.widget.EditText", resourceIdMatches=".*(chat_ui_message_edit|chathistory_message_edit).*")
            if not msg_input.exists:
                self.device_mgr.safe_back_to_main(max_tries=2)
                return False, f"Failed to enter chat room for '{target}' (chat edit box not found)"

            # 驗證聊天室標題
            title = d(resourceIdMatches=".*header_title.*")
            if title.exists:
                title_text = title.get_text() or ""
                expected_pat = re.escape(target) + r"(?:\s*[（(]\d+[）)])?"
                if not re.fullmatch(expected_pat, title_text) and target not in title_text:
                    logger.warning(f"Header title '{title_text}' does not match target '{target}'")

            # 5.1 點擊輸入框取得焦點並清理殘留的草稿
            msg_input.click()
            time.sleep(0.3)
            current_draft = msg_input.get_text() or ""
            if current_draft:
                logger.info(f"Clearing leftover draft in chat edit box: {current_draft[:25]}")
                try:
                    msg_input.clear_text()
                    time.sleep(0.2)
                except Exception as e:
                    logger.warning(f"Failed to clear draft text: {e}")

            # 5.2 輸入文字 (優先使用 FastInputIME 支援繁體中文與各類符號)
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

            # 步驟 6: 點擊發送按鈕 (嚴格限定 Send/傳送，避免誤觸語音錄音)
            send_btn = d(descriptionMatches="(?i)send|傳送|送信|Send")
            if not send_btn.exists(timeout=1.5):
                send_btn = d(resourceIdMatches=".*chat_ui_send_button.*", descriptionMatches="(?i)send|傳送|送信|Send")

            if send_btn.exists:
                send_btn.click()
            else:
                logger.warning("Send button with 'Send' description not found, pressing Enter key")
                d.press("enter")

            time.sleep(0.5)

            # 步驟 7: UI 驗證發送結果 (確認輸入框清空、無重送圖示、氣泡出現)
            verified, verify_err = self._verify_message_sent(msg_input, message)
            if not verified:
                logger.error(f"Delivery verification failed for target '{target}': {verify_err}")
                # 清除輸入框殘留，防止留作草稿
                try:
                    if msg_input.exists and msg_input.get_text():
                        msg_input.clear_text()
                except Exception:
                    pass
                self.device_mgr.safe_back_to_main(max_tries=2)
                return False, f"Verification failed: {verify_err}"

            logger.info(f"Successfully sent and verified message to '{target}': {message[:30]}...")

            # 步驟 8: 安全返回主聊天列表 (穿透 SearchActivity 並回歸 Chats Tab)
            self.device_mgr.safe_back_to_main(max_tries=5)
            time.sleep(0.5)

            return True, None

        except Exception as e:
            logger.error(f"Exception during send_message to '{target}': {e}", exc_info=True)
            self.device_mgr.press_back(3)
            return False, str(e)

    def _verify_message_sent(self, msg_input, message: str, timeout: float = 3.5) -> Tuple[bool, Optional[str]]:
        """
        驗證訊息是否確實成功送出：
        1. 檢查輸入框已被清空 (若仍留有原訊息，代表送出未觸發或卡死)
        2. 檢查未出現 LINE 發送失敗標記 (例如驚嘆號、重送按鈕)
        3. 檢查聊天氣泡列表中是否已出現剛剛發送的內容
        """
        d = self.d
        if not d:
            return False, "Device not connected during verification"

        start_time = time.time()
        input_cleared = False

        # 1. 檢查輸入框是否已清空
        while time.time() - start_time < timeout:
            current_txt = (msg_input.get_text() or "").strip() if msg_input.exists else ""
            if not current_txt or current_txt != message.strip():
                input_cleared = True
                break
            time.sleep(0.3)

        if not input_cleared:
            return False, "Input field was not cleared; send action may not have taken effect"

        # 2. 檢查畫面是否有發送失敗／重新傳送按鈕
        retry_elem = d(descriptionMatches="(?i)retry|resend|重新傳送|重試|無法傳送")
        if not retry_elem.exists(timeout=0.5):
            retry_elem = d(resourceIdMatches=".*(retry|resend|error_icon|send_fail).*")
        if retry_elem.exists:
            return False, "LINE indicated send failure (retry/error icon detected)"

        # 3. 檢查聊天室歷史中是否出現該發送文字氣泡
        sample_text = (message.split("\n")[0][:30]).strip()
        if sample_text:
            bubble_elem = d(className="android.widget.TextView", textContains=sample_text)
            if not bubble_elem.exists(timeout=1.5):
                bubble_elem = d(resourceIdMatches=".*chat_ui_message_text.*", textContains=sample_text)

            if not bubble_elem.exists:
                return False, "Message bubble not found in chat history"

        return True, None

