import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

from lineflow.db.database import Database
from lineflow.db.group_sync import GroupSyncStore, overlap, checkpoint_position
from lineflow.db.repository import MessageRepository


def msg(content, stamp="10:00"):
    return dict(content=content, msg_time=stamp, sender_name="Alice")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.directory.name) / "test.db"))
        self.store = GroupSyncStore(self.db, "one")
        self.revision = self.store.set(["Group"])["revision"]

    def tearDown(self):
        self.directory.cleanup()

    def test_baseline_then_repeated_text_and_restart(self):
        self.assertEqual(self.store.commit("Group", self.revision, [msg("OK")])[0], "baseline")
        self.assertEqual(MessageRepository(self.db).get_messages_since(0), [])
        window = [msg("OK"), msg("new"), msg("OK")]
        status, rows = self.store.commit("Group", self.revision, window)
        self.assertEqual([r["content"] for r in rows], ["new", "OK"])
        restarted = GroupSyncStore(self.db, "one")
        self.assertEqual(restarted.commit("Group", self.revision, window), ("synced", []))
        self.assertEqual(len(MessageRepository(self.db).get_messages_since(0)), 2)

    def test_gap_preserves_checkpoint(self):
        self.store.commit("Group", self.revision, [msg("old")])
        self.assertEqual(self.store.commit("Group", self.revision, [msg("different")]), ("gap_detected", []))
        self.assertEqual(self.store.checkpoint("Group"), [msg("old")])

    def test_missing_tail_recovers_without_losing_history_or_duplicating(self):
        self.store.commit("Group", self.revision, [msg("first"), msg("second")])
        self.store.commit("Group", self.revision,
                          [msg("first"), msg("second"), msg("removed")])
        window = [msg("first"), msg("second"), msg("new")]
        status, rows = self.store.commit("Group", self.revision, window)
        self.assertEqual(status, "synced")
        self.assertEqual([r["content"] for r in rows], ["new"])
        self.assertEqual([r["content"] for r in MessageRepository(self.db).get_messages_since(0)],
                         ["removed", "new"])
        self.assertEqual(self.store.commit("Group", self.revision, window), ("synced", []))

    def test_missing_tail_rejects_weak_or_ambiguous_anchors(self):
        previous = [msg("a"), msg("b"), msg("removed")]
        self.assertIsNone(checkpoint_position(previous, [msg("b"), msg("new")], "Group"))
        self.assertIsNone(checkpoint_position(previous,
                          [msg("a"), msg("b"), msg("a"), msg("b")], "Group"))
        self.assertIsNone(checkpoint_position(previous,
                          [msg("a", ""), msg("b", ""), msg("new")], "Group"))
        self.assertIsNone(checkpoint_position([msg("OK"), msg("OK"), msg("removed")],
                          [msg("OK"), msg("OK"), msg("new")], "Group"))

    def test_clipped_bubble_matches_without_avatar_or_time(self):
        clipped = dict(content="same", msg_time="", sender_name="Group")
        self.assertEqual(overlap([clipped], [msg("same"), msg("new")], "Group"), 1)
        self.assertIsNone(overlap([msg("same", "09:00")], [msg("same", "10:00")], "Group"))

    def test_remove_inflight_and_readd_baselines(self):
        self.store.commit("Group", self.revision, [msg("old")])
        self.store.set([])
        self.assertEqual(self.store.commit("Group", self.revision, [msg("old"), msg("new")]), ("stale", []))
        self.assertEqual(GroupSyncStore(self.db, "one").get()["groups"], [])
        revision = self.store.set(["Group"])["revision"]
        self.assertEqual(self.store.commit("Group", revision, [msg("new")]), ("baseline", []))

    def test_validation_and_isolation(self):
        for value in (None, "Group", [""], [1], ["x" * 201]):
            with self.assertRaises(ValueError):
                self.store.set(value)
        other = GroupSyncStore(self.db, "two")
        self.store.set([])
        self.assertNotEqual(other.get()["groups"], [])

    def test_identical_repeated_message_occurrences(self):
        self.store.commit("Group", self.revision, [msg("OK")])
        _, rows = self.store.commit("Group", self.revision, [msg("OK"), msg("OK")])
        self.assertEqual(len(rows), 1)

    def test_failed_batch_rolls_back_messages_and_checkpoint(self):
        self.store.commit("Group", self.revision, [msg("old")])
        with self.assertRaises(KeyError):
            self.store.commit("Group", self.revision, [msg("old"), msg("new"), {"content": "invalid"}])
        self.assertEqual(self.store.checkpoint("Group"), [msg("old")])
        self.assertEqual(MessageRepository(self.db).get_messages_since(0), [])

    def test_reset_checkpoint_allows_clean_rebaseline(self):
        self.store.commit("Group", self.revision, [msg("old")])
        self.assertIsNotNone(self.store.checkpoint("Group"))
        self.store.reset_checkpoint("Group")
        self.assertIsNone(self.store.checkpoint("Group"))
        status, rows = self.store.commit("Group", self.revision, [msg("new_baseline")])
        self.assertEqual(status, "baseline")
        self.assertEqual(rows, [])



class PriorityTests(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_writer_precedes_background(self):
        spec = importlib.util.spec_from_file_location("mutex", Path(__file__).parents[1] / "lineflow/core/mutex.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        lock = module.UILock()
        await lock.acquire_background()
        order = []

        async def background():
            await lock.acquire_background()
            order.append("background")
            lock.release()

        async def writer():
            async with lock:
                order.append("writer")

        scan = asyncio.create_task(background())
        send = asyncio.create_task(writer())
        await asyncio.sleep(0)
        lock.release()
        await asyncio.wait_for(asyncio.gather(scan, send), timeout=1)
        self.assertEqual(order, ["writer", "background"])


if __name__ == "__main__":
    unittest.main()
