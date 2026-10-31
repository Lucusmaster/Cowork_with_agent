"""会话存储与消息协议测试（契约第 1-2 节 + room.py 旧接口兼容）。

守的是这几件事：
- 一个会话一个 JSONL，索引里能看到会话列表与活动会话；
- 新消息结构字段齐全（id/session_id/role/agent/task_id/content/artifacts/status/created_at）；
- 旧账本 room/messages.jsonl 能映射成新格式读出来，且迁移幂等、不重复、不丢消息；
- room.post() 写新结构，但 read_all()/pending()/after() 这些旧接口照旧能跑。
"""
from __future__ import annotations

import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))  # 包在仓库根
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from agent_team import room, session  # noqa: E402

NEW_MESSAGE_FIELDS = {"id", "session_id", "role", "agent", "task_id",
                      "content", "artifacts", "status", "created_at"}


class SessionTestBase(unittest.TestCase):
    """把 room/ 搬到临时目录，测试不碰真实账本。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.room_dir = self.base / "room"
        self.room_dir.mkdir()
        self._saved = [
            (session, "ROOM", session.ROOM),
            (room, "ROOM", room.ROOM),
            (room, "MESSAGES", room.MESSAGES),
            (room, "STATE", room.STATE),
        ]
        session.ROOM = self.room_dir
        room.ROOM = self.room_dir
        room.MESSAGES = self.room_dir / "messages.jsonl"
        room.STATE = self.room_dir / "state.json"

    def tearDown(self) -> None:
        for module, attr, value in self._saved:
            setattr(module, attr, value)
        self._tmp.cleanup()

    # ---------- 便捷方法 ----------
    def new_session(self, title: str = "") -> str:
        return session.create_session(str(self.base / "proj"), title)["id"]

    def add(self, session_id: str, agent: str, content: str, **kwargs) -> dict:
        role = kwargs.pop("role", "user" if agent == "user" else "assistant")
        return session.append(session_id, role=role, agent=agent, content=content, **kwargs)

    def write_legacy_ledger(self, records: list[dict]) -> Path:
        path = self.room_dir / "messages.jsonl"
        path.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records),
                        encoding="utf-8")
        return path

    def raw_index(self) -> dict:
        return json.loads((self.room_dir / "sessions" / "index.json").read_text(encoding="utf-8"))

    def raw_messages(self, session_id: str) -> list[dict]:
        path = self.room_dir / "sessions" / f"{session_id}.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()]


class CreateSessionTests(SessionTestBase):
    def test_creates_chat_001_and_file(self) -> None:
        entry = session.create_session(str(self.base / "proj"), "给登录页加记住我")
        self.assertEqual(entry["id"], "chat-001")
        self.assertEqual(entry["title"], "给登录页加记住我")
        self.assertEqual(entry["message_count"], 0)
        self.assertTrue((self.room_dir / "sessions" / "chat-001.jsonl").is_file())
        self.assertEqual(self.raw_index()["active"], "chat-001")

    def test_second_session_increments_and_becomes_active(self) -> None:
        first = session.create_session()
        second = session.create_session(title="第二个")
        self.assertEqual((first["id"], second["id"]), ("chat-001", "chat-002"))
        self.assertEqual(session.active_session()["id"], "chat-002")

    def test_active_session_creates_chat_001_once(self) -> None:
        first = session.active_session()
        second = session.active_session()
        self.assertEqual(first["id"], "chat-001")
        self.assertEqual(second["id"], "chat-001")
        self.assertEqual([item["id"] for item in session.sessions()], ["chat-001"])

    def test_set_active_switches(self) -> None:
        first = session.create_session()
        session.create_session()
        session.set_active(first["id"])
        self.assertEqual(session.active_session()["id"], first["id"])
        self.assertEqual(self.raw_index()["active"], first["id"])

    def test_set_active_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            session.set_active("chat-404")


class AppendReadTests(SessionTestBase):
    def test_append_three_agents_keeps_order_and_fields(self) -> None:
        sid = self.new_session()
        self.add(sid, "user", "给登录页加记住我")
        self.add(sid, "planner", "拆成 3 步，验收命令是 python -m unittest")
        self.add(sid, "executor", "改完了", task_id="T-261031-120000",
                 artifacts=["tasks/T-261031-120000/exec-01.last.md", "workspace:src/login.py"])

        messages = session.messages(sid)
        self.assertEqual([item["agent"] for item in messages], ["user", "planner", "executor"])
        self.assertEqual([item["role"] for item in messages], ["user", "assistant", "assistant"])
        self.assertEqual([item["id"] for item in messages], ["msg-001", "msg-002", "msg-003"])
        for message in messages:
            self.assertTrue(NEW_MESSAGE_FIELDS.issubset(message.keys()), message)
            self.assertEqual(message["session_id"], sid)
            self.assertEqual(message["status"], "complete")
        self.assertEqual(messages[0]["artifacts"], [])
        self.assertEqual(messages[2]["task_id"], "T-261031-120000")
        self.assertEqual(messages[2]["artifacts"],
                         ["tasks/T-261031-120000/exec-01.last.md", "workspace:src/login.py"])

        entry = self.raw_index()["sessions"][0]
        self.assertEqual(entry["message_count"], 3)
        # 标题自动取第一条用户消息
        self.assertEqual(entry["title"], "给登录页加记住我")

    def test_append_supports_failed_status_and_artifacts(self) -> None:
        sid = self.new_session()
        message = self.add(sid, "tester", "验收未通过：2 个用例失败", status="failed",
                           task_id="T-261031-120000",
                           artifacts=["tasks/T-261031-120000/verify-01.txt"])
        self.assertEqual(message["status"], "failed")
        stored = self.raw_messages(sid)[-1]
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["artifacts"], ["tasks/T-261031-120000/verify-01.txt"])

    def test_messages_after_id(self) -> None:
        sid = self.new_session()
        for index in range(3):
            self.add(sid, "coordinator", f"第 {index} 条")
        after = session.messages(sid, after_id="msg-001")
        self.assertEqual([item["id"] for item in after], ["msg-002", "msg-003"])
        self.assertEqual(session.messages(sid, after_id="msg-404"), [])

    def test_messages_limit_keeps_tail(self) -> None:
        sid = self.new_session()
        for index in range(5):
            self.add(sid, "coordinator", f"第 {index} 条")
        tail = session.messages(sid, limit=2)
        self.assertEqual([item["content"] for item in tail], ["第 3 条", "第 4 条"])

    def test_messages_unknown_session_is_empty(self) -> None:
        self.assertEqual(session.messages("chat-404"), [])
        self.assertEqual(session.messages("../etc/passwd"), [])

    def test_sessions_sorted_by_updated_at_desc(self) -> None:
        session.create_session(title="早")
        session.create_session(title="晚")
        index = self.raw_index()
        index["sessions"][0]["updated_at"] = "2026-10-30T09:00:00"
        index["sessions"][1]["updated_at"] = "2026-10-31T09:00:00"
        (self.room_dir / "sessions" / "index.json").write_text(
            json.dumps(index, ensure_ascii=False), encoding="utf-8")
        self.assertEqual([item["title"] for item in session.sessions()], ["晚", "早"])


class UpdateTests(SessionTestBase):
    def test_pending_becomes_complete(self) -> None:
        sid = self.new_session()
        pending = self.add(sid, "executor", "", status="pending")
        self.assertEqual(pending["status"], "pending")
        updated = session.update(sid, pending["id"], status="complete", content="改完了")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "complete")
        stored = session.messages(sid)[-1]
        self.assertEqual(stored["content"], "改完了")
        self.assertEqual(stored["id"], pending["id"])
        self.assertEqual(stored["created_at"], pending["created_at"])

    def test_update_missing_returns_none(self) -> None:
        sid = self.new_session()
        self.assertIsNone(session.update(sid, "msg-404", status="complete"))
        self.assertIsNone(session.update("chat-404", "msg-001", status="complete"))

    def test_update_ignores_protected_fields(self) -> None:
        sid = self.new_session()
        message = self.add(sid, "user", "原文")
        updated = session.update(sid, message["id"], id="msg-999", content="改后")
        self.assertEqual(updated["id"], "msg-001")
        self.assertEqual(updated["content"], "改后")


class ContextTests(SessionTestBase):
    def test_context_has_agent_prefix_and_failure_mark(self) -> None:
        sid = self.new_session()
        self.add(sid, "user", "加个导出")
        self.add(sid, "executor", "改了 export.py", status="failed")
        self.add(sid, "reviewer", "PASS", status="complete")
        text = session.context(sid)
        self.assertIn("用户: 加个导出", text)
        self.assertIn("执行（失败）: 改了 export.py", text)
        self.assertIn("复核: PASS", text)
        self.assertEqual(len(text.splitlines()), 3)

    def test_context_skips_empty_placeholders(self) -> None:
        sid = self.new_session()
        placeholder = self.add(sid, "executor", "", status="pending")
        self.add(sid, "user", "先做登录")
        text = session.context(sid)
        self.assertEqual(text, "用户: 先做登录")
        session.update(sid, placeholder["id"], status="complete", content="登录做好了")
        self.assertIn("执行: 登录做好了", session.context(sid))

    def test_context_limit_keeps_last_lines(self) -> None:
        sid = self.new_session()
        for index in range(5):
            self.add(sid, "coordinator", f"第 {index} 条")
        lines = session.context(sid, limit=2).splitlines()
        self.assertEqual(lines, ["协调: 第 3 条", "协调: 第 4 条"])

    def test_context_flattens_whitespace_and_truncates(self) -> None:
        sid = self.new_session()
        self.add(sid, "executor", "第一行\n\n第二行" + "长" * 2000)
        line = session.context(sid)
        self.assertNotIn("\n\n", line)
        self.assertTrue(line.endswith("…"))

    def test_context_includes_failed_only_marker_on_failed(self) -> None:
        sid = self.new_session()
        self.add(sid, "tester", "全部通过", status="complete")
        self.assertNotIn("（失败）", session.context(sid))


class TitleTests(SessionTestBase):
    def test_title_from_first_user_message(self) -> None:
        sid = self.new_session()
        self.add(sid, "coordinator", "我在")
        self.add(sid, "user", "给登录页加记住我")
        self.add(sid, "user", "后面这条不该当标题")
        self.assertEqual(session.title_for(sid), "给登录页加记住我")

    def test_title_truncated(self) -> None:
        sid = self.new_session()
        self.add(sid, "user", "把" + "很长的需求" * 10)
        title = session.title_for(sid)
        self.assertEqual(len(title), 31)
        self.assertTrue(title.endswith("…"))

    def test_title_falls_back(self) -> None:
        sid = self.new_session()
        self.assertEqual(session.title_for(sid), session.DEFAULT_TITLE)
        self.assertEqual(session.title_for(sid, fallback="兜底标题"), "兜底标题")


class LegacyMigrationTests(SessionTestBase):
    def _ledger(self) -> list[dict]:
        return [
            {"id": 1, "at": "2026-10-30T23:56:28", "from": "user", "name": "我",
             "kind": "task", "text": "做一个登录页"},
            {"id": 2, "at": "2026-10-30T23:56:31", "from": "deepseek", "name": "DeepSeek（大脑）",
             "kind": "msg", "text": "好，先做表单"},
            {"id": 3, "at": "2026-10-30T23:57:00", "from": "codex", "name": "Codex（执行者）",
             "kind": "result", "text": "表单已完成", "meta": {"task": "T-261030-235628"}},
        ]

    def test_maps_old_ledger_to_new_format(self) -> None:
        self.write_legacy_ledger(self._ledger())
        messages = session.messages("legacy")
        self.assertEqual(len(messages), 3)
        self.assertEqual([item["agent"] for item in messages],
                         ["user", "coordinator", "executor"])
        self.assertEqual([item["role"] for item in messages],
                         ["user", "assistant", "assistant"])
        self.assertEqual([item["content"] for item in messages],
                         ["做一个登录页", "好，先做表单", "表单已完成"])
        self.assertEqual([item["status"] for item in messages],
                         ["complete", "complete", "complete"])
        self.assertEqual([item["artifacts"] for item in messages], [[], [], []])
        self.assertEqual(messages[0]["created_at"], "2026-10-30T23:56:28")
        self.assertEqual(messages[2]["task_id"], "T-261030-235628")

    def test_migration_is_idempotent(self) -> None:
        self.write_legacy_ledger(self._ledger())
        self.assertEqual(session.migrate_legacy(), 3)
        self.assertEqual(session.migrate_legacy(), 0)
        self.assertEqual(len(session.messages("legacy")), 3)
        # 重复迁移不能把消息写重
        self.assertEqual(len(self.raw_messages("legacy")), 3)

    def test_migration_picks_up_records_added_later(self) -> None:
        self.write_legacy_ledger(self._ledger())
        self.assertEqual(session.migrate_legacy(), 3)
        records = self._ledger() + [
            {"id": 4, "at": "2026-10-30T23:58:00", "from": "reviewer",
             "kind": "result", "text": "PASS"},
        ]
        self.write_legacy_ledger(records)
        self.assertEqual(session.migrate_legacy(), 1)
        self.assertEqual(len(session.messages("legacy")), 4)

    def test_legacy_session_listed_in_index(self) -> None:
        self.write_legacy_ledger(self._ledger())
        entries = {item["id"]: item for item in session.sessions()}
        self.assertIn("legacy", entries)
        self.assertEqual(entries["legacy"]["title"], "历史记录")
        self.assertTrue(entries["legacy"]["legacy"])
        self.assertEqual(entries["legacy"]["message_count"], 3)
        self.assertEqual(session.sessions()[-1]["id"], "legacy")   # 历史排在最下面

    def test_no_ledger_no_legacy_session(self) -> None:
        session.active_session()
        self.assertNotIn("legacy", [item["id"] for item in session.sessions()])


class RoomCompatTests(SessionTestBase):
    def test_post_writes_new_structure_and_returns_legacy_shape(self) -> None:
        posted = room.post("user", "把数据导出", "task")
        self.assertIsInstance(posted["id"], int)
        self.assertEqual(posted["from"], "user")
        self.assertEqual(posted["text"], "把数据导出")
        self.assertEqual(posted["kind"], "task")
        self.assertEqual(posted["message_id"], "msg-001")
        self.assertEqual(posted["status"], "complete")
        self.assertEqual(posted["session_id"], session.active_session()["id"])

        stored = session.messages(posted["session_id"])[-1]
        self.assertTrue(NEW_MESSAGE_FIELDS.issubset(stored.keys()))
        self.assertEqual(stored["agent"], "user")
        self.assertEqual(stored["content"], "把数据导出")

    def test_post_maps_meta_artifacts_status_task(self) -> None:
        posted = room.post("executor", "改完 2 个文件", "result", {
            "task": "T-261031-120000",
            "status": "failed",
            "artifacts": ["tasks/T-261031-120000/exec-01.last.md", "workspace:src/login.py"],
        })
        stored = session.messages(posted["session_id"])[-1]
        self.assertEqual(stored["task_id"], "T-261031-120000")
        self.assertEqual(stored["status"], "failed")
        self.assertEqual(stored["artifacts"],
                         ["tasks/T-261031-120000/exec-01.last.md", "workspace:src/login.py"])
        self.assertEqual(stored["agent"], "executor")
        # 旧格式里 meta 还得在（room_web 的事件推断依赖 phase/state）
        view = room.read_all()[-1]
        self.assertEqual(view["meta"]["task"], "T-261031-120000")
        self.assertEqual(view["status"] if "status" in view else posted["status"], "failed")

    def test_deepseek_alias_maps_to_coordinator(self) -> None:
        posted = room.post("deepseek", "我先看下群里的上下文", "msg")
        stored = session.messages(posted["session_id"])[-1]
        self.assertEqual(stored["agent"], "coordinator")
        self.assertEqual(stored["role"], "assistant")
        self.assertEqual(posted["from"], "deepseek")     # 旧接口里作者名要还原
        self.assertEqual(posted["name"], "协调")

    def test_codex_alias_maps_to_executor(self) -> None:
        posted = room.post("codex", "改完了", "result")
        stored = session.messages(posted["session_id"])[-1]
        self.assertEqual(stored["agent"], "executor")

    def test_read_all_restores_old_fields_after_migration(self) -> None:
        self.write_legacy_ledger([
            {"id": 1, "at": "2026-10-30T23:56:28", "from": "user", "kind": "task",
             "text": "做一个登录页"},
            {"id": 2, "at": "2026-10-30T23:56:31", "from": "deepseek", "kind": "msg",
             "text": "好，先做表单"},
            {"id": 3, "at": "2026-10-30T23:57:00", "from": "codex", "kind": "result",
             "text": "表单已完成", "meta": {"task": "T-261030-235628"}},
        ])
        views = room.read_all()
        self.assertEqual([item["from"] for item in views], ["user", "deepseek", "codex"])
        self.assertEqual([item["kind"] for item in views], ["task", "msg", "result"])
        self.assertEqual([item["id"] for item in views], [1, 2, 3])
        self.assertEqual(views[2]["text"], "表单已完成")
        self.assertEqual(views[2]["meta"], {"task": "T-261030-235628"})
        self.assertEqual(views[1]["name"], "协调")

        room.post("user", "接着做导出", "task")
        newest = room.read_all()[-1]
        self.assertEqual((newest["from"], newest["kind"]), ("user", "task"))
        self.assertEqual(newest["id"], 4)

    def test_pending_and_ack(self) -> None:
        room.post("user", "第一件事", "task")
        self.assertEqual([item["text"] for item in room.pending()], ["第一件事"])
        room.post("executor", "干完了", "result")       # 非用户消息不算待处理
        self.assertEqual(len(room.pending()), 1)
        state = room._read_state()                       # noqa: SLF001
        state["last_handled_id"] = room.read_all()[-1]["id"]
        room._write_state(state)                         # noqa: SLF001
        self.assertEqual(room.pending(), [])

    def test_after_returns_newer_messages(self) -> None:
        room.post("user", "第一条", "task")
        room.post("coordinator", "收到", "msg")
        self.assertEqual(len(room.after(0)), 2)
        self.assertEqual(len(room.after(1)), 1)
        self.assertEqual(room.after(99), [])

    def test_post_to_explicit_session(self) -> None:
        target = session.create_session(title="目标会话")["id"]
        other = session.create_session(title="别的会话")["id"]
        room.post("user", "投给目标会话", "task", session_id=target)
        self.assertEqual(session.messages(other), [])
        self.assertEqual(len(session.messages(target)), 1)
        self.assertEqual(session.messages(target)[-1]["content"], "投给目标会话")


class ReimportTests(SessionTestBase):
    def test_reimport_reads_same_messages_from_disk(self) -> None:
        sid = self.new_session()
        self.add(sid, "user", "给登录页加记住我")
        self.add(sid, "planner", "拆成 2 步")
        self.add(sid, "executor", "改完了", status="failed", artifacts=["tasks/x/exec-01.last.md"])

        reloaded = importlib.reload(session)   # 模拟进程重启后重新 import
        self.assertIs(reloaded, session)
        self.assertTrue((self.room_dir / "sessions" / f"{sid}.jsonl").is_file())
        session.ROOM = self.room_dir           # reload 会把模块常量重置回真实仓库

        messages = session.messages(sid)
        self.assertEqual([item["content"] for item in messages],
                         ["给登录页加记住我", "拆成 2 步", "改完了"])
        self.assertEqual([item["id"] for item in messages], ["msg-001", "msg-002", "msg-003"])
        self.assertEqual(messages[2]["status"], "failed")
        self.assertEqual(messages[2]["artifacts"], ["tasks/x/exec-01.last.md"])
        self.assertEqual(session.active_session()["id"], sid)


if __name__ == "__main__":
    unittest.main(verbosity=2)