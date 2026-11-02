"""API 通道（OpenAI 兼容）：提供方登记、拉模型、注册表合并、角色限制、接口鉴权。

全部打桩 `urllib.request.urlopen`——**这套测试不许真的联网**（CI / 沙箱都连不上
api.deepseek.com，也不该为了跑测试去连）。假 key 只用来钉住一件事：它不会出现在
返回值、落盘文件、`/api/state` 的任何位置。
"""
from __future__ import annotations

import io
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agent_team import agent_clis, api_bridge, api_providers, models, room_web

FAKE_KEY = "sk-TESTKEY-should-not-leak"
ENV = "API_TEST_KEY"


class _Resp:
    """最小响应对象：`with urlopen(...) as resp: resp.read(n)`。"""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.status = 200

    def read(self, n: int = -1) -> bytes:
        return self._payload if n is None or n < 0 else self._payload[:n]

    def getcode(self) -> int:
        return 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _ok(payload: dict, saved: list | None = None):
    """urlopen 桩：记下 Request（test 12 要检查请求体和头），返回 JSON 响应。"""
    def fake(req, timeout=None):
        if saved is not None:
            saved.append(req)
        return _Resp(json.dumps(payload).encode("utf-8"))
    return fake


def _http_error(code: int, body: bytes = b'{"error":"nope"}'):
    def fake(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, code, "error", None, io.BytesIO(body))
    return fake


