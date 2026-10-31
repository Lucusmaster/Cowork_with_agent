"""归档 / 删除 / 回收站还原 / 改标题 的存储层测试。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def _row(self, session_id: str) -> dict:
        """索引里的一条会话（不存在就失败）。"""
        for item in session.sessions():
            if item["id"] == session_id:
                return item
        self.fail(f"索引里没有会话：{session_id}")

    def _trash_name(self, session_id: str) -> str:
        """删掉会话，返回回收站里的文件名。"""
        return Path(session.delete(session_id)["trash"]).name

    def _old_trash(self, session_id: str = "chat-007",
                   contents: tuple[str, ...] = ("老会话的第一句", "老会话的第二句")) -> str:
        """手搓一个 0.9.3～0.9.5 时代那种「没有 sidecar」的回收站文件。"""
        trash = session._trash_dir()
        trash.mkdir(parents=True, exist_ok=True)
        path = trash / f"{session_id}-20260101-010101.jsonl"
        records = [
            {"id": f"msg-{index + 1:03d}", "session_id": session_id, "role": "user",
             "agent": "user", "content": text, "created_at": f"2026-01-01T01:01:0{index + 1}"}
            for index, text in enumerate(contents)
        ]
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
                        encoding="utf-8")
        return path.name


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


class TrashListTests(SessionOpsBase):
    def test_list_trash_empty(self) -> None:
        self.assertEqual(session.list_trash(), [])

    def test_list_trash_shows_deleted_session(self) -> None:
        a = self._make("会话 A")
        session.append(a["id"], role="assistant", agent="coordinator", content="收到")
        session.delete(a["id"])
        rows = session.list_trash()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertTrue(row["trash_name"].endswith(".jsonl"))
        self.assertEqual(row["session_id"], a["id"])
        self.assertEqual(row["title"], "会话 A")
        self.assertEqual(row["message_count"], 2)
        self.assertTrue(row["deleted_at"])

    def test_list_trash_keeps_workspace(self) -> None:
        entry = session.create_session(workspace="E:\\proj", title="带工作区的")
        session.append(entry["id"], role="user", agent="user", content="你好")
        session.delete(entry["id"])
        self.assertEqual(session.list_trash()[0]["workspace"], "E:\\proj")

    def test_delete_writes_sidecar_snapshot(self) -> None:
        a = self._make("会话 A")
        session.archive(a["id"])                     # 归档状态也要记进快照
        trash_path = Path(session.delete(a["id"])["trash"])
        sidecar = trash_path.with_suffix(".json")
        self.assertTrue(sidecar.is_file(), "删除时应该在回收站留一份 sidecar")
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(data["id"], a["id"])
        self.assertEqual(data["title"], "会话 A")
        self.assertEqual(data["message_count"], 1)
        self.assertTrue(data["archived_at"])
        self.assertTrue(data["created_at"])
        self.assertTrue(data["deleted_at"])
        self.assertIn("workspace", data)
        self.assertFalse(data["title_locked"], "没改过标题的会话要记成未锁定")

    def test_sidecar_failure_does_not_break_delete(self) -> None:
        a = self._make("会话 A")
        real = session._atomic_write

        def flaky(path: Path, text: str) -> None:
            # 只让 sidecar 写失败，索引照常写
            if path.suffix == ".json" and path.parent == session._trash_dir():
                raise OSError("快照写不进去")
            real(path, text)

        with mock.patch.object(session, "_atomic_write", flaky):
            result = session.delete(a["id"])
        self.assertTrue(result["trash"], "删除本身不能受快照影响")
        self.assertTrue(Path(result["trash"]).exists())
        self.assertNotIn(a["id"], [item["id"] for item in session.sessions()])

    def test_list_trash_falls_back_without_sidecar(self) -> None:
        self._old_trash()
        rows = session.list_trash()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["session_id"], "chat-007")
        self.assertEqual(row["title"], "老会话的第一句")
        self.assertEqual(row["message_count"], 2)
        self.assertTrue(row["deleted_at"], "没有 sidecar 时用文件 mtime 当删除时间")
        self.assertEqual(row["workspace"], "")

    def test_list_trash_sorted_by_deleted_at_desc(self) -> None:
        a = self._make("先删的")
        b = self._make("后删的")
        session.delete(a["id"])
        session.delete(b["id"])
        # 两次删除可能落在同一秒，把 b 的快照时间改老，排序才有确定性
        row_b = [row for row in session.list_trash() if row["session_id"] == b["id"]][0]
        sidecar = (session._trash_dir() / row_b["trash_name"]).with_suffix(".json")
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        data["deleted_at"] = "2020-01-01T00:00:00"
        sidecar.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        rows = session.list_trash()
        self.assertEqual([row["session_id"] for row in rows], [a["id"], b["id"]])

    def test_list_trash_skips_bad_files(self) -> None:
        a = self._make("会话 A")
        session.delete(a["id"])
        trash = session._trash_dir()
        (trash / "坏文件.jsonl").write_text("{不是 json}\n", encoding="utf-8")
        (trash / "chat-900-20260101-000000.jsonl").write_text("", encoding="utf-8")
        rows = session.list_trash()
        ids = [row["session_id"] for row in rows]
        self.assertIn(a["id"], ids, "一个坏文件不能连累别的")
        self.assertNotIn("坏文件", ids, "名字不合法的文件要跳过")
        empty = [row for row in rows if row["session_id"] == "chat-900"][0]
        self.assertEqual(empty["message_count"], 0)
        self.assertEqual(empty["title"], session.DEFAULT_TITLE)

    def test_list_trash_does_not_read_body_when_sidecar_complete(self) -> None:
        a = self._make("会话 A")
        session.delete(a["id"])
        real = session._read_json_lines
        calls: list[Path] = []

        def spy(path: Path) -> list[dict]:
            calls.append(path)
            return real(path)

        with mock.patch.object(session, "_read_json_lines", spy):
            rows = session.list_trash()
        self.assertEqual(calls, [], "sidecar 齐全时不该读 jsonl 正文（state 每秒都会调）")
        self.assertEqual(rows[0]["title"], "会话 A")
        self.assertEqual(rows[0]["message_count"], 1)

    def test_list_trash_reads_body_once_when_sidecar_incomplete(self) -> None:
        a = self._make("会话 A")
        session.delete(a["id"])
        row = session.list_trash()[0]
        sidecar = (session._trash_dir() / row["trash_name"]).with_suffix(".json")
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        data.pop("message_count")                     # 缺条数 → 必须读一次正文补齐
        sidecar.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        real = session._read_json_lines
        calls: list[Path] = []

        def spy(path: Path) -> list[dict]:
            calls.append(path)
            return real(path)

        with mock.patch.object(session, "_read_json_lines", spy):
            rows = session.list_trash()
        self.assertEqual(len(calls), 1, "缺字段时只读一次正文，标题和条数一起补")
        self.assertEqual(rows[0]["message_count"], 1)
        self.assertEqual(rows[0]["title"], "会话 A")


class RestoreTests(SessionOpsBase):
    def test_restore_roundtrip_keeps_messages(self) -> None:
        a = self._make("要被还原的")
        session.append(a["id"], role="assistant", agent="coordinator", content="收到")
        before = len(session.messages(a["id"]))
        trash_name = self._trash_name(a["id"])
        restored = session.restore(trash_name)
        self.assertEqual(restored["id"], a["id"])
        self.assertEqual(restored["trash_name"], trash_name)
        self.assertEqual(restored["message_count"], before)
        self.assertEqual(len(session.messages(a["id"])), before, "还原不能丢消息")
        self.assertIn(a["id"], [item["id"] for item in session.sessions()])
        self.assertEqual(session.active_session()["id"], a["id"], "还原的会话应该接手活动会话")
        self.assertFalse((session._trash_dir() / trash_name).exists(), "搬回来就不该留在回收站")
        self.assertFalse((session._trash_dir() / trash_name).with_suffix(".json").exists(),
                         "sidecar 用完要清掉")
        self.assertEqual(session.list_trash(), [])

    def test_restore_then_append(self) -> None:
        a = self._make("会话 A")
        trash_name = self._trash_name(a["id"])
        session.restore(trash_name)
        message = session.append(a["id"], role="user", agent="user", content="还原后继续说")
        self.assertEqual(message["session_id"], a["id"])
        self.assertEqual(len(session.messages(a["id"])), 2)

    def test_restore_legacy_trash_without_sidecar(self) -> None:
        trash_name = self._old_trash()
        restored = session.restore(trash_name)
        self.assertEqual(restored["id"], "chat-007")
        self.assertEqual(restored["title"], "老会话的第一句")
        self.assertEqual(restored["message_count"], 2)
        self.assertEqual(len(session.messages("chat-007")), 2)
        session.append("chat-007", role="user", agent="user", content="继续")
        self.assertEqual(len(session.messages("chat-007")), 3)

    def test_restore_id_conflict_reassigns_and_keeps_messages(self) -> None:
        a = self._make("要被还原的")
        for index in range(2):
            session.append(a["id"], role="user", agent="user", content=f"补充 {index}")
        before = len(session.messages(a["id"]))
        trash_name = self._trash_name(a["id"])
        reused = session.create_session(title="删完又新建的")     # 新建会复用同一个 chat-NNN
        self.assertEqual(reused["id"], a["id"], "前提：新建会话占住了原 id")
        restored = session.restore(trash_name)
        self.assertNotEqual(restored["id"], a["id"], "id 冲突必须换新 id")
        self.assertEqual(restored.get("restored_from"), a["id"])
        self.assertEqual(len(session.messages(restored["id"])), before, "换 id 也不能丢消息")
        self.assertEqual(len(session.messages(reused["id"])), 0, "新会话的消息不能被顶掉")
        self.assertTrue(session._session_file(restored["id"]).is_file())
        ids = [item["id"] for item in session.sessions()]
        self.assertIn(reused["id"], ids)
        self.assertIn(restored["id"], ids)

    def test_restore_keeps_archived_state(self) -> None:
        a = self._make("归档后删的")
        b = self._make("留着的")                 # b 是当前活动会话
        session.archive(a["id"])
        trash_name = self._trash_name(a["id"])
        restored = session.restore(trash_name)
        self.assertTrue(restored["archived_at"], "归档状态要照 sidecar 还原")
        self.assertTrue(self._row(restored["id"])["archived_at"])
        self.assertEqual(session.active_session()["id"], b["id"],
                         "归档会话还原后 active 不该变")

    def test_restore_unarchived_switches_active(self) -> None:
        a = self._make("会话 A")
        self._make("会话 B")                     # b 是当前活动会话
        session.set_active(a["id"])
        trash_name = self._trash_name(a["id"])   # a 删掉后 active 落到 b
        restored = session.restore(trash_name)
        self.assertEqual(restored["id"], a["id"])
        self.assertEqual(session.active_session()["id"], a["id"], "还原没归档的会话要切成活动")

    def test_restore_keeps_renamed_title_and_lock(self) -> None:
        a = self._make("会话 A")
        session.rename(a["id"], "人工标题")
        trash_name = self._trash_name(a["id"])
        restored = session.restore(trash_name)
        self.assertEqual(restored["title"], "人工标题")
        self.assertTrue(restored["title_locked"], "title_locked 要跟着 sidecar 一起还原")
        session.append(restored["id"], role="user", agent="user", content="还原后的第一条消息")
        self.assertEqual(self._row(restored["id"])["title"], "人工标题",
                         "还原之后也不许被首条消息改标题")

    def test_restore_keeps_lock_when_title_was_default(self) -> None:
        entry = session.create_session()                       # 默认标题「新会话」
        session.rename(entry["id"], session.DEFAULT_TITLE)      # 用户手改回默认标题
        trash_name = self._trash_name(entry["id"])
        restored = session.restore(trash_name)
        self.assertTrue(restored["title_locked"])
        session.append(entry["id"], role="user", agent="user", content="不该拿这句当标题")
        self.assertEqual(self._row(entry["id"])["title"], session.DEFAULT_TITLE,
                         "还原后也要保留锁定，不能被自动标题冲掉")

    def test_restore_rejects_path_traversal(self) -> None:
        a = self._make("会话 A")
        trash_name = self._trash_name(a["id"])
        bad_names = [
            "../" + trash_name,
            "..\\" + trash_name,
            "trash/" + trash_name,
            "sessions/../trash/" + trash_name,
            str(session._trash_dir() / trash_name),
            "..",
            "",
            "   ",
            trash_name.replace(".jsonl", ".json"),
        ]
        for bad in bad_names:
            with self.assertRaises(ValueError, msg=f"应该拒绝：{bad!r}"):
                session.restore(bad)
        self.assertTrue((session._trash_dir() / trash_name).is_file(), "被拒之后文件要原样留着")
        # 非法名都挡住之后，正常名字仍然能还原
        self.assertEqual(session.restore(trash_name)["id"], a["id"])

    def test_restore_missing_file(self) -> None:
        with self.assertRaises(ValueError):
            session.restore("chat-999-20260101-010101.jsonl")


class RenameTests(SessionOpsBase):
    def test_rename_updates_index_title(self) -> None:
        a = self._make("会话 A")
        entry = session.rename(a["id"], "  新的标题  ")
        self.assertEqual(entry["title"], "新的标题")
        self.assertTrue(entry["title_locked"])
        self.assertEqual(self._row(a["id"])["title"], "新的标题")

    def test_rename_truncates_to_limit(self) -> None:
        a = self._make("会话 A")
        entry = session.rename(a["id"], "长" * 200)
        self.assertEqual(entry["title"], "长" * 60)
        self.assertEqual(len(entry["title"]), 60)

    def test_rename_empty_title_rejected(self) -> None:
        a = self._make("会话 A")
        for bad in ("", "   ", "\n\t "):
            with self.assertRaises(ValueError) as ctx:
                session.rename(a["id"], bad)
            self.assertIn("标题不能为空", str(ctx.exception))
        self.assertEqual(self._row(a["id"])["title"], "会话 A", "被拒之后标题不该变")

    def test_rename_missing_session(self) -> None:
        with self.assertRaises(ValueError):
            session.rename("chat-999", "随便写点")
        self.assertEqual([item["id"] for item in session.sessions()], [])

    def test_append_after_rename_keeps_title(self) -> None:
        entry = session.create_session()                     # 默认标题「新会话」
        session.rename(entry["id"], "人工标题")
        session.append(entry["id"], role="user", agent="user", content="第一条用户消息")
        self.assertEqual(self._row(entry["id"])["title"], "人工标题",
                         "rename 之后 append 不许把标题改回去")

    def test_rename_to_default_title_stays_locked(self) -> None:
        entry = session.create_session()
        session.rename(entry["id"], session.DEFAULT_TITLE)
        session.append(entry["id"], role="user", agent="user", content="不该拿这句当标题")
        self.assertEqual(self._row(entry["id"])["title"], session.DEFAULT_TITLE,
                         "用户手动改回默认标题也算数，不能被自动标题冲掉")

    def test_auto_retitle_still_works_without_rename(self) -> None:
        entry = session.create_session()
        session.append(entry["id"], role="user", agent="user", content="自动标题")
        self.assertEqual(self._row(entry["id"])["title"], "自动标题", "没 rename 过的行为不能变")


class EmptyTrashTests(SessionOpsBase):
    """清空回收站：只删回收站里的东西，别的一律不动。"""

    def test_empty_trash_removes_files_and_sidecars(self) -> None:
        a = self._make("会话 A")
        b = self._make("会话 B")
        session.delete(a["id"])
        session.delete(b["id"])
        trash_dir = session._trash_dir()
        self.assertEqual(len(list(trash_dir.glob("*.jsonl"))), 2)
        self.assertEqual(len(list(trash_dir.glob("*.json"))), 2, "两条都该有 sidecar")

        result = session.empty_trash()

        self.assertEqual(result["removed"], 2)
        self.assertEqual(result["failed"], [])
        self.assertEqual(list(trash_dir.glob("*.jsonl")), [], "jsonl 要全删掉")
        self.assertEqual(list(trash_dir.glob("*.json")), [], "sidecar 不能留下来当垃圾")
        self.assertEqual(session.list_trash(), [])

    def test_empty_trash_on_empty_trash_is_noop(self) -> None:
        for _ in range(2):                       # 空的时候连点两次都不许炸
            result = session.empty_trash()
            self.assertEqual(result["removed"], 0)
            self.assertEqual(result["failed"], [])

    def test_empty_trash_keeps_live_sessions(self) -> None:
        a = self._make("留下的")
        b = self._make("要清掉的")
        session.delete(b["id"])
        session.empty_trash()
        ids = [item["id"] for item in session.sessions()]
        self.assertIn(a["id"], ids, "没删过的会话不能被牵连")
        self.assertEqual(len(session.messages(a["id"])), 1)
        self.assertTrue(session._session_file(a["id"]).is_file())

    def test_empty_trash_also_clears_legacy_rows_without_sidecar(self) -> None:
        self._old_trash()
        self.assertEqual(len(session.list_trash()), 1)
        self.assertEqual(session.empty_trash()["removed"], 1)
        self.assertEqual(session.list_trash(), [])

    def test_empty_trash_leaves_foreign_files_alone(self) -> None:
        a = self._make("会话 A")
        session.delete(a["id"])
        outside = session.ROOM / "sessions" / "keep-me.jsonl"
        outside.write_text('{"id":"x"}\n', encoding="utf-8")
        session.empty_trash()
        self.assertTrue(outside.is_file(), "回收站之外的目录不允许被扫到")

    def test_empty_trash_survives_undeletable_file(self) -> None:
        a = self._make("会话 A")
        b = self._make("会话 B")
        session.delete(a["id"])
        session.delete(b["id"])
        real_unlink = Path.unlink
        locked = f"{a['id']}-"

        def picky(path, *args, **kwargs):          # 只让 a 的 jsonl 删不掉（模拟被占用）
            if path.suffix == ".jsonl" and path.name.startswith(locked):
                raise PermissionError("被占用了")
            return real_unlink(path, *args, **kwargs)

        with mock.patch.object(Path, "unlink", picky):
            result = session.empty_trash()
        self.assertEqual(len(result["failed"]), 1, "删不掉的文件要报出来，不能静默")
        self.assertEqual(result["removed"], 1, "另外那条照删，一个坏文件不能把清空全卡住")
        self.assertEqual(len(session.list_trash()), 1, "删不掉的那条还留在回收站里")


if __name__ == "__main__":
    unittest.main()