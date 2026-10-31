"""归档 / 删除 的存储层测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent_team import session


class SessionOpsBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self._saved = (session.ROOM, session._LOCK)
        session.ROOM = base
        (base / "sessions").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        session.ROOM = self._saved[0]
        self.tmp.cleanup()

    def _make(self, title: str) -> dict:
        entry = session.create_session(title=title)
        session.append(entry["id"], role="user", agent="user", content=f"{title} 的第一句")
        return entry


class ArchiveTests(SessionOpsBase):
    def test_archive_marks_and_keeps_messages(self) -> None:
        a = self._make("会话 A")
        entry = session.archive(a["id"])
        self.assertTrue(entry["archived_at"])
        self.assertEqual(len(session.messages(a["id"])), 1, "归档不能删消息")

    def test_archived_session_hidden_from_active_candidates(self) -> None:
        a = self._make("会话 A")
        b = self._make("会话 B")          # b 是当前会话
        session.set_active(a["id"])
        session.archive(a["id"])          # 归档当前会话
        active = session.active_session()
        self.assertNotEqual(active["id"], a["id"], "活动会话不能停在已归档的会话上")
        self.assertEqual(active["id"], b["id"])

    def test_unarchive_restores(self) -> None:
        a = self._make("会话 A")
        session.archive(a["id"])
        entry = session.archive(a["id"], archived=False)
        self.assertEqual(entry["archived_at"], "")
        self.assertEqual(session.set_active(a["id"])["id"], a["id"])

    def test_cannot_open_archived(self) -> None:
        a = self._make("会话 A")
        session.archive(a["id"])
        with self.assertRaises(ValueError) as ctx:
            session.set_active(a["id"])
        self.assertIn("归档", str(ctx.exception))

    def test_archive_missing_session(self) -> None:
        with self.assertRaises(ValueError):
            session.archive("chat-999")

    def test_archived_flag_in_listing(self) -> None:
        a = self._make("会话 A")
        session.archive(a["id"])
        row = [item for item in session.sessions() if item["id"] == a["id"]][0]
        self.assertTrue(row["archived_at"])
        others = [item for item in session.sessions() if not item.get("archived_at")]
        self.assertNotIn(a["id"], [item["id"] for item in others])


class DeleteTests(SessionOpsBase):
    def test_delete_moves_file_to_trash(self) -> None:
        a = self._make("要删的会话")
        path = session._session_file(a["id"])
        self.assertTrue(path.exists())
        result = session.delete(a["id"])
        self.assertFalse(path.exists(), "原文件应该被挪走")
        self.assertTrue(result["trash"], "应该记录回收站路径")
        self.assertTrue(Path(result["trash"]).exists(), "回收站里要留一份")
        self.assertNotIn(a["id"], [item["id"] for item in session.sessions()])

    def test_delete_active_switches(self) -> None:
        a = self._make("会话 A")
        b = self._make("会话 B")
        session.set_active(b["id"])
        session.delete(b["id"])
        self.assertEqual(session.active_session()["id"], a["id"])

    def test_delete_last_session_is_recreateable(self) -> None:
        a = self._make("唯一的会话")
        session.delete(a["id"])
        fresh = session.active_session()
        self.assertTrue(fresh["id"].startswith("chat-"))
        self.assertEqual(session.messages(fresh["id"]), [], "新会话应该是空的")

    def test_delete_missing(self) -> None:
        with self.assertRaises(ValueError):
            session.delete("chat-999")

    def test_legacy_session_cannot_be_deleted(self) -> None:
        # 造一个 legacy 条目
        index = session._read_index()
        index["sessions"].append({"id": "legacy", "title": "旧记录", "legacy": True,
                                  "created_at": "2026-01-01T00:00:00",
                                  "updated_at": "2026-01-01T00:00:00", "message_count": 2})
        session._write_index(index)
        with self.assertRaises(ValueError) as ctx:
            session.delete("legacy")
        self.assertIn("旧会话", str(ctx.exception))

    def test_delete_keeps_other_sessions_intact(self) -> None:
        a = self._make("留着的")
        b = self._make("删掉的")
        session.delete(b["id"])
        self.assertEqual(len(session.messages(a["id"])), 1)
        ids = [item["id"] for item in session.sessions()]
        self.assertIn(a["id"], ids)
        self.assertNotIn(b["id"], ids)

    def test_index_is_clean_json_after_delete(self) -> None:
        a = self._make("会话 A")
        session.delete(a["id"])
        data = json.loads(session._index_path().read_text(encoding="utf-8"))
        self.assertIsInstance(data["sessions"], list)
        self.assertNotIn(a["id"], [item.get("id") for item in data["sessions"]])


if __name__ == "__main__":
    unittest.main()