"""协作台网页层：访问令牌 + 暂停/继续/停止/重新执行的控制接口。

这里起一个真的 HTTP 服务（随机端口），但把 room/ 下的状态文件、任务目录、令牌文件
全部指到临时目录，会话用假 store —— 不碰真实 room/sessions，也不会把暂停指令
写进用户正在跑的那个引擎里。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_team import control, procs, room_web

TASK = "T-261031-160000"
HEX32 = re.compile(r"^[0-9a-f]{32}$")
MSG_ID = re.compile(r"^msg-(\d+)$")     # 网页里 idNum() 认的格式


class FakeStore:
    """够用的假会话存储：只实现 room_web 用到的几个方法。"""

    ready = True
    broken = ""

    def __init__(self) -> None:
        self.appended: list[dict] = []
        self.items = [{
            "id": "msg-001", "session_id": "chat-test", "role": "user", "agent": "user",
            "task_id": None, "content": "测试：给登录页加记住我", "artifacts": [],
            "status": "complete", "created_at": "2026-10-31T16:00:00",
        }]

    def seed(self, count: int) -> None:
        """造 count 条历史消息：第一条是用户需求，后面交替助手回复。"""
        self.items = []
        for i in range(1, count + 1):
            role = "user" if i == 1 else "assistant"
            self.items.append({
                "id": f"msg-{i:03d}", "session_id": "chat-test", "role": role,
                "agent": "user" if role == "user" else "coordinator",
                "task_id": None, "content": f"第 {i} 条消息", "artifacts": [],
                "status": "complete", "created_at": "2026-10-31T16:00:00",
            })

    def sessions(self) -> list[dict]:
        return [{
            "id": "chat-test", "title": "测试会话", "workspace": "",
            "created_at": "2026-10-31T16:00:00", "updated_at": "2026-10-31T16:00:00",
            "message_count": len(self.items),
        }]

    def active(self, session_id: str = "") -> dict:
        return self.sessions()[0]

    def messages(self, session_id: str, after_id: str = "", limit: int = 200) -> list[dict]:
        """和 session.messages 一个口径：after 之后、再取最后 limit 条（limit<=0 不截断）。"""
        rows = [dict(item) for item in self.items]
        if after_id:
            position = next((i for i, m in enumerate(rows) if m["id"] == after_id), None)
            rows = rows[position + 1:] if position is not None else []
        if limit and limit > 0:
            rows = rows[-limit:]
        return rows

    def create(self, workspace: str = "", title: str = "") -> dict:
        return self.sessions()[0]

    def select(self, session_id: str) -> dict:
        return self.sessions()[0]

    def append(self, session_id: str, role: str = "assistant", agent: str = "coordinator",
               content: str = "", task_id: str | None = None, artifacts=None,
               status: str = "complete", meta=None, kind: str = "") -> dict:
        item = {
            "id": f"msg-{len(self.items) + 1:03d}", "session_id": session_id,
            "role": role, "agent": agent, "task_id": task_id,
            "content": content, "artifacts": list(artifacts or []),
            "status": status, "created_at": "2026-10-31T16:01:00",
        }
        self.items.append(item)
        self.appended.append(item)
        return item


class WebCase(unittest.TestCase):
    """起一个隔离的 HTTP 服务，令牌固定成 self.token。"""

    token = "test-token-0123456789abcdef"

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.store = FakeStore()
        self._saved = {
            "TOKEN": room_web.TOKEN, "TOKEN_FILE": room_web.TOKEN_FILE,
            "ROOM": room_web.ROOM, "TASKS": room_web.TASKS,
            "PROJECT_FILE": room_web.PROJECT_FILE, "TYPING_FILE": room_web.TYPING_FILE,
            "NUDGE_FILE": room_web.NUDGE_FILE, "SESSION_STATE": room_web.SESSION_STATE,
            "STORE": room_web.STORE,
            "CONTROL": control.CONTROL, "PAUSE": control.PAUSE, "CONTROL_ROOM": control.ROOM,
        }
        room_web.TOKEN = self.token
        room_web.TOKEN_FILE = self.tmp / "web-token"
        room_web.ROOM = self.tmp / "room"
        room_web.TASKS = self.tmp / "tasks"
        room_web.PROJECT_FILE = room_web.ROOM / "project.json"
        room_web.TYPING_FILE = room_web.ROOM / "typing.json"
        room_web.NUDGE_FILE = room_web.ROOM / "nudge"
        room_web.SESSION_STATE = room_web.ROOM / "session_state.json"
        room_web.STORE = self.store
        control.ROOM = room_web.ROOM
        control.CONTROL = room_web.ROOM / "control.json"
        control.PAUSE = room_web.ROOM / "pause.json"
        room_web.ROOM.mkdir(parents=True, exist_ok=True)

        self.server = room_web.ThreadingHTTPServer(("127.0.0.1", 0), room_web.Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        room_web.TOKEN = self._saved["TOKEN"]
        room_web.TOKEN_FILE = self._saved["TOKEN_FILE"]
        room_web.ROOM = self._saved["ROOM"]
        room_web.TASKS = self._saved["TASKS"]
        room_web.PROJECT_FILE = self._saved["PROJECT_FILE"]
        room_web.TYPING_FILE = self._saved["TYPING_FILE"]
        room_web.NUDGE_FILE = self._saved["NUDGE_FILE"]
        room_web.SESSION_STATE = self._saved["SESSION_STATE"]
        room_web.STORE = self._saved["STORE"]
        control.CONTROL = self._saved["CONTROL"]
        control.PAUSE = self._saved["PAUSE"]
        control.ROOM = self._saved["CONTROL_ROOM"]
        self.tmpdir.cleanup()

    # ---------- 工具 ----------
    def call(self, path: str, method: str = "GET", body=None, token: str | None = None):
        """打一次真实 HTTP 请求。

        Windows 下偶发 WinError 10053（连接被中止，尤其是 /api/bye 这种
        发完就走的请求），重试一次即可；这不是接口问题，是本地 socket 的抖动。
        """
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        status = 0
        raw = ""
        for attempt in range(2):
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Content-Type", "application/json")
            if token:
                req.add_header("X-Team-Token", token)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    raw = resp.read().decode("utf-8")
                    status = resp.status
                break
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8")
                status = exc.code
                break
            except (ConnectionError, TimeoutError):
                if attempt == 1:
                    raise
                continue
        try:
            return status, json.loads(raw)
        except ValueError:
            return status, raw

    # ---------- 伪造引擎状态 ----------
    def write_lock(self, pid: int, age: float) -> None:
        """写一个 worker.lock：pid 用真的（本进程），age 是心跳多少秒前。"""
        (room_web.ROOM / "worker.lock").write_text(
            json.dumps({"pid": pid, "beat": time.time() - age}), encoding="utf-8")

    def write_typing(self, who: str = "coordinator") -> None:
        room_web.TYPING_FILE.write_text(
            json.dumps({"who": who, "at": time.time()}), encoding="utf-8")


class TokenFileTests(WebCase):
    def test_token_is_32_hex_and_reused(self) -> None:
        first = room_web._ensure_token()      # noqa: SLF001
        self.assertRegex(first, HEX32)
        self.assertEqual(room_web.TOKEN_FILE.read_text(encoding="utf-8").strip(), first)
        self.assertEqual(room_web._ensure_token(), first, "已有令牌文件就该复用")  # noqa: SLF001

    def test_broken_token_file_rebuilt(self) -> None:
        room_web.TOKEN_FILE.write_text("  ", encoding="utf-8")
        token = room_web._ensure_token()      # noqa: SLF001
        self.assertRegex(token, HEX32)
        self.assertNotEqual(token.strip(), "")


class TokenGuardTests(WebCase):
    def test_api_requires_token(self) -> None:
        status, data = self.call("/api/state")
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])
        self.assertEqual(data["error"], "需要访问令牌")

    def test_wrong_token_rejected(self) -> None:
        status, data = self.call("/api/state", token="f" * 32)
        self.assertEqual(status, 401)
        self.assertEqual(data["error"], "需要访问令牌")

    def test_header_token_ok(self) -> None:
        status, data = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_query_token_ok(self) -> None:
        status, data = self.call(f"/api/state?token={self.token}")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_alive_is_exempt(self) -> None:
        status, data = self.call("/api/alive")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_html_without_token_shows_unlock_card(self) -> None:
        status, html = self.call("/")
        self.assertEqual(status, 200, "HTML 本身不拦，页面里再让用户贴令牌")
        self.assertIn('const TOKEN = ""', html)
        self.assertIn("需要访问令牌", html)
        self.assertIn('id="unlock"', html)

    def test_html_injects_valid_token(self) -> None:
        status, html = self.call(f"/?token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn(f'const TOKEN = "{self.token}"', html)

    def test_html_does_not_leak_token_on_bad_link(self) -> None:
        status, html = self.call("/?token=deadbeef")
        self.assertEqual(status, 200)
        self.assertIn('const TOKEN = ""', html)
        self.assertNotIn(self.token, html)

    def test_bye_accepts_query_token(self) -> None:
        status, data = self.call(f"/api/bye?token={self.token}", method="POST", body={})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_bye_still_requires_token(self) -> None:
        status, data = self.call("/api/bye", method="POST", body={})
        self.assertEqual(status, 401)


class ControlEndpointTests(WebCase):
    def test_pause_writes_command_and_state_reports_it(self) -> None:
        status, data = self.call("/api/control", method="POST",
                                 body={"action": "pause", "task_id": TASK}, token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["action"], "pause")
        self.assertEqual(data["task"], TASK)
        pending = control.pending()
        self.assertIsNotNone(pending, "指令要落到 control.json 里等引擎消费")
        self.assertEqual(pending["action"], "pause")
        self.assertEqual(pending["task"], TASK)

        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(state["control"]["action"], "pause")
        self.assertIsNone(state["paused"])

    def test_state_reports_paused_and_engine(self) -> None:
        control.set_paused(TASK, "等你批准后再动手", "approve")
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("paused", state)
        self.assertEqual(state["paused"]["task"], TASK)
        self.assertEqual(state["paused"]["stage"], "approve")
        self.assertEqual(state["paused"]["reason"], "等你批准后再动手")
        self.assertIn("engine", state)
        self.assertFalse(state["engine"]["alive"], "测试目录里没有 worker.lock，应为未运行")
        self.assertEqual(state["engine"]["status"], "none")

    def test_control_get_lists_actions(self) -> None:
        status, data = self.call("/api/control", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(data["available"])
        self.assertEqual(data["actions"], ["pause", "resume", "stop", "approve", "rerun"])
        self.assertIsNone(data["paused"])
        self.assertIsNone(data["control"])

    def test_invalid_action_rejected(self) -> None:
        status, data = self.call("/api/control", method="POST",
                                 body={"action": "delete-everything"}, token=self.token)
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("不支持的控制指令", data["error"])

    def test_missing_action_rejected(self) -> None:
        status, data = self.call("/api/control", method="POST", body={}, token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("缺少 action", data["error"])

    def test_control_requires_token(self) -> None:
        status, data = self.call("/api/control", method="POST", body={"action": "stop"})
        self.assertEqual(status, 401)
        self.assertIsNone(control.pending(), "没令牌不能写进任何指令")

    def test_approve_and_rerun_accepted(self) -> None:
        for action in ("approve", "rerun", "resume", "stop"):
            status, data = self.call("/api/control", method="POST",
                                     body={"action": action, "task_id": TASK}, token=self.token)
            self.assertEqual(status, 200, action)
            self.assertEqual(data["action"], action)


class MessageCompatTests(WebCase):
    def test_new_format_post_still_works(self) -> None:
        status, data = self.call("/api/post", method="POST",
                                 body={"session_id": "chat-test", "content": "新格式也行"},
                                 token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["content"], "新格式也行")
        self.assertEqual(data["role"], "user")
        self.assertEqual(len(self.store.appended), 1)

    def test_old_format_post_still_works(self) -> None:
        status, data = self.call("/api/post", method="POST",
                                 body={"as": "deepseek", "text": "旧格式也要能用"},
                                 token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["agent"], "coordinator")
        self.assertEqual(data["role"], "assistant")

    def test_empty_content_rejected(self) -> None:
        status, data = self.call("/api/post", method="POST", body={"content": "   "},
                                 token=self.token)
        self.assertEqual(status, 400)
        self.assertIn("不能为空", data["error"])


class StateShapeTests(WebCase):
    def test_state_keeps_old_fields(self) -> None:
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        for key in ("session", "sessions", "messages", "agents", "typing",
                    "task", "changes", "workspace"):
            self.assertIn(key, state, f"旧字段 {key} 不能被改坏")
        self.assertEqual(state["session"]["id"], "chat-test")
        self.assertEqual(len(state["messages"]), 1)
        for key in ("engine", "busy", "messages_limit", "total_messages"):
            self.assertIn(key, state, f"新字段 {key} 得在 state 里")

    def test_engine_payload_matches_procs(self) -> None:
        self.write_lock(os.getpid(), 5)
        status, state = self.call("/api/state", token=self.token)
        engine = state["engine"]
        self.assertTrue(engine["alive"])
        self.assertEqual(engine["status"], "running")
        self.assertEqual(engine["pid"], os.getpid())
        self.assertFalse(engine["stale"])
        self.assertLess(engine["age"], 90)
        direct = procs.lock_state(room_web.ROOM / "worker.lock")
        self.assertEqual(engine["status"], direct["status"], "state 里的引擎态必须和 procs 一致")
        self.assertEqual(engine["alive"], direct["alive"])


class EngineStatusTests(WebCase):
    """引擎三态：进程活着心跳旧 = stalled，不是「没在跑」。"""

    def test_stalled_when_process_alive_but_heartbeat_old(self) -> None:
        self.write_lock(os.getpid(), 200)
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        engine = state["engine"]
        self.assertTrue(engine["alive"], "进程明明还活着")
        self.assertEqual(engine["status"], "stalled")
        self.assertTrue(engine["stale"])
        self.assertGreaterEqual(engine["age"], 190)

    def test_dead_when_pid_does_not_exist(self) -> None:
        self.write_lock(999_999_999, 5)
        status, state = self.call("/api/state", token=self.token)
        engine = state["engine"]
        self.assertFalse(engine["alive"])
        self.assertEqual(engine["status"], "dead")

    def test_none_when_no_lock_file(self) -> None:
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(state["engine"]["status"], "none")
        self.assertFalse(state["engine"]["alive"])

    def test_busy_when_beat_is_between_30_and_90(self) -> None:
        self.write_lock(os.getpid(), 45)
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(state["engine"]["status"], "busy", "45 秒没刷新算忙，但仍然活着")

    def test_stalled_still_alive_for_buttons(self) -> None:
        """stalled 时 alive 为真 —— 前端按钮就靠这个，心跳旧也必须能点。"""
        self.write_lock(os.getpid(), 600)
        status, state = self.call("/api/state", token=self.token)
        self.assertTrue(state["engine"]["alive"])
        self.assertEqual(state["engine"]["status"], "stalled")


class PagingTests(WebCase):
    """消息分页：state 默认最近 120 条，往前翻走 /api/messages?before=。"""

    def test_state_defaults_to_latest_120(self) -> None:
        self.store.seed(300)
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(state["messages"]), 120)
        self.assertEqual(state["messages"][0]["id"], "msg-181")
        self.assertEqual(state["messages"][-1]["id"], "msg-300")
        self.assertEqual(state["messages_limit"], 120)
        self.assertEqual(state["total_messages"], 300)

    def test_state_limit_param(self) -> None:
        self.store.seed(300)
        status, state = self.call("/api/state?limit=50", token=self.token)
        self.assertEqual(len(state["messages"]), 50)
        self.assertEqual(state["messages"][-1]["id"], "msg-300")
        self.assertEqual(state["messages_limit"], 50)

    def test_state_after_returns_only_new(self) -> None:
        self.store.seed(300)
        status, state = self.call("/api/state?after=msg-298", token=self.token)
        self.assertEqual([m["id"] for m in state["messages"]], ["msg-299", "msg-300"])

    def test_before_pages_backwards(self) -> None:
        self.store.seed(300)
        status, page = self.call("/api/messages?before=msg-181&limit=120", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(page["ok"])
        self.assertEqual(len(page["messages"]), 120)
        self.assertEqual(page["messages"][0]["id"], "msg-061")
        self.assertEqual(page["messages"][-1]["id"], "msg-180")
        self.assertTrue(page["has_more"], "前面还有 60 条")

        status, page2 = self.call("/api/messages?before=msg-061&limit=120", token=self.token)
        self.assertEqual(len(page2["messages"]), 60)
        self.assertEqual(page2["messages"][0]["id"], "msg-001")
        self.assertFalse(page2["has_more"], "到头了")

    def test_limit_is_clamped(self) -> None:
        self.store.seed(600)
        status, page = self.call("/api/messages?limit=99999", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(page["limit"], 500, "limit 上限 500")
        self.assertEqual(len(page["messages"]), 500)
        self.assertTrue(page["has_more"])

    def test_bad_limit_falls_back_to_default(self) -> None:
        self.store.seed(300)
        status, page = self.call("/api/messages?limit=abc", token=self.token)
        self.assertEqual(page["limit"], 120)
        self.assertEqual(len(page["messages"]), 120)
        self.assertTrue(page["has_more"])

    def test_paging_requires_token(self) -> None:
        status, data = self.call("/api/messages?before=msg-001")
        self.assertEqual(status, 401)


class BusyTests(WebCase):
    """规划阶段：有 typing 就是忙着，右栏据此摆占位卡。"""

    def test_busy_true_while_typing_and_engine_alive(self) -> None:
        self.write_lock(os.getpid(), 5)
        self.write_typing("coordinator")
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        busy = state["busy"]
        self.assertTrue(busy["busy"])
        self.assertEqual(busy["phase"], "plan")
        self.assertIn("记住我", busy["request"])
        self.assertTrue(busy["since"])

    def test_busy_false_without_typing(self) -> None:
        self.write_lock(os.getpid(), 5)
        status, state = self.call("/api/state", token=self.token)
        self.assertFalse(state["busy"]["busy"])

    def test_busy_false_when_engine_dead(self) -> None:
        """typing.json 忘了删也不能骗用户「在规划」。"""
        self.write_lock(999_999_999, 5)
        self.write_typing("coordinator")
        status, state = self.call("/api/state", token=self.token)
        self.assertFalse(state["busy"]["busy"])

    def test_phase_follows_who(self) -> None:
        self.write_lock(os.getpid(), 5)
        self.write_typing("executor")
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(state["busy"]["phase"], "execute")

    def test_typing_short_stays_visible(self) -> None:
        """typing 的 3 分钟过滤只管「正在思考」气泡，忙不忙另算。"""
        self.write_lock(os.getpid(), 5)
        self.write_typing("coordinator")
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(state["typing"]["who"], "coordinator", "刚写的 typing 应该可见")
        self.assertTrue(state["busy"]["busy"])


class _GapClient:
    """和网页 JS 里那套「轮询 → 发现断层 → before 循环补」一一对应的客户端。

    这是接口契约测试（HTTP 打真接口、复现同一套算法），不替代浏览器验证。
    """

    def __init__(self, case, msgs: list[dict]) -> None:
        self.case = case
        self.by_id = {m["id"]: m for m in msgs}
        self.pages = 0
        self.remaining = 0

    @staticmethod
    def num(mid: str) -> int:
        return int(str(mid).split("-")[1])

    def ids(self) -> list[str]:
        return [m["id"] for m in sorted(self.by_id.values(), key=lambda m: self.num(m["id"]))]

    def merge(self, rows: list[dict]) -> None:
        for row in rows:
            self.by_id[row["id"]] = row

    def poll(self, after_id: str) -> tuple[dict, dict | None]:
        status, state = self.case.call(f"/api/state?after={after_id}", token=self.case.token)
        self.case.assertEqual(status, 200)
        incoming = state["messages"]
        max_id = max(self.num(mid) for mid in self.by_id)
        first = self.num(incoming[0]["id"])
        if first <= max_id + 1:
            self.merge(incoming)
            return state, None
        gap = {"from": max_id + 1, "to": first - 1, "boundary": incoming[0]["id"]}
        self.merge(incoming)                    # 和网页一样：先判断层，再把这页并进来
        return state, gap

    def heal(self, gap: dict, max_pages: int = 5) -> int:
        """循环 /api/messages?before= 补缺口，返回还没补上的条数（0 = 补齐）。"""
        frm, to, bound = gap["from"], gap["to"], gap["boundary"]
        while self.pages < max_pages and frm <= to:
            self.pages += 1
            status, page = self.case.call(
                f"/api/messages?before={bound}&limit=120&session=chat-test",
                token=self.case.token)
            self.case.assertEqual(status, 200)
            chunk = [m for m in page["messages"] if frm <= self.num(m["id"]) <= to]
            if not chunk:
                break
            self.merge(chunk)
            lowest = min(self.num(m["id"]) for m in chunk)
            to = lowest - 1
            if to < frm:
                break
            oldest = min(page["messages"], key=lambda m: self.num(m["id"]))
            if oldest["id"] == bound:
                break                       # 没有前进，别死循环
            bound = oldest["id"]
            if not page["has_more"]:
                break
        self.remaining = max(0, to - frm + 1)
        return self.remaining


class GapHealContractTests(WebCase):
    """断层自愈的接口契约：按网页同一套算法打 /api/messages?before= 多页拼回完整序列。"""

    def client(self, local_count: int) -> _GapClient:
        return _GapClient(self, self.store.items[:local_count])

    def test_burst_gap_detected_and_healed(self) -> None:
        """两次轮询之间来了 200 条：轮询窗口只给 181..300，101..180 得自己补。"""
        self.store.seed(300)
        cli = self.client(100)
        _, gap = cli.poll("msg-081")
        self.assertIsNotNone(gap, "第一条 181 > 本地 100+1，应该判定断层")
        self.assertEqual(gap["from"], 101)
        self.assertEqual(gap["to"], 180)
        self.assertEqual(gap["boundary"], "msg-181")
        remaining = cli.heal(gap)
        self.assertEqual(remaining, 0, "一页就够补 80 条")
        self.assertEqual(cli.pages, 1)
        self.assertEqual(cli.ids(), [f"msg-{i:03d}" for i in range(1, 301)],
                         "补完必须是 1..300 连续，一条不少")

    def test_overlap_means_no_gap(self) -> None:
        """轮询带 20 条重叠，正常情况不该误报断层。"""
        self.store.seed(140)
        cli = self.client(100)
        _, gap = cli.poll("msg-081")      # 服务端回 82..140，第一条 82 <= 101
        self.assertIsNone(gap)
        self.assertEqual(cli.pages, 0)

    def test_gap_larger_than_page_cap_reports_remaining(self) -> None:
        """缺口 700 条 > 5 页（600 条）：补 5 页后要明确还剩多少，不能静默。"""
        self.store.seed(900)
        cli = self.client(100)
        _, gap = cli.poll("msg-800")
        self.assertEqual((gap["from"], gap["to"]), (101, 800))
        remaining = cli.heal(gap, max_pages=5)
        self.assertEqual(cli.pages, 5, "最多补 5 页，不能无限循环")
        self.assertEqual(remaining, 100, "600 条补回来，还差 101..200 这 100 条")
        self.assertTrue(cli.remaining > 0)

    def test_retry_with_more_pages_finishes_the_job(self) -> None:
        """用户点「重试补齐」（页数放宽）后能补全，且序列连续。"""
        self.store.seed(900)
        cli = self.client(100)
        _, gap = cli.poll("msg-800")
        cli.heal(gap, max_pages=5)
        self.assertGreater(cli.remaining, 0)
        # 重试用「缺口上界 + 1 那条」的原始 id 当边界（它一定已经加载在本地）
        retry_to = 101 + cli.remaining - 1
        above = next(m for m in cli.by_id.values() if cli.num(m["id"]) == retry_to + 1)
        retry_gap = {"from": 101, "to": retry_to, "boundary": above["id"]}
        remaining = cli.heal(retry_gap, max_pages=20)
        self.assertEqual(remaining, 0)
        self.assertEqual(cli.ids(), [f"msg-{i:03d}" for i in range(1, 901)])

    def test_before_page_contract(self) -> None:
        """自愈依赖的 before 语义：before 不含，返回它之前的一页 + has_more。"""
        self.store.seed(300)
        status, page = self.call("/api/messages?before=msg-105&limit=120&session=chat-test",
                                 token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(page["messages"][-1]["id"], "msg-104")
        self.assertEqual(page["messages"][0]["id"], "msg-001")
        self.assertFalse(page["has_more"])
        status, page2 = self.call("/api/messages?before=msg-181&limit=120&session=chat-test",
                                  token=self.token)
        self.assertEqual(page2["messages"][0]["id"], "msg-061")
        self.assertEqual(page2["messages"][-1]["id"], "msg-180")
        self.assertTrue(page2["has_more"])

    def test_unparseable_ids_skip_heal(self) -> None:
        """id 不是 msg-数字 就跳过自愈，不报错、不多发请求。"""
        self.store.seed(10)
        for i, item in enumerate(self.store.items):
            item["id"] = f"legacy-{i + 1}"
        cli = _GapClient(self, self.store.items[:5])
        incoming = self.store.items[5:]
        self.assertIsNone(MSG_ID.match(incoming[0]["id"]),
                          "网页的 idNum() 解析不出数字 → findGap() 直接跳过自愈")
        self.assertEqual(cli.pages, 0, "跳过自愈就不会去发 /api/messages 请求")


if __name__ == "__main__":
    unittest.main()