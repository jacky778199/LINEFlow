"""Persistent per-instance subscriptions and atomic message checkpoints."""
import json
import time
import uuid

DEFAULT_GROUPS = ["台灣福祉車隊-資源群", "106-台灣福祉車衛星派遣車隊"]


def checkpoint_position(previous, current, chat_name):
    """Recover a missing tail only from a unique, timed multi-message anchor.

    Call with a continuous window ending at the verified newest message.
    A removed/unsent last message must not permanently block synchronization.
    """
    end = overlap(previous, current, chat_name)
    if end is not None:
        return end
    candidates = []
    for old_start in range(len(previous)):
        for new_start in range(len(current)):
            size = 0
            while old_start + size < len(previous) and new_start + size < len(current):
                a, b = previous[old_start + size], current[new_start + size]
                if (not a.get("msg_time") or a.get("msg_time") != b.get("msg_time")
                        or overlap([a], [b], chat_name) is None):
                    break
                size += 1
            if size >= 2:
                old_end, new_end = old_start + size, new_start + size
                if len({(x["content"], x["msg_time"]) for x in previous[old_start:old_end]}) < 2:
                    continue
                # Do not reinsert surviving tail messages or accept an ambiguous gap.
                if any(overlap([item], current, chat_name) is not None
                       for item in previous[old_end:]):
                    continue
                candidates.append((size, new_end))
    if not candidates:
        return None
    longest = max(size for size, _ in candidates)
    ends = {end for size, end in candidates if size == longest}
    return ends.pop() if len(ends) == 1 else None


def overlap(previous, current, chat_name=None):
    """Match the longest suffix of the checkpoint in the current window."""
    for size in range(min(len(previous), len(current)), 0, -1):
        for start in range(len(current) - size + 1):
            def same(a, b):
                if a == b:
                    return True
                if not isinstance(a, dict) or not isinstance(b, dict):
                    return False
                # Viewport edges can omit the timestamp/avatar of the same bubble.
                return (a.get("content") == b.get("content") and
                        (a.get("msg_time") == b.get("msg_time") or
                         not a.get("msg_time") or not b.get("msg_time")) and
                        (a.get("sender_name") == b.get("sender_name") or
                         chat_name in (a.get("sender_name"), b.get("sender_name"))))
            if all(same(a, b) for a, b in zip(previous[-size:], current[start:start + size])):
                return start + size
    return None


class GroupSyncStore:
    def __init__(self, db, instance_id):
        self.db, self.instance_id = db, instance_id
        with db.get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS group_subscriptions (
                    instance_id TEXT PRIMARY KEY, groups_json TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS group_checkpoints (
                    instance_id TEXT NOT NULL, chat_name TEXT NOT NULL,
                    snapshot TEXT NOT NULL, scanned_at INTEGER NOT NULL,
                    PRIMARY KEY(instance_id, chat_name)
                );
            """)
            conn.execute("INSERT OR IGNORE INTO group_subscriptions VALUES (?, ?, 1)",
                         (instance_id, json.dumps(DEFAULT_GROUPS, ensure_ascii=False)))

    def get(self):
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT * FROM group_subscriptions WHERE instance_id=?",
                               (self.instance_id,)).fetchone()
            return {"groups": json.loads(row["groups_json"]), "revision": row["revision"]}

    def set(self, groups):
        if (not isinstance(groups, list) or len(groups) > 100 or
                any(not isinstance(g, str) or not g.strip() or len(g) > 200 for g in groups)):
            raise ValueError("groups must be a list of up to 100 non-empty names (max 200 characters)")
        groups = list(dict.fromkeys(g.strip() for g in groups))
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            old = json.loads(conn.execute(
                "SELECT groups_json FROM group_subscriptions WHERE instance_id=?",
                (self.instance_id,)).fetchone()[0])
            for name in set(old) - set(groups):
                conn.execute("DELETE FROM group_checkpoints WHERE instance_id=? AND chat_name=?",
                             (self.instance_id, name))
            conn.execute("UPDATE group_subscriptions SET groups_json=?, revision=revision+1 WHERE instance_id=?",
                         (json.dumps(groups, ensure_ascii=False), self.instance_id))
            revision = conn.execute("SELECT revision FROM group_subscriptions WHERE instance_id=?",
                                    (self.instance_id,)).fetchone()[0]
        return {"groups": groups, "revision": revision}

    def checkpoint(self, name):
        with self.db.get_connection() as conn:
            row = conn.execute("SELECT snapshot FROM group_checkpoints WHERE instance_id=? AND chat_name=?",
                               (self.instance_id, name)).fetchone()
            return json.loads(row[0]) if row else None

    def reset_checkpoint(self, name):
        """Reset checkpoint for a specific group to re-baseline on next scan."""
        with self.db.get_connection() as conn:
            conn.execute("DELETE FROM group_checkpoints WHERE instance_id=? AND chat_name=?",
                         (self.instance_id, name))

    def commit(self, name, revision, snapshot):
        """Baseline stores no history; no overlap leaves the checkpoint unchanged."""
        if not snapshot:
            return "empty", []
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            subscription = conn.execute("SELECT * FROM group_subscriptions WHERE instance_id=?",
                                        (self.instance_id,)).fetchone()
            if subscription["revision"] != revision or name not in json.loads(subscription["groups_json"]):
                return "stale", []
            row = conn.execute("SELECT snapshot FROM group_checkpoints WHERE instance_id=? AND chat_name=?",
                               (self.instance_id, name)).fetchone()
            previous = json.loads(row[0]) if row else None
            start = checkpoint_position(previous, snapshot, name) if previous else len(snapshot)
            if start is None:
                return "gap_detected", []
            messages = []
            now = int(time.time())
            for item in snapshot[start:]:
                message = dict(instance_id=self.instance_id, chat_type="group", chat_name=name,
                               sender_name=item["sender_name"], content=item["content"],
                               msg_time=item["msg_time"], timestamp=now, delivered=0,
                               msg_hash="sync:" + uuid.uuid4().hex)
                cursor = conn.execute("""INSERT INTO messages
                    (msg_hash, instance_id, chat_type, chat_name, sender_name, content, msg_time, timestamp)
                    VALUES (:msg_hash, :instance_id, :chat_type, :chat_name, :sender_name, :content, :msg_time, :timestamp)
                    """, message)
                message["seq_id"] = cursor.lastrowid
                messages.append(message)
            conn.execute("INSERT OR REPLACE INTO group_checkpoints VALUES (?, ?, ?, ?)",
                         (self.instance_id, name, json.dumps(snapshot[-100:], ensure_ascii=False), now))
        return "baseline" if previous is None else "synced", messages
