import hashlib
import sqlite3
import time
from typing import Dict, List, Optional
from .database import Database

class MessageRepository:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def generate_hash(instance_id: str, chat_name: str, sender_name: str, content: str, timestamp: int) -> str:
        # 粗略至分鐘級或以內容+發言者+群組做唯一指紋，避免毫秒微差產生重複
        raw_str = f"{instance_id}|{chat_name.strip()}|{sender_name.strip()}|{content.strip()}"
        return hashlib.md5(raw_str.encode("utf-8")).hexdigest()

    def exists_by_hash(self, msg_hash: str) -> bool:
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM messages WHERE msg_hash = ? LIMIT 1", (msg_hash,))
            return cursor.fetchone() is not None

    def insert_message(
        self,
        instance_id: str,
        chat_type: str,
        chat_name: str,
        sender_name: str,
        content: str,
        msg_time: Optional[str] = None,
        timestamp: Optional[int] = None
    ) -> Optional[Dict]:
        """
        嘗試寫入訊息。如果 msg_hash 已存在則返回 None (去重)。
        若成功入庫則返回包含全域自動遞增 seq_id 的訊息物件字典。
        """
        if timestamp is None:
            timestamp = int(time.time())

        msg_hash = self.generate_hash(instance_id, chat_name, sender_name, content, timestamp)

        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(
                    """
                    INSERT INTO messages (msg_hash, instance_id, chat_type, chat_name, sender_name, msg_time, content, timestamp, delivered)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (msg_hash, instance_id, chat_type, chat_name, sender_name, msg_time, content, timestamp)
                )
                conn.commit()
                seq_id = cursor.lastrowid
                return {
                    "seq_id": seq_id,
                    "msg_hash": msg_hash,
                    "instance_id": instance_id,
                    "chat_type": chat_type,
                    "chat_name": chat_name,
                    "sender_name": sender_name,
                    "msg_time": msg_time,
                    "content": content,
                    "timestamp": timestamp,
                    "delivered": 0
                }
            except sqlite3.IntegrityError:
                # 訊息已存在 (去重)
                return None

    def get_messages_since(self, since_seq_id: int, instance_id: Optional[str] = None, limit: int = 500) -> List[Dict]:
        """
        斷線補償查詢：取得大於 since_seq_id 的歷史遺漏訊息
        """
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            if instance_id:
                cursor.execute(
                    """
                    SELECT seq_id, msg_hash, instance_id, chat_type, chat_name, sender_name, msg_time, content, timestamp, delivered, created_at
                    FROM messages
                    WHERE seq_id > ? AND instance_id = ?
                    ORDER BY seq_id ASC
                    LIMIT ?
                    """,
                    (since_seq_id, instance_id, limit)
                )
            else:
                cursor.execute(
                    """
                    SELECT seq_id, msg_hash, instance_id, chat_type, chat_name, sender_name, msg_time, content, timestamp, delivered, created_at
                    FROM messages
                    WHERE seq_id > ?
                    ORDER BY seq_id ASC
                    LIMIT ?
                    """,
                    (since_seq_id, limit)
                )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]

    def mark_delivered(self, seq_id: int):
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE messages SET delivered = 1 WHERE seq_id = ?", (seq_id,))
            conn.commit()