class ApiCase(unittest.TestCase):
    """隔离的配置 / 覆盖文件 / 提供方文件 / 环境变量；本机 CLI 一律当成没装。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        room = self.tmp / "room"
        room.mkdir(parents=True)
        self._saved = (models.CONFIG, models.OVERRIDES)
        models.CONFIG = self.tmp / "team.json"
        models.OVERRIDES = room / "model_overrides.json"
        self.write_config()
        self._patches = [
            mock.patch.object(api_providers, "STORE", room / "api_providers.json"),
            mock.patch.object(agent_clis, "STORE", room / "agent_clis.json"),
            mock.patch.object(agent_clis, "_common_candidates", return_value=[]),
            mock.patch.dict(agent_clis.os.environ, {}, clear=False),
            mock.patch.dict(api_providers.os.environ, {}, clear=False),
        ]
        for p in self._patches:
            p.start()
        for key in ("CODEX_EXE", "CLAUDE_EXE", "DEEPSEEK_EXE"):
            agent_clis.os.environ.pop(key, None)
        agent_clis._PROBES.clear()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        models.CONFIG, models.OVERRIDES = self._saved
        self._tmp.cleanup()

    def write_config(self, registry: dict | None = None) -> None:
        models.CONFIG.write_text(json.dumps({
            "model_registry": registry or {"gpt-6.1-terra": {"cli": "codex", "aliases": ["terra"]}},
            "agents": [
                {"role": "coordinator", "name": "协调", "model": "gpt-6.1-terra"},
                {"role": "planner", "name": "规划", "model": "gpt-6.1-terra"},
                {"role": "executor", "name": "执行", "model": "gpt-6.1-terra"},
                {"role": "reviewer", "name": "复核", "model": "gpt-6.1-terra"},
            ],
        }, ensure_ascii=False), encoding="utf-8")

    def add_provider(self, pid: str = "deepseek", url: str = "https://api.deepseek.com",
                     env: str = ENV) -> None:
        ok, message = api_providers.save_provider(pid, pid.title(), url, env)
        self.assertTrue(ok, message)

    def allow_key(self) -> None:
        api_providers.os.environ[ENV] = FAKE_KEY


class SaveProviderTests(ApiCase):
    def test_rejects_plain_http_to_remote_host(self) -> None:
        ok, message = api_providers.save_provider("dd", "D", "http://example.com", ENV)
        self.assertFalse(ok)
        self.assertIn("https", message)

    def test_accepts_https_and_local_http(self) -> None:
        self.assertTrue(api_providers.save_provider("aa", "A", "https://x.com", ENV)[0])
        self.assertTrue(api_providers.save_provider("bb", "B", "http://127.0.0.1:11434/v1", ENV)[0])
        self.assertTrue(api_providers.save_provider("cc", "C", "http://localhost:11434/v1", ENV)[0])

    def test_localhost_lookalike_is_not_local(self) -> None:
        """按主机名判断：`localhost.evil.com` 是外网域名，不是本机。"""
        ok, message = api_providers.save_provider("dd", "D", "http://localhost.evil.com/v1", ENV)
        self.assertFalse(ok, message)
        self.assertIn("本机", message)

    def test_file_only_stores_env_var_name(self) -> None:
        self.add_provider()
        raw = api_providers.STORE.read_text(encoding="utf-8")
        self.assertIn(ENV, raw)
        self.assertNotIn(FAKE_KEY, raw)
        saved = json.loads(raw)["deepseek"]
        self.assertEqual(set(saved), {"label", "base_url", "api_key_env", "models", "fetched_at"})


class FetchModelsTests(ApiCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_provider()
        self.allow_key()

    def test_parses_data_ids_and_filters_junk(self) -> None:
        payload = {"data": [{"id": "aa"}, {"id": "bb"}, {"id": "bad id!"}, {"noid": 1}, "x"]}
        with mock.patch.object(urllib.request, "urlopen", side_effect=_ok(payload)):
            ok, message, ids = api_providers.fetch_models("deepseek")
        self.assertTrue(ok, message)
        self.assertEqual(ids, ["aa", "bb"])

    def test_401_names_the_env_var_but_never_leaks_the_key(self) -> None:
        with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(401)):
            ok, message, ids = api_providers.fetch_models("deepseek")
        self.assertFalse(ok)
        self.assertEqual(ids, [])
        self.assertIn(ENV, message)
        self.assertNotIn(FAKE_KEY, message)
        self.assertNotIn(FAKE_KEY, api_providers.STORE.read_text(encoding="utf-8"))

    def test_404_hints_at_v1_suffix(self) -> None:
        with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(404)):
            ok, message, _ = api_providers.fetch_models("deepseek")
        self.assertFalse(ok)
        self.assertIn("/v1", message)

    def test_network_error_says_the_host(self) -> None:
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("boom")):
            ok, message, _ = api_providers.fetch_models("deepseek")
        self.assertFalse(ok)
        self.assertIn("api.deepseek.com", message)

    def test_missing_key_does_not_call_out(self) -> None:
        api_providers.os.environ.pop(ENV, None)
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=AssertionError("没 key 不许联网")):
            ok, message, _ = api_providers.fetch_models("deepseek")
        self.assertFalse(ok)
        self.assertIn(ENV, message)
        self.assertIn("重启", message)

    def test_broken_url_in_file_returns_error_instead_of_raising(self) -> None:
        """文件可以被手改坏：菜单应该报错，而不是整个 /api/state 500。"""
        api_providers.STORE.write_text(json.dumps({
            "deepseek": {"label": "D", "base_url": "不是网址", "api_key_env": ENV, "models": []},
        }), encoding="utf-8")
        ok, message, ids = api_providers.fetch_models("deepseek")
        self.assertFalse(ok)
        self.assertEqual(ids, [])
        self.assertIn("地址", message)


class RegistryTests(ApiCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_provider()
        self.allow_key()
        ok, message = api_providers.save_models("deepseek", ["deepseek-chat", "deepseek-reasoner", "bad id!"])
        self.assertTrue(ok, message)

    def test_registry_merges_prefixed_ids(self) -> None:
        reg = models.registry()
        self.assertIn("deepseek:deepseek-chat", reg)
        self.assertEqual(reg["deepseek:deepseek-chat"]["cli"], "api")
        self.assertEqual(reg["deepseek:deepseek-chat"]["provider"], "deepseek")
        self.assertEqual(reg["deepseek:deepseek-chat"]["remote_id"], "deepseek-chat")
        self.assertNotIn("bad id!", json.dumps(reg))

    def test_name_collision_keeps_the_local_cli_entry(self) -> None:
        api_providers.save_models("deepseek", ["gpt-6.1-terra"])
        reg = models.registry()
        self.assertEqual(reg["gpt-6.1-terra"]["cli"], "codex", "本机 CLI 的型号不许被覆盖")
        self.assertEqual(reg["deepseek:gpt-6.1-terra"]["cli"], "api")

    def test_missing_key_is_not_ready_and_mentions_restart(self) -> None:
        api_providers.os.environ.pop(ENV, None)
        ok, why = models.cli_ready("deepseek:deepseek-chat")
        self.assertFalse(ok)
        self.assertIn(ENV, why)
        self.assertIn("重启", why)
        models.os.environ[ENV] = FAKE_KEY
        self.assertTrue(models.cli_ready("deepseek:deepseek-chat")[0])

    def test_snapshot_keeps_cli_agents_and_adds_api(self) -> None:
        snap = models.snapshot()
        agents = {a["id"]: a for a in snap["agents"]}
        self.assertEqual(set(agents), {"codex", "claude", "deepseek", "api"})
        self.assertIn("api", snap["clis"])
        self.assertTrue(agents["api"]["found"])
        self.assertTrue(agents["api"]["works"])
        self.assertEqual(agents["api"]["providers"][0]["id"], "deepseek")
        self.assertIn("deepseek:deepseek-chat", agents["api"]["models"])

    def test_snapshot_never_contains_the_key(self) -> None:
        raw = json.dumps(models.snapshot(), ensure_ascii=False)
        self.assertNotIn(FAKE_KEY, raw)
        self.assertIn(ENV, raw)

    def test_executor_rejected_planner_allowed(self) -> None:
        ok, message = models.set_model("executor", "deepseek:deepseek-chat")
        self.assertFalse(ok)
        self.assertIn("执行者", message)
        self.assertEqual(models.overrides(), {}, "被拒的切换不能留下痕迹")
        ok, message = models.set_model("planner", "deepseek:deepseek-chat")
        self.assertTrue(ok, message)
        self.assertEqual(models.overrides(), {"planner": "deepseek:deepseek-chat"})

    def test_delete_refuses_while_in_use(self) -> None:
        ok, message = models.set_model("planner", "deepseek:deepseek-chat")
        self.assertTrue(ok, message)
        ok, message = api_providers.delete("deepseek")
        self.assertFalse(ok)
        self.assertIn("切走", message)
        models.set_model("planner", "")
        ok, message = api_providers.delete("deepseek")
        self.assertTrue(ok, message)


class ApiBridgeTests(ApiCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_provider()
        self.allow_key()
        api_providers.save_models("deepseek", ["deepseek-chat"])

    def test_request_uses_remote_model_id_and_hides_key(self) -> None:
        saved: list = []
        payload = {"choices": [{"message": {"content": "好的，我来规划。\n第二行"}}],
                   "usage": {"total_tokens": 42}}
        prefix = self.tmp / "runs" / "planner"
        with mock.patch.object(urllib.request, "urlopen", side_effect=_ok(payload, saved)):
            result = api_bridge.run_api("写个计划", out_prefix=str(prefix),
                                        model="deepseek:deepseek-chat")
        self.assertTrue(result["ok"], result["summary"])
        self.assertEqual(result["model"], "deepseek-chat")
        self.assertEqual(result["agent"], "api")
        self.assertEqual(result["tokens"], 42)
        body = json.loads(saved[0].data.decode("utf-8"))
        self.assertEqual(body["model"], "deepseek-chat", "发出去的必须是远端型号，不是带前缀的注册表 id")
        self.assertEqual(saved[0].get_header("Authorization"), f"Bearer {FAKE_KEY}")
        last = Path(result["last"]).read_text(encoding="utf-8")
        self.assertIn("好的，我来规划", last)
        for path in (result["last"], result["stdout"], result["stderr"]):
            self.assertNotIn(FAKE_KEY, Path(path).read_text(encoding="utf-8"))

    def test_at_capacity_is_recognizable_for_fallback(self) -> None:
        from agent_team import fallback
        with mock.patch.object(urllib.request, "urlopen", side_effect=_http_error(429)):
            result = api_bridge.run_api("x", out_prefix=str(self.tmp / "runs" / "r"),
                                        model="deepseek:deepseek-chat")
        self.assertFalse(result["ok"])
        self.assertTrue(fallback.is_capacity_error(result), "429 要能被降级链认出来")

    def test_no_key_fails_before_network(self) -> None:
        api_providers.os.environ.pop(ENV, None)
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=AssertionError("没 key 不许联网")):
            result = api_bridge.run_api("x", out_prefix=str(self.tmp / "runs" / "r"),
                                        model="deepseek:deepseek-chat")
        self.assertFalse(result["ok"])
        self.assertIn(ENV, result["summary"])
        self.assertNotIn(FAKE_KEY, result["summary"])

    def test_broken_url_returns_result_instead_of_raising(self) -> None:
        api_providers.STORE.write_text(json.dumps({
            "deepseek": {"label": "D", "base_url": "不是网址", "api_key_env": ENV,
                         "models": ["deepseek-chat"]},
        }), encoding="utf-8")
        result = api_bridge.run_api("x", out_prefix=str(self.tmp / "runs" / "bad"),
                                    model="deepseek:deepseek-chat")
        self.assertFalse(result["ok"])
        self.assertIn("地址", result["summary"])


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


class RouteAuthTests(ApiCase):
    """`/api/api-provider/*` 和别的写接口一样：不带令牌一律挡掉。"""

    def setUp(self) -> None:
        super().setUp()
        self.token = "t" * 32
        self._web = {
            "TOKEN": room_web.TOKEN, "TOKEN_FILE": room_web.TOKEN_FILE,
            "CODE_FILE": room_web.CODE_FILE, "ROOM": room_web.ROOM,
            "TASKS": room_web.TASKS, "PROJECT_FILE": room_web.PROJECT_FILE,
            "TYPING_FILE": room_web.TYPING_FILE, "NUDGE_FILE": room_web.NUDGE_FILE,
            "SESSION_STATE": room_web.SESSION_STATE, "STORE": room_web.STORE,
        }
        room_web.TOKEN = self.token
        room_web.ROOM = self.tmp / "webroom"
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
        self.server = room_web.ThreadingHTTPServer(("127.0.0.1", 0), room_web.Handler)
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for key, value in self._web.items():
            setattr(room_web, key, value)
        super().tearDown()

    def request(self, path: str, body: dict | None = None, token: str | None = None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data,
                                     method="POST" if body is not None else "GET")
        req.add_header("X-Team-Token", self.token if token is None else token)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8")

    def test_requires_token(self) -> None:
        for path, body in (("/api/api-provider/save", {"id": "d"}),
                           ("/api/api-provider/fetch", {"id": "d"}),
                           ("/api/api-provider/models", {"id": "d", "models": []}),
                           ("/api/api-provider/delete", {"id": "d"})):
            status, _ = self.request(path, body, token="")
            self.assertEqual(status, 401, path)

    def test_save_models_roundtrip_and_state_has_no_key(self) -> None:
        self.allow_key()
        status, raw = self.request("/api/api-provider/save", {
            "id": "deepseek", "label": "DeepSeek",
            "base_url": "https://api.deepseek.com", "api_key_env": ENV})
        self.assertEqual(status, 200, raw)
        self.assertNotIn(FAKE_KEY, raw)
        status, raw = self.request("/api/api-provider/models", {
            "id": "deepseek", "models": ["deepseek-chat"]})
        self.assertEqual(status, 200, raw)
        self.assertIn("deepseek:deepseek-chat", raw)
        status, raw = self.request("/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(FAKE_KEY, raw)
        self.assertIn("deepseek:deepseek-chat", raw)

    def test_fetch_route_does_not_need_network(self) -> None:
        self.add_provider()
        self.allow_key()
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=_ok({"data": [{"id": "m1"}]})):
            status, raw = self.request("/api/api-provider/fetch", {"id": "deepseek"})
        self.assertEqual(status, 200, raw)
        self.assertIn("m1", raw)
        self.assertNotIn(FAKE_KEY, raw)     # 假 key 进了请求头，也不能回显


if __name__ == "__main__":
    unittest.main()