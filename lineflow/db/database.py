import sqlite3
from pathlib import Path
from typing import Optional

SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS messages (
    seq_id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_hash TEXT UNIQUE NOT NULL,
    instance_id TEXT NOT NULL DEFAULT 'instance1',
    chat_type TEXT NOT NULL,
    chat_name TEXT NOT NULL,
    sender_name TEXT NOT NULL,
    msg_time TEXT,
    content TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_messages_seq_id ON messages (seq_id);
CREATE INDEX IF NOT EXISTS idx_messages_msg_hash ON messages (msg_hash);
CREATE INDEX IF NOT EXISTS idx_messages_instance ON messages (instance_id);
"""

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_dir()
        self.init_db()

    def _ensure_dir(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        with self.get_connection() as conn:
            conn.executescript(SCHEMA_SQL)
            # 自動遷移：若既有資料表缺少 msg_time 欄位則自動添加
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(messages)")
            columns = [row["name"] for row in cursor.fetchall()]
            if "msg_time" not in columns:
                cursor.execute("ALTER TABLE messages ADD COLUMN msg_time TEXT")
            conn.commit()
