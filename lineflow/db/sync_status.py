"""Persistent per-group scan health; alert transitions are evaluated independently of UI work."""
import json
import time


class SyncStatusStore:
    def __init__(self, db, instance_id):
        self.db, self.instance_id = db, instance_id
        with db.get_connection() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS group_sync_status (
                instance_id TEXT NOT NULL, chat_name TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(instance_id, chat_name))""")

    def _save(self, conn, name, state):
        conn.execute("INSERT OR REPLACE INTO group_sync_status VALUES (?, ?, ?)",
                     (self.instance_id, name, json.dumps(state)))

    def reconcile(self, groups, now=None):
        now = int(time.time()) if now is None else now
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = {r[0] for r in conn.execute(
                "SELECT chat_name FROM group_sync_status WHERE instance_id=?", (self.instance_id,))}
            for name in existing - set(groups):
                conn.execute("DELETE FROM group_sync_status WHERE instance_id=? AND chat_name=?",
                             (self.instance_id, name))
            for name in set(groups) - existing:
                self._save(conn, name, dict(created_at=now, last_attempt_at=None,
                    last_success_at=None, last_complete_at=None, last_finished_at=None,
                    in_progress=False, consecutive_failures=0, consecutive_successes=0,
                    outcome="pending", reason=None, stage=None, duration_sec=None,
                    inserted=0, alert_level="ok"))

    def begin(self, name, now=None):
        self._update(name, lambda s: s.update(last_attempt_at=int(time.time()) if now is None else now,
                                             in_progress=True))

    def _update(self, name, update):
        with self.db.get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT data FROM group_sync_status WHERE instance_id=? AND chat_name=?",
                               (self.instance_id, name)).fetchone()
            if row:
                state = json.loads(row[0])
                update(state)
                self._save(conn, name, state)

    def finish(self, name, result, now=None):
        now = int(time.time()) if now is None else now
        def update(s):
            s.update(result, in_progress=False, last_finished_at=now)
            if result.get("sync_success"):
                s["last_success_at"] = now
            if result["outcome"] == "success":
                s.update(last_complete_at=now, consecutive_failures=0,
                         consecutive_successes=s["consecutive_successes"] + 1)
            elif result["outcome"] == "failed":
                s.update(consecutive_failures=s["consecutive_failures"] + 1, consecutive_successes=0)
            else:
                s["consecutive_successes"] = 0
        self._update(name, update)

    def snapshot(self, now=None):
        now = int(time.time()) if now is None else now
        with self.db.get_connection() as conn:
            rows = conn.execute("SELECT chat_name,data FROM group_sync_status WHERE instance_id=?",
                                (self.instance_id,)).fetchall()
        result = []
        for name, data in rows:
            state = json.loads(data)
            age = max(0, now - (state["last_complete_at"] or state["created_at"]))
            level = "critical" if age >= 600 else "warning" if age >= 180 else "ok"
            if state["alert_level"] != "ok" and state["consecutive_successes"] < 2:
                level = max((level, state["alert_level"]), key={"ok": 0, "warning": 1, "critical": 2}.get)
            result.append(dict(state, chat_name=name, instance_id=self.instance_id,
                               stale_seconds=age, effective_level=level))
        return result

    def evaluate(self, now=None):
        events = []
        for state in self.snapshot(now):
            level = state["effective_level"]
            if level == state["alert_level"]:
                continue
            self._update(state["chat_name"], lambda s: s.update(alert_level=level))
            events.append(dict(type="sync_recovered" if level == "ok" else "sync_alert",
                               data=dict(state, alert_level=level)))
        return events
