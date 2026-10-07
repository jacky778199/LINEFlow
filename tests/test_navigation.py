import unittest
from unittest.mock import Mock, patch

from lineflow.adb.device import AndroidDeviceManager
from lineflow.adb.group_watcher import GroupWatcher


class NavigationTests(unittest.TestCase):
    @patch("lineflow.adb.device.time.sleep")
    def test_main_activity_chat_still_requires_back(self, sleep):
        mgr = AndroidDeviceManager("test")
        d = mgr.d = Mock()
        d.app_current.return_value = {"package": mgr.package_name, "activity": "MainActivity"}
        state = {"chat": True}
        d.press.side_effect = lambda key: state.update(chat=False)

        def select(**args):
            rid = args.get("resourceId", "")
            exists = (not state["chat"] if rid else state["chat"])
            return Mock(exists=exists)

        d.side_effect = select
        self.assertTrue(mgr.safe_back_to_main())
        d.press.assert_called_once_with("back")

    @patch("lineflow.adb.device.time.sleep")
    def test_navigation_failure_is_bounded(self, sleep):
        mgr = AndroidDeviceManager("test")
        mgr.d = Mock()
        mgr.d.app_current.return_value = {"package": mgr.package_name}
        mgr.d.return_value.exists = False
        self.assertFalse(mgr.safe_back_to_main(max_tries=2))
        self.assertEqual(mgr.d.press.call_count, 2)

    def test_failed_return_prevents_search_and_still_cleans_up(self):
        mgr, store = Mock(), Mock()
        mgr.safe_back_to_main.return_value = False
        GroupWatcher(mgr, store, None, lambda: False).scan("Group", 1)
        mgr.d.assert_not_called()
        store.commit.assert_not_called()
        self.assertEqual(mgr.safe_back_to_main.call_count, 2)

    def test_sender_refuses_when_not_returned_to_main(self):
        from lineflow.adb.sender import LineSender
        mgr = Mock()
        mgr.safe_back_to_main.return_value = False
        sender = LineSender(mgr)
        ok, err = sender.send_message("TargetGroup", "Hello")
        self.assertFalse(ok)
        self.assertIn("Failed to navigate back to Chats tab", err)
        mgr.safe_back_to_main.assert_called_once_with(max_tries=5)

    def test_sender_aborts_search_when_in_conversation(self):
        from lineflow.adb.sender import LineSender
        mgr = Mock()
        mgr.safe_back_to_main.return_value = True
        mgr.package_name = "jp.naver.line.android"
        d = mgr.d = Mock()
        # search_bar exists
        d.return_value.exists = True
        # but chat edit box exists during search step
        def selector(**kwargs):
            m = Mock()
            if "resourceIdMatches" in kwargs and "chat_ui_message_edit" in kwargs["resourceIdMatches"]:
                m.exists = True
            elif "main_tab_search_bar" in kwargs.get("resourceId", ""):
                m.exists = True
            else:
                m.exists = False
            return m
        d.side_effect = selector

        sender = LineSender(mgr)
        ok, err = sender.send_message("TargetGroup", "Hello")
        self.assertFalse(ok)
        self.assertIn("inside conversation", err)

