import os
import tempfile
from lineflow.config import load_config
from lineflow.db.database import Database
from lineflow.db.repository import MessageRepository

def test_config():
    os.environ.setdefault("LINEFLOW_AUTH_TOKEN", "test_mock_token_for_unit_tests")
    cfg = load_config("config.yaml")
    assert cfg.server.port == 8000
    assert "instance1" in cfg.instances
    print("✓ Config loading test passed")

def test_database():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        tmp_path = f.name

    try:
        db = Database(tmp_path)
        repo = MessageRepository(db)

        # 1. 測試寫入新訊息 (包含 msg_time)
        msg1 = repo.insert_message(
            instance_id="instance1",
            chat_type="group",
            chat_name="業務交流群",
            sender_name="陳小華",
            content="請問報價單？",
            msg_time="10:42 AM",
            timestamp=1700000000
        )
        assert msg1 is not None
        assert msg1["seq_id"] == 1
        assert msg1["msg_time"] == "10:42 AM"
        print("✓ Message insertion with seq_id and msg_time test passed")

        # 2. 測試指紋去重 (重複寫入應返回 None)
        dup = repo.insert_message(
            instance_id="instance1",
            chat_type="group",
            chat_name="業務交流群",
            sender_name="陳小華",
            content="請問報價單？",
            msg_time="10:42 AM",
            timestamp=1700000000
        )
        assert dup is None
        print("✓ Hash deduplication test passed")

        # 3. 測試寫入第二則訊息
        msg2 = repo.insert_message(
            instance_id="instance1",
            chat_type="group",
            chat_name="業務交流群",
            sender_name="李大明",
            content="我也想了解",
            msg_time="10:45 AM",
            timestamp=1700000010
        )
        assert msg2 is not None
        assert msg2["seq_id"] == 2
        assert msg2["msg_time"] == "10:45 AM"

        # 4. 測試斷線補償查詢 (since_seq_id = 1 應只查出 seq_id = 2)
        synced = repo.get_messages_since(since_seq_id=1, instance_id="instance1")
        assert len(synced) == 1
        assert synced[0]["seq_id"] == 2
        assert synced[0]["content"] == "我也想了解"
        assert synced[0]["msg_time"] == "10:45 AM"
        print("✓ Sync query test passed")

    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

if __name__ == "__main__":
    test_config()
    test_database()
    print("\n🎉 ALL TESTS PASSED!")
