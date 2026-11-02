"""执行者端点覆盖（model_providers）与"连不上模型端点"的识别。

背景：这台机器直连 OpenAI 超时，`codex exec` 在 127.0.0.1 的本地中转没开时
会连挂两轮、每轮空烧四分钟，用户只看到"写完 plan.md 就停了"。
这里守住两件事：

- 型号可以在 `room/model_providers.json` 里单独指定 base_url / 密钥，桥把它翻成
  `codex -c model_provider=...` 覆盖并注入密钥，不碰全局 `~/.codex/config.toml`；
- 真连不上时桥留一张 `.hint.txt`，编排据此立刻停手，不再跑第二轮。

全程用假 codex（记录 argv/stdin/env），不联网。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import api_providers, codex_bridge, models, worker

FAKE_CODEX_PY = r'''
import json, os, sys
argv = sys.argv[1:]
data = sys.stdin.buffer.read().decode("utf-8", "replace")
last = ""
if "--output-last-message" in argv:
    last = argv[argv.index("--output-last-message") + 1]
log = os.environ.get("FAKE_CODEX_LOG")
if log:
    with open(log, "w", encoding="utf-8") as fh:
        json.dump({"argv": argv, "stdin": data,
                   "key": os.environ.get("XUNNIA_API_KEY", ""),
                   "codex_home": os.environ.get("CODEX_HOME", "")}, fh)
msg = os.environ.get("FAKE_CODEX_MSG", "## 做了什么\n- 写好了")
if last:
    with open(last, "w", encoding="utf-8") as fh:
        fh.write(msg)
err = os.environ.get("FAKE_CODEX_ERR")
if err:
    sys.stderr.write(err)
sys.exit(int(os.environ.get("FAKE_CODEX_RC", "0")))
'''

PROVIDERS = {
    "model_providers": {
        "claude-sonnet-5": {
            "name": "Xunnia",
            "base_url": "https://api.example.com/v1",
            "env_key": "XUNNIA_API_KEY",
            "key_store": "xunnia",
            "wire_api": "responses",
        }
    }
}


def make_fake_codex(folder: Path) -> Path:
    script = folder / "fake_codex.py"
    script.write_text(FAKE_CODEX_PY, encoding="utf-8")
    if os.name == "nt":
        exe = folder / "codex.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = folder / "codex"
        exe.write_text(f"#!{sys.executable}\n" + FAKE_CODEX_PY, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


class ProviderCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self._saved = (models.CONFIG, models.OVERRIDES, models.PROVIDERS,
                       api_providers.STORE, api_providers.KEYS, worker.TASKS)
        models.CONFIG = self.tmp / "team.json"
        models.OVERRIDES = self.tmp / "room" / "model_overrides.json"
        models.PROVIDERS = self.tmp / "room" / "model_providers.json"
        api_providers.STORE = self.tmp / "room" / "api_providers.json"
        api_providers.KEYS = self.tmp / "room" / "api_keys.json"
        worker.TASKS = self.tmp / "tasks"
        models.CONFIG.write_text(json.dumps({"model_registry": {}}), encoding="utf-8")
        models.OVERRIDES.parent.mkdir(parents=True, exist_ok=True)

        self.codex = make_fake_codex(self.tmp)
        self.log = self.tmp / "codex-call.json"
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.codex_home = self.tmp / "codexhome"
        self.codex_home.mkdir()
        self._env = mock.patch.dict(os.environ, {
            "CODEX_EXE": str(self.codex),
            "CODEX_HOME": str(self.codex_home),
            "FAKE_CODEX_LOG": str(self.log),
            "XUNNIA_API_KEY": "",
        })
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        (models.CONFIG, models.OVERRIDES, models.PROVIDERS,
         api_providers.STORE, api_providers.KEYS, worker.TASKS) = self._saved
        self.tmpdir.cleanup()

    def write_providers(self, data: dict = PROVIDERS) -> None:
        models.PROVIDERS.write_text(json.dumps(data), encoding="utf-8")

    def call(self) -> dict:
        return json.loads(self.log.read_text(encoding="utf-8"))

    # ---------- 端点覆盖 ----------

    def test_model_provider_reads_room_table(self) -> None:
        self.write_providers()
        spec = models.model_provider("claude-sonnet-5")
        self.assertEqual(spec["base_url"], "https://api.example.com/v1")
        self.assertEqual(spec["wire_api"], "responses")
        # 没配过的型号不许瞎编端点
        self.assertEqual(models.model_provider("gpt-6.1-sol"), {})

    def test_unknown_model_without_endpoint_is_left_alone(self) -> None:
        self.assertEqual(codex_bridge.provider_overrides("gpt-6.1-sol"), ([], {}))

    def test_overrides_become_codex_config_flags(self) -> None:
        self.write_providers()
        args, env = codex_bridge.provider_overrides("claude-sonnet-5")
        joined = " ".join(args)
        self.assertIn("model_provider=workbench", joined)
        self.assertIn('model_providers.workbench.base_url="https://api.example.com/v1"', joined)
        self.assertIn('model_providers.workbench.wire_api="responses"', joined)
        self.assertIn("model_providers.workbench.env_key=XUNNIA_API_KEY", joined)
        # 没有 key 时不往环境里塞空值
        self.assertEqual(env, {})

    def test_key_comes_from_store_and_lands_in_child_env(self) -> None:
        self.write_providers()
        api_providers.KEYS.parent.mkdir(parents=True, exist_ok=True)
        api_providers.KEYS.write_text(json.dumps({"xunnia": "sk-from-store"}), encoding="utf-8")
        args, env = codex_bridge.provider_overrides("claude-sonnet-5")
        self.assertEqual(env, {"XUNNIA_API_KEY": "sk-from-store"})
        self.assertTrue(args)

    def test_run_codex_passes_provider_and_key(self) -> None:
        self.write_providers()
        api_providers.KEYS.parent.mkdir(parents=True, exist_ok=True)
        api_providers.KEYS.write_text(json.dumps({"xunnia": "sk-from-store"}), encoding="utf-8")
        result = codex_bridge.run_codex(
            "写个文件", workspace=str(self.ws), out_prefix=str(self.tmp / "exec-01"),
            timeout=30, model="claude-sonnet-5", fallback_models=[])
        self.assertTrue(result["ok"], result)
        call = self.call()
        self.assertIn("model_provider=workbench", " ".join(call["argv"]))
        self.assertEqual(call["key"], "sk-from-store")
        self.assertEqual(result["endpoint"], "https://api.example.com/v1")

    # ---------- 连不上要立刻停手 ----------

    def test_network_failure_is_classified(self) -> None:
        hint = codex_bridge.classify_env_failure(
            stderr="ERROR: workspace routing discovery timed out")
        self.assertIn("连不上模型端点", hint)
        # 提示必须能照着做：先查网络/VPN，再给"换模型"和"配中转"两条替代路
        self.assertIn("VPN", hint)
        self.assertIn("/model 执行", hint)
        self.assertIn("model_providers.json", hint)
        self.assertIn("OpenAI 默认端点", hint)

    def test_hint_names_the_configured_endpoint(self) -> None:
        hint = codex_bridge.classify_env_failure(
            stderr="failed to connect", endpoint="https://api.example.com/v1")
        self.assertIn("https://api.example.com/v1", hint)
        self.assertNotIn("OpenAI 默认端点", hint)

    def test_ordinary_failure_is_not_an_endpoint_problem(self) -> None:
        self.assertEqual(codex_bridge.classify_env_failure(stderr="assert 1 == 2 failed"), "")
        self.assertEqual(codex_bridge.classify_env_failure(stderr="timed out", timedout=True), "")

    def test_failed_run_leaves_hint_file(self) -> None:
        self.write_providers()
        env = mock.patch.dict(os.environ, {
            "FAKE_CODEX_RC": "1",
            "FAKE_CODEX_ERR": "ERROR: workspace routing discovery timed out",
            "FAKE_CODEX_MSG": "",
        })
        with env:
            result = codex_bridge.run_codex(
                "写个文件", workspace=str(self.ws), out_prefix=str(self.tmp / "exec-01"),
                timeout=30, model="claude-sonnet-5", fallback_models=[])
        self.assertFalse(result["ok"])
        self.assertIn("连不上模型端点", result["env_error"])
        self.assertIn("VPN", result["env_error"])
        self.assertTrue(Path(result["hint"]).is_file())

    def test_report_present_means_not_an_endpoint_failure(self) -> None:
        """尾部的 MCP 通道报错不算"连不上"：文件已经改完了，别拦着流水线往下走。

        T-261102-200022 实测：Codex 退出码 1，但最后一条消息是完整的"做了什么/验证"，
        stderr 里只有它自带的 `.../backend-api/ps/mcp` 传输错误。这种情况必须放行到验收。
        """
        self.write_providers()
        env = mock.patch.dict(os.environ, {
            "FAKE_CODEX_RC": "1",
            "FAKE_CODEX_ERR": ("ERROR rmcp::transport::worker: worker quit with fatal: "
                               "Transport channel closed, when Client(HttpRequest(HttpRequest("
                               "\"http/request failed: error sending request for url "
                               "(https://chatgpt.com/backend-api/ps/mcp)\")))"),
            "FAKE_CODEX_MSG": "## 做了什么\n- pelican_bicycle.html: 写好了\n\n## 验证\n- 通过",
        })
        with env:
            result = codex_bridge.run_codex(
                "写个文件", workspace=str(self.ws), out_prefix=str(self.tmp / "exec-02"),
                timeout=30, model="claude-sonnet-5", fallback_models=[])
        self.assertEqual(result["env_error"], "")
        self.assertFalse(Path(str(self.tmp / "exec-02") + ".hint.txt").exists())

    def test_env_hint_reader(self) -> None:
        tdir = worker.TASKS / "T-test"
        tdir.mkdir(parents=True)
        self.assertEqual(worker._env_hint("T-test", 1), "")
        (tdir / "exec-01.hint.txt").write_text("执行者连不上模型端点\n", encoding="utf-8")
        self.assertIn("连不上模型端点", worker._env_hint("T-test", 1))
        self.assertEqual(worker._env_hint("T-test", 2), "")

    def test_success_round_ignores_stale_hint(self) -> None:
        """降级成功了就不该再按上一型号留下的便签停手（T-261102-205317 踩过）。"""
        tdir = worker.TASKS / "T-gate"
        tdir.mkdir(parents=True)
        (tdir / "exec-02.hint.txt").write_text("执行者连不上模型端点\n", encoding="utf-8")
        self.assertEqual(worker._env_gate("T-gate", 2, 0), "")          # 跑成了：忽略便签
        self.assertIn("连不上模型端点", worker._env_gate("T-gate", 2, 1))  # 没跑成：停手

    def test_successful_run_removes_stale_hint_file(self) -> None:
        stale = self.tmp / "exec-05.hint.txt"
        stale.write_text("执行者连不上模型端点\n", encoding="utf-8")
        ok_result = {"ok": True, "hint": str(stale), "env_error": "执行者连不上模型端点"}
        with mock.patch.object(codex_bridge.fallback, "run_with_fallback", return_value=ok_result):
            result = codex_bridge.run_codex(
                "写个文件", workspace=str(self.ws), out_prefix=str(self.tmp / "exec-05"),
                timeout=30, model="claude-sonnet-5", fallback_models=["claude-opus-4-8"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["env_error"], "")
        self.assertFalse(stale.exists(), "成功之后还留着便签，会把下一轮编排带偏")


if __name__ == "__main__":
    unittest.main()