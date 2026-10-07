import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from lineflow.adb.group_watcher import parse_messages, find_chat_result
from lineflow.config import AppConfig, ServerConfig
from lineflow.db.database import Database
from lineflow.db.group_sync import GroupSyncStore
from lineflow.db.sync_status import SyncStatusStore
from lineflow.db.repository import MessageRepository
from lineflow.ws.connection import ConnectionManager
from lineflow.ws.router import create_ws_router


class GroupApiTests(unittest.TestCase):
    def test_search_duplicate_across_sections_is_not_ambiguous(self):
        xml = '''<hierarchy>
        <node text="Chats 1"/><node class="android.widget.TextView" text="Group (90)" bounds="[100,300][600,400]"/>
        <node text="Messages 1"/><node class="android.widget.TextView" text="Group" bounds="[100,500][600,600]"/>
        <node text="Groups 1"/><node class="android.widget.TextView" text="Group (90)" bounds="[100,700][600,800]"/>
        </hierarchy>'''
        self.assertEqual(find_chat_result(xml, "Group"), (350, 350))
        self.assertIsNone(find_chat_result(xml, "Other"))
        duplicate = xml.replace('<node text="Messages 1"/>', '')
        self.assertIsNone(find_chat_result(duplicate, "Group"))

    def test_authenticated_get_set_validation_and_instance_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(str(Path(directory) / "test.db"))
            stores = {name: GroupSyncStore(db, name) for name in ("one", "two")}
            app = FastAPI()
            app.include_router(create_ws_router(
                AppConfig(server=ServerConfig(auth_token="test-token")), MessageRepository(db),
                {name: SimpleNamespace(group_store=store, sync_status=SyncStatusStore(db, name))
                 for name, store in stores.items()},
                ConnectionManager()))
            with TestClient(app) as client:
                with client.websocket_connect("/ws/lineflow") as ws:
                    ws.send_json({"action": "auth", "token": "wrong", "instance_id": "one"})
                    self.assertEqual(ws.receive_json()["type"], "auth_fail")
                with client.websocket_connect("/ws/lineflow") as ws:
                    ws.send_json({"action": "auth", "token": "test-token", "instance_id": "one"})
                    self.assertEqual(ws.receive_json()["type"], "auth_ok")
                    ws.send_json({"action": "set_group_whitelist", "groups": [" A ", "A"], "request_id": "set"})
                    response = ws.receive_json()
                    self.assertEqual(response["groups"], ["A"])
                    self.assertEqual(response["request_id"], "set")
                    ws.send_json({"action": "get_sync_status", "request_id": "status"})
                    health = ws.receive_json()
                    self.assertEqual(health["type"], "sync_status")
                    self.assertEqual(health["request_id"], "status")
                    self.assertEqual([g["chat_name"] for g in health["groups"]], ["A"])
                    self.assertEqual(health["thresholds"]["critical_seconds"], 600)
                    ws.send_json({"action": "set_group_whitelist", "groups": "wrong"})
                    self.assertEqual(ws.receive_json()["status"], "error")
                    ws.send_json({"action": "get_group_whitelist"})
                    self.assertEqual(ws.receive_json()["groups"], ["A"])
                    ws.send_json({"action": "set_group_whitelist", "groups": []})
                    self.assertEqual(ws.receive_json()["groups"], [])
                self.assertNotEqual(stores["two"].get()["groups"], [])
                self.assertEqual(GroupSyncStore(db, "one").get()["groups"], [])

    def test_nested_containers_only_emit_each_bubble_once(self):
        xml = '''<hierarchy><node resource-id="chat_ui_row_swipeable_framelayout">
          <node resource-id="chat_ui_row_contentview_container">
            <node resource-id="chat_ui_message_text" text="OK"/>
            <node resource-id="chat_ui_row_timestamp" text="10:01"/>
            <node resource-id="chat_ui_row_thumbnail" content-desc="Alice's profile"/>
          </node></node></hierarchy>'''
        self.assertEqual(parse_messages(xml, "Group"), [
            {"content": "OK", "msg_time": "10:01", "sender_name": "Alice"}])


if __name__ == "__main__":
    unittest.main()
