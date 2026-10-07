import logging
import re
import time
import xml.etree.ElementTree as ET

from ..db.group_sync import overlap, checkpoint_position

logger = logging.getLogger(__name__)


def find_chat_result(xml, name):
    """Select an exact result inside Chats, excluding the duplicate Groups card."""
    nodes = list(ET.fromstring(xml).iter())
    pattern = re.escape(name) + r"(?:\s*[（(]\d+[）)])?"
    section = None
    matches = []
    for node in nodes:
        text = node.get("text", "").strip()
        if re.fullmatch(r"(?:Chats|聊天|トーク)\s*\d+", text, re.I):
            section = "chats"
        elif re.fullmatch(r"(?:Messages|Groups|Friends|訊息|群組|好友|メッセージ|グループ|友だち)\s*\d+", text, re.I):
            section = "other"
        if section == "chats" and node.get("class") == "android.widget.TextView" and re.fullmatch(pattern, text):
            bounds = [int(v) for v in re.findall(r"\d+", node.get("bounds", ""))]
            if len(bounds) == 4 and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                matches.append(((bounds[0] + bounds[2]) // 2, (bounds[1] + bounds[3]) // 2))
    return matches[0] if len(matches) == 1 else None


def parse_messages(xml, name):
    root = ET.fromstring(xml)
    items = []
    # Only leaf message containers: LINE may nest both known container types.
    def is_container(node):
        return any(key in node.get("resource-id", "") for key in
                   ("chat_ui_row_contentview_container", "chat_ui_row_swipeable_framelayout"))
    for container in root.iter():
        if not is_container(container) or any(is_container(n) for n in list(container.iter())[1:]):
            continue
        content, stamp, sender, own = "", "", "", False
        for node in container.iter():
            rid, text = node.get("resource-id", ""), node.get("text", "").strip()
            if "chat_ui_message_text" in rid:
                content = text
            elif "chat_ui_row_timestamp" in rid:
                stamp = text
            elif "chat_ui_row_thumbnail" in rid:
                sender = node.get("content-desc", "").replace("'s profile", "").replace("的個人檔案", "").strip()
            elif "chat_ui_row_read_count" in rid:
                own = True
        if content:
            items.append(dict(content=content, msg_time=stamp, sender_name=sender or ("Me" if own else name)))
    return items


class GroupWatcher:
    def __init__(self, device_mgr, store, callback, interrupted):
        self.device_mgr, self.store = device_mgr, store
        self.callback, self.interrupted = callback, interrupted

    def scan(self, name, revision):
        started = time.monotonic()
        self.result = dict(outcome="failed", stage="device", reason=None,
                           sync_success=False, inserted=0)
        try:
            self._scan(name, revision)
        except Exception:
            logger.exception("Scan failed at %s for %s", self.result["stage"], name)
            self.result["outcome"] = "failed"
            self.result["reason"] = self.result["stage"] + "_error"
        if self.interrupted() and not self.result["sync_success"]:
            self.result.update(outcome="cancelled", reason="interrupted")
        if self.result["outcome"] == "failed" and not self.result["reason"]:
            self.result["reason"] = self.result["stage"] + "_failed"
        self.result["duration_sec"] = round(time.monotonic() - started, 3)
        return self.result

    def _latest_window(self, d, name):
        """Use the list's accessibility scroll result, not screen stability alone."""
        package = self.device_mgr.package_name
        listing = d(resourceId=package + ":id/chathistory_message_list")
        newest = d(resourceId=package + ":id/chat_ui_scroll_to_bottom_button")
        if newest.exists:
            newest.click()
            time.sleep(.5)
        stable_bottom = 0
        for _ in range(12):
            if self.interrupted():
                return None
            before = d.dump_hierarchy()
            if not listing.exists:
                return None
            # Android's scrollForward reports whether the actual list can scroll.
            moved = listing.scroll.forward(steps=30)
            time.sleep(.3)
            after = d.dump_hierarchy()
            root = ET.fromstring(after)
            lists = [n for n in root.iter() if n.get("resource-id") == package + ":id/chathistory_message_list"]
            old_lists = [n for n in ET.fromstring(before).iter()
                         if n.get("resource-id") == package + ":id/chathistory_message_list"]
            unchanged = (len(lists) == len(old_lists) == 1 and
                         ET.tostring(lists[0]) == ET.tostring(old_lists[0]))
            title = d(resourceId=package + ":id/header_title")
            valid_title = title.exists and re.fullmatch(
                re.escape(name) + r"(?:\s*[（(]\d+[）)])?", title.get_text() or "")
            if moved is False and unchanged and not newest.exists and valid_title:
                stable_bottom += 1
                if stable_bottom >= 2:
                    return parse_messages(after, name)
            else:
                stable_bottom = 0
        return None

    def _scan(self, name, revision):
        """Bounded scan, with cooperative interruption between UI operations."""
        mgr = self.device_mgr
        try:
            mgr.ensure_app_running()
            d = mgr.d
            if not d or self.interrupted():
                return
            self.result["stage"] = "return_to_main"
            if not mgr.safe_back_to_main(max_tries=5):
                logger.warning("Skipping scan: Chats page not verified for %s", name)
                return
            self.result["stage"] = "search"
            search = d(resourceId=mgr.package_name + ":id/main_tab_search_bar")
            if not search.exists or self.interrupted():
                return
            search.click()
            field = d(className="android.widget.EditText",
                      resourceId=mgr.package_name + ":id/input_text")
            if not field.exists(timeout=2):
                logger.warning("Search input not verified for %s", name)
                return
            if d(resourceIdMatches=".*(chat_ui_message_edit|chathistory_message_edit).*").exists:
                logger.warning("Refusing search in a conversation: %s", name)
                return
            field.set_text(name)
            time.sleep(1)
            point = None
            for _ in range(6):
                if self.interrupted():
                    return
                point = find_chat_result(d.dump_hierarchy(), name)
                if point:
                    break
                time.sleep(.3)
            if point is None:
                logger.warning("Group missing or ambiguous in Chats search section: %s", name)
                return
            if self.interrupted():
                return
            self.result["stage"] = "enter_conversation"
            d.click(*point)
            edit = d(className="android.widget.EditText", resourceIdMatches=".*(chat_ui_message_edit|chathistory_message_edit).*")
            if not edit.exists(timeout=3):
                return
            title = d(resourceIdMatches=".*header_title.*")
            if not title.exists or not re.fullmatch(re.escape(name) + r"(?:\s*[（(]\d+[）)])?", title.get_text() or ""):
                logger.warning("Group title verification failed: %s", name)
                return
            logger.info("Verified conversation opened: %s", name)
            # 自動清理殘留的群組名稱草稿 (若先前因搜尋 bug 將群組名誤填入對話框)
            if edit.exists:
                draft_text = (edit.get_text() or "").strip()
                if draft_text and (draft_text == name.strip() or draft_text in name):
                    logger.info("Cleaning up leftover group name draft in %s: %s", name, draft_text)
                    try:
                        edit.clear_text()
                    except Exception as err:
                        logger.warning("Failed to clear leftover draft: %s", err)
            self.result["stage"] = "verify_latest"
            snapshot = self._latest_window(d, name)
            if snapshot is None:
                return
            if not snapshot:
                self.result["reason"] = "no_readable_messages"
                return
            width, height = d.window_size()
            self.result["stage"] = "backfill"
            checkpoint = self.store.checkpoint(name)
            for page in range(40):
                if self.interrupted():
                    return
                if checkpoint is None or checkpoint_position(checkpoint, snapshot, name) is not None:
                    break
                d.swipe(width // 2, int(height * .35), width // 2, int(height * .58), .2)
                time.sleep(.3)
                older = parse_messages(d.dump_hierarchy(), name)
                join = overlap(older, snapshot, name)
                if not older:
                    continue
                if join is None:
                    logger.warning("Cannot stitch group page %d for %s (older=%d, newer=%d)",
                                   page, name, len(older), len(snapshot))
                    break
                # Preserve complete timestamps/avatars from the newer page when
                # its overlapping bubble is clipped at the bottom of the older page.
                shared = min(len(older), join)
                for offset in range(1, shared + 1):
                    old_item, new_item = older[-offset], snapshot[join-offset]
                    if overlap([old_item], [new_item], name) is None:
                        break
                    if not old_item["msg_time"]:
                        old_item["msg_time"] = new_item["msg_time"]
                    if old_item["sender_name"] == name:
                        old_item["sender_name"] = new_item["sender_name"]
                snapshot = older + snapshot[join:]
            if self.interrupted():
                return
            if (checkpoint and overlap(checkpoint, snapshot, name) is None
                    and checkpoint_position(checkpoint, snapshot, name) is not None):
                logger.warning("Recovering missing checkpoint tail using unique multi-message anchor: %s", name)
            self.result["stage"] = "commit"
            status, messages = self.store.commit(name, revision, snapshot)
            self.result.update(outcome="success" if status in ("synced", "baseline") else
                               "cancelled" if status == "stale" else "failed",
                               reason=status, sync_success=status in ("synced", "baseline"),
                               inserted=len(messages))
            logger.log(logging.WARNING if status == "gap_detected" else logging.INFO,
                       "Group sync %s: %s, inserted=%d", name, status, len(messages))
            for message in messages:
                if self.callback:
                    self.result["stage"] = "broadcast"
                    self.callback(message)
        finally:
            if not mgr.safe_back_to_main(max_tries=5):
                self.result.update(outcome="failed", stage="cleanup", reason="return_to_main_failed")
                logger.warning("Scan cleanup failed to return to Chats: %s", name)
            else:
                logger.info("Verified return to Chats after scan: %s", name)
