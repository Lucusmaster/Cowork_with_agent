"""协作台网页层：访问令牌 + 暂停/继续/停止/重新执行的控制接口。

这里起一个真的 HTTP 服务（随机端口），但把 room/ 下的状态文件、任务目录、令牌文件
全部指到临时目录，会话用假 store —— 不碰真实 room/sessions，也不会把暂停指令
写进用户正在跑的那个引擎里。
"""
from __future__ import annotations

import json
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_team import control, room_web

TASK = "T-261031-160000"
HEX32 = re.compile(r"^[0-9a-f]{32}$")


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

    def sessions(self) -> list[dict]:
        return [{
            "id": "chat-test", "title": "测试会话", "workspace": "",
            "created_at": "2026-10-31T16:00:00", "updated_at": "2026-10-31T16:00:00",
            "message_count": len(self.items),
        }]

    def active(self, session_id: str = "") -> dict:
        return self.sessions()[0]

    def messages(self, session_id: str, after_id: str = "", limit: int = 200) -> list[dict]:
        return [dict(item) for item in self.items]

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


if __name__ == "__main__":
    unittest.main()