import logging
import time
from typing import Callable, List, Optional
import uiautomator2 as u2
from .device import AndroidDeviceManager
from ..db.repository import MessageRepository

logger = logging.getLogger("lineflow.adb.watcher")

class LineWatcher:
    def __init__(
        self,
        instance_id: str,
        device_mgr: AndroidDeviceManager,
        repository: MessageRepository,
        on_new_message_callback: Optional[Callable[[dict], None]] = None
    ):
        self.instance_id = instance_id
        self.device_mgr = device_mgr
        self.repository = repository
        self.on_new_message_callback = on_new_message_callback

    @property
    def d(self) -> Optional[u2.Device]:
        return self.device_mgr.d

    def ensure_chats_tab(self):
        """確保目前在 LINE 的聊天 (Chats) 標籤頁"""
        d = self.d
        if not d:
            return
        # 尋找底部的聊天標籤
        chats_tab = d(descriptionMatches="(?i)chats|聊天|トーク")
        if not chats_tab.exists(timeout=1):
            chats_tab = d(textMatches="(?i)chats|聊天|トーク")
        if chats_tab.exists:
            chats_tab.click()
            time.sleep(0.3)

    def scan_and_process_unread(self):
        """
        掃描聊天列表中的未讀紅點，點入提取新訊息並防重入庫
        """
        d = self.d
        if not d:
            return

        self.device_mgr.ensure_app_running()

        # 如果處於登入/註冊/歡迎畫面，不要干擾使用者手動操作
        if d(textMatches="(?i).*登入|登錄|註冊|條碼登入|Login|Welcome.*").exists(timeout=0.5):
            return

        self.ensure_chats_tab()

        # 尋找未讀紅點 (限定 resource-id 包含 badge 或 unread)
        badges = d(resourceIdMatches=".*(badge|unread).*")

        badge_count = badges.count if badges.exists else 0
        if badge_count == 0:
            return

        logger.debug(f"Detected {badge_count} unread badge(s) on screen")

        # 針對第一個未讀項目點擊進入
        try:
            target_badge = badges[0]
            target_badge.click()
            time.sleep(1.0) # 等待進入聊天室

            # 確認當前確實已進入聊天室 (輸入框或標題欄存在)，避免誤點擊首頁元素
            if d(resourceIdMatches=".*(chathistory_message_edit|header_title).*").exists(timeout=1.5):
                self._extract_chat_messages()

            # 提取完畢後安全退回主聊天列表 (一旦到達 MainActivity 自動停止)
            self.device_mgr.safe_back_to_main(max_tries=2)
            time.sleep(0.5)

        except Exception as e:
            logger.warning(f"Error while processing unread chat item: {e}")
            self.device_mgr.safe_back_to_main(max_tries=2)

    def _extract_chat_messages(self):
        """
        在聊天室內部透過 DOM 快照解析結構化訊息氣泡：
        精確分離 content (純訊息內文) 與 msg_time (訊息時間，如 10:57 AM)
        """
        d = self.d
        if not d:
            return

        # 1. 取得聊天室名稱
        chat_name = "Unknown"
        header_title = d(resourceIdMatches=".*header_title.*")
        if not header_title.exists:
            header_title = d(resourceIdMatches=".*title.*")
        if header_title.exists:
            chat_name = header_title.get_text() or "Unknown"

        # 判斷是否為群組 (依名稱特徵或標題欄資訊)
        chat_type = "group" if ("(" in chat_name and ")" in chat_name) or "群" in chat_name else "direct"

        # 2. 獲取當前畫面的 DOM 結構快照並進行 XML 解析 (高效、原子化)
        try:
            xml_str = d.dump_hierarchy()
            import xml.etree.ElementTree as ET
            root = ET.fromstring(xml_str)
        except Exception as e:
            logger.error(f"Failed to dump/parse DOM hierarchy: {e}")
            return

        parsed_items = []
        # 尋找所有訊息容器 (chat_ui_row_contentview_container)
        for container in root.iter():
            res_id = container.get("resource-id", "")
            if "chat_ui_row_contentview_container" in res_id or "chat_ui_row_swipeable_framelayout" in res_id:
                content = None
                msg_time = None
                sender_name = None
                is_self = False

                for n in container.iter("node"):
                    n_res = n.get("resource-id", "")
                    n_text = n.get("text", "")
                    n_desc = n.get("content-desc", "")

                    if "chat_ui_message_text" in n_res and n_text:
                        content = n_text.strip()
                    elif "chat_ui_row_timestamp" in n_res and n_text:
                        msg_time = n_text.strip()
                    elif "chat_ui_row_thumbnail" in n_res and n_desc:
                        sender_name = n_desc.replace("'s profile", "").replace("的個人檔案", "").strip()
                    elif "chat_ui_row_read_count" in n_res:
                        is_self = True

                if content:
                    final_sender = sender_name or ("Me" if is_self else chat_name)
                    parsed_items.append({
                        "sender_name": final_sender,
                        "content": content,
                        "msg_time": msg_time or ""
                    })

        if not parsed_items:
            return

        # 3. 由下至上 (最新到舊) 遍歷比對指紋
        new_messages_to_insert = []
        now_ts = int(time.time())

        for item in reversed(parsed_items):
            content = item["content"]
            sender_name = item["sender_name"]
            msg_time = item["msg_time"]

            msg_hash = MessageRepository.generate_hash(
                self.instance_id, chat_name, sender_name, content, now_ts
            )

            # 若遇到已入庫的指紋，表示更早之前的訊息都已經被提取過了，停止向上掃描
            if self.repository.exists_by_hash(msg_hash):
                break

            new_messages_to_insert.append({
                "instance_id": self.instance_id,
                "chat_type": chat_type,
                "chat_name": chat_name,
                "sender_name": sender_name,
                "content": content,
                "msg_time": msg_time,
                "timestamp": now_ts
            })

        # 4. 正序 (由舊到新) 寫入資料庫並廣播推播
        for msg in reversed(new_messages_to_insert):
            inserted = self.repository.insert_message(
                instance_id=msg["instance_id"],
                chat_type=msg["chat_type"],
                chat_name=msg["chat_name"],
                sender_name=msg["sender_name"],
                content=msg["content"],
                msg_time=msg["msg_time"],
                timestamp=msg["timestamp"]
            )
            if inserted:
                logger.info(
                    f"New message [seq={inserted['seq_id']}] from '{chat_name}' ({msg['sender_name']}) at {msg['msg_time']}: {msg['content'][:25]}"
                )
                if self.on_new_message_callback:
                    try:
                        self.on_new_message_callback(inserted)
                    except Exception as cb_err:
                        logger.error(f"Error in on_new_message_callback: {cb_err}")
