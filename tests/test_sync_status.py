import tempfile
import unittest
import json
import importlib
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock, AsyncMock, patch

from lineflow.db.database import Database
from lineflow.db.sync_status import SyncStatusStore
from lineflow.adb.group_watcher import GroupWatcher
from lineflow.ws.connection import ConnectionManager
from lineflow.config import AppConfig, DatabaseConfig


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(str(Path(self.tmp.name) / "test.db"))
        self.store = SyncStatusStore(self.db, "one")
        self.store.reconcile(["Group"], now=1000)

    def test_stuck_scan_thresholds_persistence_and_recovery(self):
        self.store.begin("Group", now=1001)
        self.assertEqual(self.store.evaluate(now=1179), [])
        self.assertEqual(self.store.evaluate(now=1180)[0]["data"]["alert_level"], "warning")
        self.assertEqual(self.store.evaluate(now=1190), [])
        restarted = SyncStatusStore(self.db, "one")
        self.assertEqual(restarted.evaluate(now=1599), [])
        self.assertEqual(restarted.evaluate(now=1600)[0]["data"]["alert_level"], "critical")
        success = dict(outcome="success", sync_success=True, reason="synced", inserted=0)
        restarted.finish("Group", success, now=1601)
        self.assertEqual(restarted.evaluate(now=1601), [])
        restarted.finish("Group", success, now=1650)
        self.assertEqual(restarted.evaluate(now=1650)[0]["type"], "sync_recovered")
        self.assertEqual(restarted.evaluate(now=1651), [])

    def test_cancel_is_not_failure_and_cleanup_failure_keeps_sync_time(self):
        self.store.finish("Group", dict(outcome="cancelled", sync_success=False), now=1010)
        self.assertEqual(self.store.snapshot(now=1010)[0]["consecutive_failures"], 0)
        self.store.finish("Group", dict(outcome="failed", sync_success=True,
                          reason="return_to_main_failed"), now=1020)
        state = self.store.snapshot(now=1020)[0]
        self.assertEqual(state["last_success_at"], 1020)
        self.assertIsNone(state["last_complete_at"])
        self.assertEqual(state["consecutive_failures"], 1)

    def test_subscription_removal_and_instance_isolation(self):
        other = SyncStatusStore(self.db, "two")
        other.reconcile(["Group"], now=1000)
        self.store.reconcile([], now=1100)
        self.assertEqual(self.store.evaluate(now=2000), [])
        self.assertEqual(len(other.snapshot(now=1100)), 1)
        self.store.reconcile(["Group"], now=2000)
        self.assertEqual(self.store.evaluate(now=2001), [])


class LatestTests(unittest.TestCase):
    def device(self, moved=False, newest=False):
        package = "jp.naver.line.android"
        xml = '''<hierarchy><node resource-id="jp.naver.line.android:id/chathistory_message_list">
          <node resource-id="chat_ui_row_contentview_container">
           <node resource-id="chat_ui_message_text" text="hello"/>
          </node></node></hierarchy>'''
        d = Mock()
        listing = Mock(exists=True)
        listing.scroll.forward.return_value = moved
        title = Mock(exists=True)
        title.get_text.return_value = "Group"
        button = Mock(exists=newest)
        d.side_effect = lambda **kw: {package + ":id/chathistory_message_list": listing,
            package + ":id/header_title": title,
            package + ":id/chat_ui_scroll_to_bottom_button": button}[kw["resourceId"]]
        d.dump_hierarchy.return_value = xml
        return d, listing, title

    @patch("lineflow.adb.group_watcher.time.sleep")
    def test_unchanged_screen_alone_is_not_bottom(self, sleep):
        watcher = GroupWatcher(Mock(package_name="jp.naver.line.android"), None, None, lambda: False)
        for moved, newest in [(True, False), (False, True), (None, False)]:
            d, _, _ = self.device(moved=moved, newest=newest)
            self.assertIsNone(watcher._latest_window(d, "Group"))

    @patch("lineflow.adb.group_watcher.time.sleep")
    def test_bottom_requires_two_confirmations_and_correct_title(self, sleep):
        watcher = GroupWatcher(Mock(package_name="jp.naver.line.android"), None, None, lambda: False)
        d, listing, title = self.device()
        self.assertEqual(watcher._latest_window(d, "Group")[0]["content"], "hello")
        self.assertEqual(listing.scroll.forward.call_count, 2)
        title.get_text.return_value = "Wrong group"
        self.assertIsNone(watcher._latest_window(d, "Group"))


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_events_are_instance_scoped(self):
        manager = ConnectionManager()
        one, two = Mock(), Mock()
        one.send_text, two.send_text = AsyncMock(), AsyncMock()
        await manager.register(one, "one")
        await manager.register(two, "two")
        for kind in ("sync_alert", "sync_recovered"):
            event = dict(type=kind, data={"chat_name": "Group"})
            await manager.broadcast_event(event, "one")
            self.assertEqual(json.loads(one.send_text.call_args.args[0]), event)
        two.send_text.assert_not_called()

    async def test_health_reports_stall_even_when_device_is_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(database=DatabaseConfig(path=str(Path(directory) / "test.db")))
            with patch("lineflow.config.load_config", return_value=config):
                main = importlib.import_module("lineflow.main")
            task = Mock()
            task.done.return_value = False
            engine = SimpleNamespace(device_mgr=Mock(), _monitor_task=task, _poll_task=task,
                                     config=SimpleNamespace(adb_serial="test"), sync_status=Mock())
            engine.device_mgr.is_alive.return_value = True
            with patch.dict(main.engines, {"test": engine}, clear=True):
                for level, expected in [("ok", 200), ("warning", 503), ("critical", 503)]:
                    engine.sync_status.snapshot.return_value = [dict(effective_level=level, last_complete_at=100)]
                    response = await main.health_check()
                    self.assertEqual(response.status_code, expected)
                    self.assertNotIn("chat_name", response.body.decode())
