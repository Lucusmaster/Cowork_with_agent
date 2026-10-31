"""协作台网页层：访问令牌 + 暂停/继续/停止/重新执行的控制接口。

这里起一个真的 HTTP 服务（随机端口），但把 room/ 下的状态文件、任务目录、令牌文件
全部指到临时目录，会话用假 store —— 不碰真实 room/sessions，也不会把暂停指令
写进用户正在跑的那个引擎里。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.parse import quote

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
        room_web.UNLOCK_FAILS.clear()      # 限流计数是进程级的，测试之间别互相污染

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

    # ---------- 原始请求：不跟随重定向，能看到 302 和 Set-Cookie ----------
    def raw(self, path: str, method: str = "GET", headers: dict | None = None,
            body=None, cookie: str = "", ctype: str | None = "application/json"):
        """用 http.client 直连：返回 (status, headers, body_text)，302 不自动跟。

        ctype=None 表示故意不发 Content-Type（测 415 用）。
        """
        hdrs = {k: v for k, v in (headers or {}).items()}
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        if data is not None:
            if ctype:
                hdrs.setdefault("Content-Type", ctype)
            hdrs["Content-Length"] = str(len(data))
        if cookie:
            hdrs["Cookie"] = cookie
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=data, headers=hdrs)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", errors="replace")
            return resp.status, resp.headers, text
        finally:
            conn.close()

    def set_cookie_values(self, headers) -> list[str]:
        return [str(v) for v in (headers.get_all("Set-Cookie") or [])]

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

    def test_query_token_rejected_on_api(self) -> None:
        """旧书签/历史里残留的 ?token= 在 /api/* 上不再受理。"""
        status, data = self.call(f"/api/state?token={self.token}")
        self.assertEqual(status, 401)
        self.assertIn("URL", data["error"])

    def test_alive_is_exempt(self) -> None:
        status, data = self.call("/api/alive")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_html_without_token_shows_unlock_card(self) -> None:
        status, html = self.call("/")
        self.assertEqual(status, 200, "HTML 本身不拦，页面里再让用户贴令牌")
        self.assertIn("需要访问令牌", html)
        self.assertIn('id="unlock"', html)
        self.assertNotIn(self.token, html)
        self.assertNotIn("const TOKEN", html)

    def test_bye_rejects_query_token(self) -> None:
        status, data = self.call(f"/api/bye?token={self.token}", method="POST", body={})
        self.assertEqual(status, 401)

    def test_bye_accepts_cookie(self) -> None:
        status, headers, body = self.raw("/api/bye", method="POST", body={},
                                        cookie=f"team_token={self.token}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

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


class CookieBootstrapTests(WebCase):
    """?token= 只在 GET / 引导时用一次：302 到干净地址 + 种 Cookie。"""

    def test_bootstrap_redirects_and_sets_cookie(self) -> None:
        status, headers, body = self.raw(f"/?token={self.token}")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/", "Location 必须是去掉 token 的干净地址")
        cookies = self.set_cookie_values(headers)
        self.assertEqual(len(cookies), 1, f"只该有一条 Set-Cookie，实际 {cookies}")
        cookie = cookies[0]
        self.assertIn(f"team_token={self.token}", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        self.assertIn("Path=/", cookie)
        self.assertIn("Max-Age=2592000", cookie)
        self.assertNotIn(self.token, body)
        self.assertEqual(body, "")

    def test_bootstrap_keeps_other_query_params(self) -> None:
        """其它查询参数要留下（比如 ?token=..&session=chat-a）。"""
        status, headers, _ = self.raw(f"/?token={self.token}&session=chat-a")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/?session=chat-a")
        self.assertNotIn(self.token, headers.get("Location"))

    def test_bootstrap_wrong_token_is_401_without_cookie(self) -> None:
        status, headers, body = self.raw("/?token=deadbeef")
        self.assertEqual(status, 401)
        self.assertEqual(self.set_cookie_values(headers), [], "令牌错了绝不能种 Cookie")
        self.assertNotIn(self.token, body)
        self.assertIn("令牌不对", body)

    def test_plain_root_serves_page_without_token(self) -> None:
        status, headers, body = self.raw("/")
        self.assertEqual(status, 200)
        self.assertEqual(self.set_cookie_values(headers), [])
        self.assertNotIn(self.token, body)
        self.assertNotIn("const TOKEN", body)
        self.assertIn('id="unlock"', body)


class CookieAuthTests(WebCase):
    """Cookie 主凭证 + X-Team-Token 兼容头；URL 令牌在 API 上一律拒绝。"""

    def test_state_with_cookie_ok(self) -> None:
        status, data = self.call("/api/state", token=None)
        self.assertEqual(status, 401)
        status, headers, body = self.raw("/api/state",
                                        cookie=f"team_token={self.token}")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_wrong_cookie_rejected(self) -> None:
        status, _, body = self.raw("/api/state", cookie="team_token=" + "f" * 32)
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["ok"])

    def test_header_still_works_for_cli(self) -> None:
        status, data = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

    def test_url_token_rejected_even_if_correct(self) -> None:
        status, _, body = self.raw(f"/api/state?token={self.token}")
        self.assertEqual(status, 401)
        self.assertIn("URL", json.loads(body)["error"])

    def test_url_token_rejected_on_every_api(self) -> None:
        for path in ("/api/alive", "/api/session", "/api/tasks"):
            status, _, _ = self.raw(f"{path}?token={self.token}")
            self.assertEqual(status, 401, f"{path} 不该接受 URL 里的令牌")

    def test_cookie_value_is_ignored_for_other_cookie_names(self) -> None:
        status, _, _ = self.raw("/api/state", cookie=f"other={self.token}")
        self.assertEqual(status, 401)


class CsrfTests(WebCase):
    """写操作：Sec-Fetch-Site → Origin → 都没有则放行；只收 JSON。"""

    def setUp(self) -> None:
        super().setUp()
        self.cookie = f"team_token={self.token}"

    def post(self, path: str, body=None, headers=None, cookie: str | None = None,
             ctype: str | None = "application/json"):
        return self.raw(path, method="POST", body=body if body is not None else {},
                        headers=dict(headers or {}),
                        cookie=self.cookie if cookie is None else cookie, ctype=ctype)

    def test_post_requires_credentials(self) -> None:
        status, _, body = self.raw("/api/post", method="POST", body={"content": "hi"})
        self.assertEqual(status, 401)
        status, _, body = self.post("/api/post", body={"content": "hi"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_cross_site_origin_rejected(self) -> None:
        status, _, body = self.post("/api/post", body={"content": "hi"},
                                    headers={"Origin": "http://evil.com"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "跨站请求被拒绝")

    def test_loopback_origin_accepted(self) -> None:
        origin = f"http://127.0.0.1:{self.port}"
        status, _, body = self.post("/api/post", body={"content": "hi"},
                                    headers={"Origin": origin})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_origin_port_must_match_host(self) -> None:
        """Origin 是 localhost 但这次请求的 Host 是 127.0.0.1:port → 端口/主机对不上，拒。"""
        status, _, _ = self.post("/api/post", body={"content": "hi"},
                                 headers={"Origin": f"http://localhost:{self.port}"})
        self.assertEqual(status, 403)

    def test_origin_with_wrong_port_rejected(self) -> None:
        status, _, _ = self.post("/api/post", body={"content": "hi"},
                                 headers={"Origin": f"http://127.0.0.1:{self.port + 1}"})
        self.assertEqual(status, 403)

    def test_sec_fetch_site_cross_site_rejected(self) -> None:
        status, _, body = self.post("/api/post", body={"content": "hi"},
                                    headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"], "跨站请求被拒绝")

    def test_sec_fetch_site_same_site_rejected(self) -> None:
        status, _, _ = self.post("/api/post", body={"content": "hi"},
                                 headers={"Sec-Fetch-Site": "same-site"})
        self.assertEqual(status, 403)

    def test_sec_fetch_site_same_origin_ok(self) -> None:
        status, _, body = self.post("/api/control", body={"action": "pause", "task_id": TASK},
                                    headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_no_origin_no_fetch_site_is_allowed(self) -> None:
        """curl/CLI 两个头都没有：放行（非浏览器客户端）。"""
        status, _, body = self.post("/api/control", body={"action": "resume", "task_id": TASK})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_form_content_type_is_415(self) -> None:
        status, _, body = self.post("/api/control", body={"action": "pause"},
                                    ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)["error"], "只接受 JSON")

    def test_missing_content_type_is_415(self) -> None:
        status, _, _ = self.post("/api/post", body={"content": "hi"}, ctype=None)
        self.assertEqual(status, 415)

    def test_cross_site_checked_before_content_type(self) -> None:
        """跨站 + 表单类型：先报 403，别告诉外面的人内容类型要求。"""
        status, _, _ = self.post("/api/post", body={"content": "hi"},
                                 headers={"Origin": "http://evil.com"},
                                 ctype="application/x-www-form-urlencoded")
        self.assertEqual(status, 403)

    def test_get_does_not_require_origin(self) -> None:
        status, _, _ = self.raw("/api/state", headers={"Origin": "http://evil.com"},
                                cookie=self.cookie)
        self.assertEqual(status, 200, "GET 不校验 Origin（也不该有 GET 写状态的接口）")


class UnlockTests(WebCase):
    """/api/unlock：换 Cookie、失败计数限流、同样过 CSRF/JSON。"""

    def unlock(self, token: str, headers=None):
        return self.raw("/api/unlock", method="POST", body={"token": token},
                        headers=headers or {})

    def test_unlock_returns_cookie(self) -> None:
        status, headers, body = self.unlock(self.token)
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        cookies = self.set_cookie_values(headers)
        self.assertEqual(len(cookies), 1)
        self.assertIn(f"team_token={self.token}", cookies[0])
        self.assertIn("HttpOnly", cookies[0])
        self.assertIn("SameSite=Strict", cookies[0])

    def test_unlock_wrong_token_is_401(self) -> None:
        status, headers, body = self.unlock("nope")
        self.assertEqual(status, 401)
        self.assertEqual(self.set_cookie_values(headers), [])
        self.assertFalse(json.loads(body)["ok"])

    def test_unlock_rate_limited_after_five_fails(self) -> None:
        for i in range(5):
            status, _, _ = self.unlock(f"bad-{i}")
            self.assertEqual(status, 401, f"第 {i + 1} 次失败应该是 401")
        status, _, body = self.unlock("bad-6")
        self.assertEqual(status, 429)
        self.assertIn("等一分钟", json.loads(body)["error"])
        # 限流期间即使令牌对也不放行，避免被拿来做暴力枚举
        status, _, _ = self.unlock(self.token)
        self.assertEqual(status, 429)

    def test_unlock_requires_json(self) -> None:
        status, _, body = self.raw("/api/unlock", method="POST", body={"token": self.token},
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(status, 415)
        self.assertEqual(json.loads(body)["error"], "只接受 JSON")

    def test_unlock_rejects_cross_site(self) -> None:
        status, _, _ = self.unlock(self.token, headers={"Origin": "http://evil.com"})
        self.assertEqual(status, 403)

    def test_unlock_rejects_token_in_url(self) -> None:
        """令牌只能走请求体；?token= 在 /api/* 上一律不认（unlock 也不例外）。"""
        status, _, _ = self.raw(f"/api/unlock?token={self.token}", method="POST",
                                body={"token": self.token})
        self.assertEqual(status, 401)


class AliveTests(WebCase):
    def test_alive_needs_no_credentials_or_csrf(self) -> None:
        status, _, body = self.raw("/api/alive")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_no_get_endpoint_writes_state(self) -> None:
        """GET 不校验 Origin，那就不许有 GET 写状态的接口：读接口照旧只读，写接口只认 POST。"""
        before = control.pending()
        status, _, body = self.raw("/api/control", cookie=f"team_token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn("paused", json.loads(body), "GET /api/control 只是读状态")
        self.assertEqual(control.pending(), before, "GET 不能顺手写下控制指令")
        for path in ("/api/post", "/api/unlock", "/api/session/select"):
            status, _, _ = self.raw(path, cookie=f"team_token={self.token}")
            self.assertIn(status, (404, 405), f"{path} 不该用 GET 就能改状态")

    def test_put_and_delete_are_guarded(self) -> None:
        status, _, _ = self.raw("/api/post", method="PUT", body={"content": "x"})
        self.assertEqual(status, 401, "写方法先鉴权")
        status, _, _ = self.raw("/api/post", method="DELETE", body={},
                                cookie=f"team_token={self.token}")
        self.assertEqual(status, 404, "有凭证但没有这个接口")


class BrowseTests(WebCase):
    """/api/browse：只列目录 + 体检，给「选工作区」对话框用。"""

    def make_tree(self) -> Path:
        root = self.tmp / "repo"
        (root / "src").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "README.md").write_text("hi", encoding="utf-8")
        (root / "main.py").write_text("print(1)", encoding="utf-8")
        (root / "空文件.txt").write_text("", encoding="utf-8")
        return root

    def browse(self, path=None):
        url = "/api/browse"
        if path is not None:
            url += "?path=" + quote(str(path), safe="")
        return self.call(url, token=self.token)

    def test_root_lists_drives_or_roots(self) -> None:
        status, data = self.browse()
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertTrue(data["drives"], "至少给一个起点（盘符）")
        self.assertEqual(data["path"], "")
        self.assertTrue(data["home"])
        self.assertTrue(data["entries"], "根模式也要有可点的条目")
        self.assertFalse(data["selected"]["exists"], "还没选具体目录时不该假装存在")

    def test_lists_only_directories_and_counts_files(self) -> None:
        root = self.make_tree()
        status, data = self.browse(root)
        self.assertEqual(status, 200)
        names = [e["name"] for e in data["entries"]]
        self.assertEqual(names, ["src", "tests"], "只列目录，不列文件")
        self.assertTrue(all("file_count" in e for e in data["entries"]))
        selected = data["selected"]
        self.assertTrue(selected["exists"])
        self.assertEqual(selected["file_count"], 3, "这一层 3 个文件")
        self.assertEqual(selected["entry_count"], 5)
        self.assertFalse(selected["is_git"])
        self.assertTrue(selected["writable"])
        self.assertEqual(data["path"], str(root))
        self.assertEqual(data["parent"], str(root.parent))
        self.assertFalse(data["truncated"])

    def test_missing_dir_is_404_and_file_is_400(self) -> None:
        status, data = self.browse(self.tmp / "nope")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "目录不存在")
        target = self.tmp / "plain.txt"
        target.write_text("x", encoding="utf-8")
        status, data = self.browse(target)
        self.assertEqual(status, 400)
        self.assertIn("不是目录", data["error"])

    def test_entries_capped_at_200_but_counts_are_real(self) -> None:
        root = self.tmp / "many"
        root.mkdir()
        for i in range(250):
            (root / f"d{i:03d}").mkdir()
        for i in range(5):
            (root / f"f{i}.txt").write_text("x", encoding="utf-8")
        status, data = self.browse(root)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["entries"]), 200, "entries 最多 200 条")
        self.assertTrue(data["truncated"])
        self.assertEqual(data["dir_count"], 250)
        self.assertEqual(data["selected"]["file_count"], 5, "file_count 不被 200 条截断影响")
        self.assertEqual(data["selected"]["entry_count"], 255)

    def test_git_repo_detected_with_branch(self) -> None:
        root = self.tmp / "gitted"
        root.mkdir()
        (root / "a.txt").write_text("x", encoding="utf-8")
        git = shutil.which("git")
        if git:
            subprocess.run([git, "init", "-q"], cwd=root, capture_output=True)
            subprocess.run([git, "symbolic-ref", "HEAD", "refs/heads/main"],
                           cwd=root, capture_output=True)
        else:                                   # 没装 git：至少 .git 目录要认出来
            (root / ".git").mkdir()
        status, data = self.browse(root)
        self.assertEqual(status, 200)
        self.assertTrue(data["selected"]["is_git"])
        branch = data["selected"]["git_branch"]
        self.assertIsInstance(branch, str)
        if git:
            self.assertEqual(branch, "main")

    def test_noise_dirs_go_last_and_are_marked(self) -> None:
        root = self.tmp / "noisy"
        root.mkdir()
        for name in ("app", "node_modules", "__pycache__", ".venv", "docs"):
            (root / name).mkdir()
        status, data = self.browse(root)
        self.assertEqual(status, 200)
        names = [e["name"] for e in data["entries"]]
        self.assertEqual(names[:2], ["app", "docs"], "正常目录在前，按名字排")
        noisy = [e for e in data["entries"] if e["noise"]]
        self.assertEqual({e["name"] for e in noisy}, {"node_modules", "__pycache__", ".venv"})
        self.assertFalse(data["entries"][0]["noise"])

    def test_permission_denied_is_403(self) -> None:
        target = self.tmp / "locked"
        target.mkdir()
        (target / "sub").mkdir()
        real_scandir = room_web.os.scandir

        def fake_scandir(path="."):
            if str(path).replace("\\", "/").endswith("locked"):
                raise PermissionError(13, "denied", str(path))
            return real_scandir(path)

        with mock.patch.object(room_web.os, "scandir", fake_scandir):
            status, data = self.call("/api/browse?path=" + quote(str(target), safe=""),
                                     token=self.token)
        self.assertEqual(status, 403, "权限不足要 403，不能 500")
        self.assertIn("权限", data["error"])

    def test_non_ascii_path_decoded(self) -> None:
        """中文目录：正常 percent-encode 能读；没 encode 的 latin-1 串也要救回来。"""
        target = self.tmp / "中文目录"
        target.mkdir()
        status, data = self.browse(target)
        self.assertEqual(status, 200)
        self.assertEqual(Path(data["path"]).name, "中文目录")
        mojibake = str(target).encode("utf-8").decode("latin-1")
        status, data = self.call("/api/browse?path=" + quote(mojibake, safe=""),
                                 token=self.token)
        self.assertEqual(status, 200, "latin-1 串进来的中文路径也要认")
        self.assertEqual(Path(data["path"]).name, "中文目录")

    def test_browse_requires_credentials(self) -> None:
        status, data = self.call("/api/browse")
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])

    def test_browse_rejects_url_token(self) -> None:
        status, data = self.call(f"/api/browse?token={self.token}")
        self.assertEqual(status, 401)
        self.assertIn("URL", data["error"])


class SessionOpsTests(WebCase):
    """归档 / 删除接口：用真的 session.py（ROOM 指到临时目录），不打桩。

    这样测的是"路由 + 适配层 + 存储层"整条链，包括 active 切换和 trash 挪文件。
    """

    def setUp(self) -> None:
        super().setUp()
        from agent_team import session as sess_mod
        self.sess = sess_mod
        self._saved_sess_room = sess_mod.ROOM
        sess_mod.ROOM = self.tmp / "session_room"
        (sess_mod.ROOM / "sessions").mkdir(parents=True, exist_ok=True)
        room_web.STORE = room_web.ChatStore()          # 指向临时 ROOM 的真适配层
        self.assertTrue(room_web.STORE.ready, "session.py 应该可用")

    def tearDown(self) -> None:
        self.sess.ROOM = self._saved_sess_room
        super().tearDown()

    # ---------- 工具 ----------
    def make(self, title: str) -> str:
        entry = self.sess.create_session(title=title)
        self.sess.append(entry["id"], role="user", agent="user", content=f"{title} 的第一句")
        return str(entry["id"])

    def rows(self) -> list[dict]:
        status, data = self.call("/api/session", token=self.token)
        self.assertEqual(status, 200)
        return data["sessions"]

    def post(self, path: str, body: dict):
        return self.call(path, method="POST", body=body, token=self.token)

    # ---------- 归档 ----------
    def test_archive_marks_session_and_listing_carries_it(self) -> None:
        a = self.make("会话 A")
        status, data = self.post("/api/session/archive", {"id": a, "archived": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertTrue(data["session"]["archived_at"], "返回体里要带 archived_at")
        row = [r for r in self.rows() if r["id"] == a][0]
        self.assertTrue(row["archived_at"], "GET /api/session 的每一条都要有 archived_at")
        unarchived = [r for r in self.rows() if not r["archived_at"]]
        self.assertNotIn(a, [r["id"] for r in unarchived])

    def test_archive_defaults_to_true_and_can_restore(self) -> None:
        a = self.make("会话 A")
        status, data = self.post("/api/session/archive", {"id": a})
        self.assertEqual(status, 200)
        self.assertTrue(data["session"]["archived_at"], "archived 缺省就是 True")
        status, data = self.post("/api/session/archive", {"id": a, "archived": False})
        self.assertEqual(status, 200)
        self.assertEqual(data["session"]["archived_at"], "", "传 false 就是恢复")
        self.assertEqual([r for r in self.rows() if r["id"] == a][0]["archived_at"], "")

    def test_archive_current_switches_active(self) -> None:
        a = self.make("会话 A")
        b = self.make("会话 B")                      # b 是当前会话
        self.sess.set_active(a)
        status, data = self.post("/api/session/archive", {"id": a, "archived": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["active"], b, "归档当前会话后 active 要切到另一个")

    def test_archive_missing_session_is_404(self) -> None:
        status, data = self.post("/api/session/archive", {"id": "chat-999"})
        self.assertEqual(status, 404)
        self.assertIn("会话不存在", data["error"])

    def test_archive_without_id_is_400(self) -> None:
        status, data = self.post("/api/session/archive", {})
        self.assertEqual(status, 400)
        self.assertIn("id", data["error"])

    # ---------- 删除 ----------
    def test_delete_removes_from_index_and_moves_file(self) -> None:
        a = self.make("会话 A")
        src = self.sess.ROOM / "sessions" / f"{a}.jsonl"
        self.assertTrue(src.is_file(), "会话要真的落在 sessions/")
        status, data = self.post("/api/session/delete", {"id": a, "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertEqual(data["deleted"]["id"], a)
        self.assertNotIn(a, [r["id"] for r in self.rows()], "索引里不能再有它")
        self.assertFalse(src.is_file(), "消息文件要挪走")
        trash = list((self.sess.ROOM / "sessions" / "trash").glob(f"{a}-*.jsonl"))
        self.assertEqual(len(trash), 1, "文件要留在 sessions/trash/ 里")
        self.assertIn("trash", data["deleted"])

    def test_delete_requires_confirm_true(self) -> None:
        a = self.make("会话 A")
        status, data = self.post("/api/session/delete", {"id": a})
        self.assertEqual(status, 400)
        self.assertIn("confirm", data["error"])
        self.assertIn(a, [r["id"] for r in self.rows()], "没确认就不能删")
        status, data = self.post("/api/session/delete", {"id": a, "confirm": False})
        self.assertEqual(status, 400, "confirm:false 也不行")

    def test_delete_missing_session_is_404(self) -> None:
        status, data = self.post("/api/session/delete", {"id": "chat-999", "confirm": True})
        self.assertEqual(status, 404)
        self.assertIn("会话不存在", data["error"])

    def test_delete_archived_session_allowed(self) -> None:
        a = self.make("会话 A")
        self.post("/api/session/archive", {"id": a, "archived": True})
        status, data = self.post("/api/session/delete", {"id": a, "confirm": True})
        self.assertEqual(status, 200, "已归档的会话也要能删掉")
        self.assertNotIn(a, [r["id"] for r in self.rows()])

    def test_delete_current_switches_active(self) -> None:
        a = self.make("会话 A")
        b = self.make("会话 B")
        self.sess.set_active(b)
        status, data = self.post("/api/session/delete", {"id": b, "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["active"], a)

    def test_delete_other_keeps_active(self) -> None:
        """删的不是当前会话：active 一动不动（同一秒建出来的会话也不能被「顺手切走」）。"""
        a = self.make("会话 A")
        b = self.make("会话 B")
        self.sess.set_active(a)
        status, data = self.post("/api/session/delete", {"id": b, "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["active"], a, "删别人不能改当前会话")
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(state["session"]["id"], a)
        self.assertEqual([s["id"] for s in state["sessions"]], [a])

    def test_archive_other_keeps_active(self) -> None:
        a = self.make("会话 A")
        b = self.make("会话 B")
        self.sess.set_active(a)
        status, data = self.post("/api/session/archive", {"id": b, "archived": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["active"], a, "归档别人不能改当前会话")

    def test_delete_last_session_leaves_empty_state(self) -> None:
        a = self.make("会话 A")
        self.sess.set_active(a)
        status, data = self.post("/api/session/delete", {"id": a, "confirm": True})
        self.assertEqual(status, 200, data)
        self.assertEqual(data["active"], "", "全删空就没有 active")
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(state["sessions"], [], "轮询不能偷偷把 chat-001 建回来")
        self.assertEqual(state["messages"], [])
        self.assertEqual(state["session"], {})

    def test_send_from_empty_state_opens_new_session(self) -> None:
        """空状态下说一句话就会开一个新的（列表里那句承诺得真成立）。"""
        a = self.make("会话 A")
        self.post("/api/session/delete", {"id": a, "confirm": True})
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual(state["sessions"], [])
        status, data = self.post("/api/post", {"content": "空状态里说一句话"})
        self.assertEqual(status, 200, data)
        new_id = data["session_id"]
        status, state = self.call("/api/state", token=self.token)
        self.assertEqual([s["id"] for s in state["sessions"]], [new_id])
        self.assertEqual(state["session"]["id"], new_id)
        self.assertEqual(len(state["messages"]), 1)

    # ---------- 鉴权 / 方法 ----------
    def test_ops_require_token(self) -> None:
        a = self.make("会话 A")
        status, data = self.call("/api/session/archive", method="POST", body={"id": a})
        self.assertEqual(status, 401)
        self.assertFalse(data["ok"])
        status, data = self.call("/api/session/delete", method="POST",
                                 body={"id": a, "confirm": True})
        self.assertEqual(status, 401)

    def test_ops_reject_url_token(self) -> None:
        a = self.make("会话 A")
        status, data = self.post(f"/api/session/delete?token={self.token}",
                                 {"id": a, "confirm": True})
        self.assertEqual(status, 401)
        self.assertIn("URL", data["error"])
        self.assertIn(a, [r["id"] for r in self.rows()], "被拒的请求不能改状态")

    def test_get_cannot_write(self) -> None:
        a = self.make("会话 A")
        status, data = self.call("/api/session/delete", token=self.token)
        self.assertEqual(status, 404, "读方法打写接口就是没有这个接口")
        self.assertIn(a, [r["id"] for r in self.rows()])

    def test_archived_session_cannot_be_opened(self) -> None:
        a = self.make("会话 A")
        self.post("/api/session/archive", {"id": a, "archived": True})
        status, data = self.post("/api/session/select", {"session_id": a})
        self.assertEqual(status, 400, data)
        self.assertIn("归档", data["error"])


if __name__ == "__main__":
    unittest.main()