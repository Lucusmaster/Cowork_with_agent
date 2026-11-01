"""网页上的模型切换：接口、右栏数据、以及"不许绕过的规则"。

界面是最终用户唯一的入口，所以这里要钉住的不是"能点"，而是：
- 没登录不能改（走 _guard）；
- 拼错型号 / 切协调者 → 400 且带可读原因，覆盖文件不动；
- 改完 /api/state 立刻反映出来（右栏刷新就能看到），校准不需要重启。
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agent_team import models, room_web


class ModelApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.token = "t" * 32
        self._saved = {
            "TOKEN": room_web.TOKEN, "TOKEN_FILE": room_web.TOKEN_FILE, "CODE_FILE": room_web.CODE_FILE,
            "ROOM": room_web.ROOM, "TASKS": room_web.TASKS, "PROJECT_FILE": room_web.PROJECT_FILE,
            "TYPING_FILE": room_web.TYPING_FILE, "NUDGE_FILE": room_web.NUDGE_FILE,
            "SESSION_STATE": room_web.SESSION_STATE, "STORE": room_web.STORE,
            "CONFIG": models.CONFIG, "OVERRIDES": models.OVERRIDES,
        }
        room_web.TOKEN = self.token
        room_web.ROOM = self.tmp / "room"
        room_web.TOKEN_FILE = room_web.ROOM / "web-token"
        room_web.CODE_FILE = room_web.ROOM / "web-code"
        room_web.TASKS = self.tmp / "tasks"
        room_web.PROJECT_FILE = room_web.ROOM / "project.json"
        room_web.TYPING_FILE = room_web.ROOM / "typing.json"
        room_web.NUDGE_FILE = room_web.ROOM / "nudge"
        room_web.SESSION_STATE = room_web.ROOM / "session_state.json"
        room_web.STORE = _FakeStore()
        room_web.ROOM.mkdir(parents=True, exist_ok=True)
        room_web.UNLOCK_FAILS.clear()

        models.CONFIG = self.tmp / "team.json"
        models.OVERRIDES = room_web.ROOM / "model_overrides.json"
        models.CONFIG.write_text(json.dumps({
            "model_registry": {
                "gpt-6.1-terra": {"cli": "codex", "aliases": ["terra"]},
                "gpt-6-luna": {"cli": "codex", "aliases": ["luna"]},
                "claude-opus-5-5": {"cli": "claude", "aliases": ["claude"]},
            },
            "agents": [
                {"role": "coordinator", "name": "协调", "model": "gpt-6.1-terra"},
                {"role": "executor", "name": "执行", "model": "gpt-6.1-terra"},
                {"role": "reviewer", "name": "复核", "model": "gpt-6-luna"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

        self.server = room_web.ThreadingHTTPServer(("127.0.0.1", 0), room_web.Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()
        self._which = mock.patch.object(models.shutil, "which",
                                        side_effect=lambda n: "/usr/bin/codex" if n == "codex" else None)
        self._which.start()

    def tearDown(self) -> None:
        self._which.stop()
        self.server.shutdown()
        self.server.server_close()
        for key, value in self._saved.items():
            setattr(room_web if key not in ("CONFIG", "OVERRIDES") else models, key, value)
        self.tmpdir.cleanup()

    # ---------- 工具 ----------
    def request(self, path: str, body: dict | None = None, token: str | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET")
        req.add_header("X-Team-Token", token if token is not None else self.token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8") or "{}"
            try:
                return exc.code, json.loads(raw)
            except json.JSONDecodeError:
                return exc.code, {"raw": raw}


class _FakeStore:
    ready = True
    broken = ""

    def sessions(self):
        return []

    def active(self, sid=""):
        return {}

    def messages(self, sid, after="", limit=None):
        return []

    def trash(self):
        return []


class ModelsEndpointTests(ModelApiCase):
    def test_get_models_lists_roles_and_registry(self) -> None:
        status, data = self.request("/api/models")
        self.assertEqual(status, 200)
        payload = data["models"]
        roles = {row["role"]: row for row in payload["roles"]}
        self.assertEqual(roles["executor"]["model"], "gpt-6.1-terra")
        self.assertEqual(roles["reviewer"]["model"], "gpt-6-luna")
        self.assertFalse(roles["coordinator"]["switchable"])
        self.assertEqual({item["id"] for item in payload["registry"]},
                         {"gpt-6.1-terra", "gpt-6-luna", "claude-opus-5-5"})

    def test_models_requires_auth(self) -> None:
        status, _ = self.request("/api/models", token="")
        self.assertEqual(status, 401)

    def test_set_model_changes_next_card(self) -> None:
        status, data = self.request("/api/set-model", {"role": "执行", "model": "luna"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        roles = {row["role"]: row for row in data["models"]["roles"]}
        self.assertEqual(roles["executor"]["model"], "gpt-6-luna")
        self.assertEqual(roles["executor"]["source"], "临时")
        self.assertEqual(models.overrides(), {"executor": "gpt-6-luna"})

    def test_set_model_rejects_unknown(self) -> None:
        status, data = self.request("/api/set-model", {"role": "executor", "model": "gemini-9"})
        self.assertEqual(status, 400)
        self.assertFalse(data["ok"])
        self.assertIn("不认识", data["error"])
        self.assertEqual(models.overrides(), {}, "失败的切换不能留下痕迹")

    def test_set_model_rejects_coordinator(self) -> None:
        status, data = self.request("/api/set-model", {"role": "coordinator", "model": "luna"})
        self.assertEqual(status, 400)
        self.assertIn("大脑模型", data["error"])

    def test_set_model_reset(self) -> None:
        self.request("/api/set-model", {"role": "executor", "model": "luna"})
        status, data = self.request("/api/set-model", {"role": "executor", "model": ""})
        self.assertEqual(status, 200)
        roles = {row["role"]: row for row in data["models"]["roles"]}
        self.assertEqual(roles["executor"]["source"], "配置")
        self.assertEqual(models.overrides(), {})

    def test_set_model_requires_auth(self) -> None:
        status, _ = self.request("/api/set-model", {"role": "executor", "model": "luna"}, token="")
        self.assertEqual(status, 401)
        self.assertEqual(models.overrides(), {})

    def test_state_payload_carries_models(self) -> None:
        """右栏靠 /api/state 一次拿到模型信息，不需要额外再请求一次。"""
        status, data = self.request("/api/state")
        self.assertEqual(status, 200, data)
        self.assertIn("models", data)
        self.assertTrue(data["models"]["roles"])


class PageContractTests(unittest.TestCase):
    """页面里必须有模型徽章和菜单，否则右栏就是死的（纯字符串契约，不起服务）。"""

    def test_page_has_model_menu_and_wiring(self) -> None:
        page = room_web.PAGE
        for needle in ('id="model-menu"', "openModelMenu", "switchModel",
                       "/api/set-model", "class=\"am\"", ".amrow"):
            self.assertIn(needle, page, f"页面里缺 {needle}")

    def test_model_picker_marks_current_and_unready(self) -> None:
        src = room_web.PAGE
        self.assertIn("✓", src)
        self.assertIn("disabled", src)
        self.assertIn("本机没装", src, "没装 CLI 的型号要当场说明，而不是让用户选完失败")

    def test_switch_goes_through_api_not_local_only(self) -> None:
        """只在内存里改 st.models 是不算数的——必须落盘到服务端。"""
        idx = room_web.PAGE.index("async function switchModel")
        body = room_web.PAGE[idx:idx + 700]
        self.assertIn("apiPost('/api/set-model'", body)


if __name__ == "__main__":
    unittest.main()