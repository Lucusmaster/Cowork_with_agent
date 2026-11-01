"""本机 Agent CLI：识别粘贴、保存校验、查找顺序、执行分流（claude -p）。

全程隔离：登记文件指到临时目录、常见安装目录清空、CODEX_EXE/CLAUDE_EXE 不继承，
所以开发机上装没装 claude 都不影响结果。真正"跑起来"用的是一个假 claude（打印 --version、
把收到的参数和 stdin 记下来、按 Claude Code 的 JSON 格式回结果）。
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

from agent_team import agent_clis, codex_bridge, models

FAKE_PY = r'''
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print("2.0.31 (Claude Code)"); sys.exit(int(os.environ.get("FAKE_VERSION_RC", "0")))
# stdin 上是 UTF-8 字节；裸 sys.stdin.read() 会按 Windows 本地代码页解码（中文机器上是 GBK），
# 于是桥送出去的中文提示词在断言里变成乱码。这里按字节读、显式 UTF-8 解。
data = sys.stdin.buffer.read().decode("utf-8", "replace")
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "w", encoding="utf-8") as fh:
        json.dump({"argv": sys.argv[1:], "stdin": data, "cwd": os.getcwd()}, fh)
if os.environ.get("FAKE_PLAIN"):
    print("just text\nPASS"); sys.exit(0)
err = bool(os.environ.get("FAKE_ERROR"))
print(json.dumps({"type": "result", "is_error": err, "result": "额度用完了" if err else "PASS\n看过了",
                  "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5}}))
'''


def make_fake_claude(folder: Path) -> Path:
    script = folder / "fake_claude.py"
    script.write_text(FAKE_PY, encoding="utf-8")
    if os.name == "nt":
        exe = folder / "claude.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = folder / "claude"
        exe.write_text(f"#!{sys.executable}\n" + FAKE_PY, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


class IsolatedCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self._saved = (agent_clis.STORE, models.CONFIG, models.OVERRIDES)
        agent_clis.STORE = self.tmp / "room" / "agent_clis.json"
        models.CONFIG = self.tmp / "team.json"
        models.OVERRIDES = self.tmp / "room" / "model_overrides.json"
        models.CONFIG.write_text(json.dumps({"model_registry": {
            "gpt-6.1-terra": {"cli": "codex"},
            "claude-sonnet-4-5": {"cli": "claude"},
            "ds-flash": {"cli": "deepseek"},
        }, "executor": {"model": "gpt-6.1-terra"}}), encoding="utf-8")
        agent_clis._PROBES.clear()
        self.patches = [
            mock.patch.object(agent_clis, "_common_candidates", return_value=[]),
            mock.patch.object(agent_clis.shutil, "which", return_value=None),
            mock.patch.dict(os.environ, {"FAKE_CLAUDE_LOG": str(self.tmp / "call.json")}),
        ]
        for p in self.patches:
            p.start()
        for key in ("CODEX_EXE", "CLAUDE_EXE", "DEEPSEEK_EXE", "FAKE_ERROR", "FAKE_PLAIN", "FAKE_VERSION_RC"):
            os.environ.pop(key, None)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        agent_clis.STORE, models.CONFIG, models.OVERRIDES = self._saved
        agent_clis._PROBES.clear()
        self.tmpdir.cleanup()

    def install_claude(self) -> Path:
        exe = make_fake_claude(self.bin)
        ok, message, _ = agent_clis.save("claude", str(exe))
        self.assertTrue(ok, message)
        return exe

    def last_call(self) -> dict:
        return json.loads((self.tmp / "call.json").read_text(encoding="utf-8"))


class ParsePasteTests(unittest.TestCase):
    """识别粘贴是纯文本处理：不看本机、不执行，所以不需要隔离。"""

    def test_where_output_prefers_cmd_over_shim_and_ps1(self) -> None:
        text = ("C:\\Users\\me\\AppData\\Roaming\\npm\\claude\n"
                "C:\\Users\\me\\AppData\\Roaming\\npm\\claude.cmd\n"
                "C:\\Users\\me\\AppData\\Roaming\\npm\\claude.ps1\n")
        out = agent_clis.parse_paste("claude", text, nt=True)
        self.assertEqual(out["paths"][0]["path"], "C:\\Users\\me\\AppData\\Roaming\\npm\\claude.cmd")
        usable = {p["path"].rsplit("\\", 1)[-1]: p["usable"] for p in out["paths"]}
        self.assertEqual(usable, {"claude.cmd": True, "claude": False, "claude.ps1": False})

    def test_free_text_answer_with_spaces_backticks_and_chinese(self) -> None:
        text = ("我的路径是 `C:\\Program Files\\nodejs\\claude.cmd`。可用模型：\n"
                "- claude-opus-4-1-20250805\n- `opus`\n你也可以 claude --model haiku 试试。"
                "Claude Code 版本 2.0.31。gpt-5 不归我管。")
        out = agent_clis.parse_paste("claude", text, nt=True)
        self.assertEqual([p["path"] for p in out["paths"]], ["C:\\Program Files\\nodejs\\claude.cmd"])
        self.assertEqual(out["models"][0], "claude-opus-4-1-20250805", "完整 id 排在简称前面")
        self.assertIn("opus", out["models"])
        self.assertIn("haiku", out["models"])
        self.assertNotIn("claude-code", [m.lower() for m in out["models"]], "产品名不是模型")
        self.assertEqual(out["other_models"], ["gpt-5"])
        self.assertEqual(out["ignored_paths"], [], "C:\\Program 这种碎片不该冒出来")

    def test_posix_path_and_trailing_punctuation(self) -> None:
        out = agent_clis.parse_paste("claude", "it's at /home/u/.local/bin/claude, use claude-sonnet-4-5.",
                                     nt=False)
        self.assertEqual([p["path"] for p in out["paths"]], ["/home/u/.local/bin/claude"])
        self.assertTrue(out["paths"][0]["usable"])
        self.assertEqual(out["models"], ["claude-sonnet-4-5"])

    def test_other_agents_path_is_flagged_not_taken(self) -> None:
        out = agent_clis.parse_paste("claude", "C:\\tools\\codex.exe gpt-5-codex o3\n", nt=True)
        self.assertEqual(out["paths"], [])
        self.assertEqual(out["ignored_paths"], ["C:\\tools\\codex.exe"])
        self.assertEqual(out["other_models"], ["gpt-5-codex", "o3"])

    def test_codex_families(self) -> None:
        out = agent_clis.parse_paste("codex", "models: gpt-5, gpt-5-codex, o3, o4-mini, codex-mini-latest")
        self.assertEqual(out["models"], ["gpt-5", "gpt-5-codex", "o3", "o4-mini", "codex-mini-latest"])

    def test_nothing_recognized_gives_actionable_hint(self) -> None:
        out = agent_clis.parse_paste("claude", "我不知道，你自己查一下吧")
        self.assertEqual((out["paths"], out["models"]), ([], []))
        self.assertIn(agent_clis.guide("claude")["command"], out["hint"])

    def test_unknown_agent_raises(self) -> None:
        with self.assertRaises(ValueError):
            agent_clis.parse_paste("gemini", "x")

    def test_guide_asks_for_path_and_model_ids(self) -> None:
        g = agent_clis.guide("claude")
        self.assertIn("claude", g["command"])
        self.assertIn("完整路径", g["ask"])
        self.assertIn("--model", g["ask"])


class SaveTests(IsolatedCase):
    def test_save_probes_and_persists(self) -> None:
        exe = make_fake_claude(self.bin)
        ok, message, row = agent_clis.save("claude", f'"{exe}"', ["claude-sonnet-4-5", "opus"])
        self.assertTrue(ok, message)
        self.assertTrue(row["works"])
        self.assertIn("2.0.31", row["version"])
        self.assertEqual(row["source"], "已保存")
        stored = json.loads(agent_clis.STORE.read_text(encoding="utf-8"))
        self.assertEqual(stored["claude"], {"path": str(exe), "models": ["claude-sonnet-4-5", "opus"]})
        self.assertEqual(list(agent_clis.STORE.parent.glob(".agent-clis-*")), [], "临时文件要清干净")

    def test_wrong_binary_name_is_rejected(self) -> None:
        other = self.bin / ("notepad.exe" if os.name == "nt" else "notepad")
        other.write_text("x", encoding="utf-8")
        ok, message, _ = agent_clis.save("claude", str(other))
        self.assertFalse(ok)
        self.assertIn("不是 Claude Code 的命令", message)
        self.assertFalse(agent_clis.STORE.exists())

    def test_missing_file_is_rejected(self) -> None:
        ok, message, _ = agent_clis.save("claude", str(self.bin / "claude"), nt=False)
        self.assertFalse(ok)
        self.assertIn("不存在", message)

    def test_windows_ps1_or_bare_shim_is_rejected(self) -> None:
        ok, message, _ = agent_clis.save("claude", "C:\\npm\\claude.ps1", nt=True)
        self.assertFalse(ok)
        self.assertIn(".cmd", message)

    def test_failing_version_is_rejected(self) -> None:
        exe = make_fake_claude(self.bin)
        with mock.patch.dict(os.environ, {"FAKE_VERSION_RC": "3"}):
            ok, message, _ = agent_clis.save("claude", str(exe))
        self.assertFalse(ok)
        self.assertIn("跑不起来", message)
        self.assertFalse(agent_clis.STORE.exists())

    def test_unsafe_model_id_is_rejected(self) -> None:
        exe = make_fake_claude(self.bin)
        ok, message, _ = agent_clis.save("claude", str(exe), ["claude-x & del *"])
        self.assertFalse(ok)
        self.assertIn("不像模型 id", message)

    def test_models_only_needs_existing_path(self) -> None:
        ok, message, _ = agent_clis.save("claude", "", ["claude-sonnet-4-5"])
        self.assertFalse(ok, "本机找不到它时只存模型没有意义")
        self.install_claude()
        ok, _, _ = agent_clis.save("claude", "", ["claude-haiku-4-5"])
        self.assertTrue(ok)
        self.assertIn("claude-haiku-4-5", agent_clis.saved_models("claude"))
        self.assertIn("claude-haiku-4-5", models.registry(), "登记的模型要进注册表")
        self.assertEqual(models.registry()["claude-haiku-4-5"]["cli"], "claude")


class ResolveAndReadyTests(IsolatedCase):
    def test_saved_path_wins_and_falls_back_when_deleted(self) -> None:
        exe = self.install_claude()
        self.assertEqual(agent_clis.resolve_path("claude"), (str(exe), "已保存"))
        exe.unlink()
        with mock.patch.dict(os.environ, {"CLAUDE_EXE": str(make_fake_claude(self.tmp))}):
            self.assertEqual(agent_clis.resolve_path("claude")[1], "环境变量")

    def test_status_does_not_execute_anything(self) -> None:
        exe = make_fake_claude(self.bin)
        with mock.patch.dict(os.environ, {"CLAUDE_EXE": str(exe)}), \
                mock.patch.object(agent_clis, "probe") as probe:
            row = agent_clis.status("claude")
        probe.assert_not_called()
        self.assertTrue(row["found"])
        self.assertFalse(row["checked"], "轮询只看文件，没点检测就不算验过")

    def test_missing_claude_blocks_switch_with_guidance(self) -> None:
        ok, message = models.set_model("executor", "claude-sonnet-4-5")
        self.assertFalse(ok)
        self.assertIn("换模型菜单", message)

    def test_failed_probe_blocks_switch(self) -> None:
        exe = make_fake_claude(self.bin)
        with mock.patch.dict(os.environ, {"CLAUDE_EXE": str(exe), "FAKE_VERSION_RC": "1"}):
            self.assertFalse(agent_clis.detect("claude")["works"])
            ok, message = models.set_model("executor", "claude-sonnet-4-5")
        self.assertFalse(ok)
        self.assertIn("跑不起来", message)

    def test_installed_claude_can_be_switched_to(self) -> None:
        self.install_claude()
        ok, message = models.set_model("reviewer", "claude-sonnet-4-5")
        self.assertTrue(ok, message)

    def test_deepseek_found_but_not_runnable(self) -> None:
        ds = self.bin / ("deepseek.cmd" if os.name == "nt" else "deepseek")
        ds.write_text("x", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DEEPSEEK_EXE": str(ds)}):
            row = agent_clis.status("deepseek")
            ok, message = models.set_model("executor", "ds-flash")
        self.assertTrue(row["found"])
        self.assertFalse(row["runnable"])
        self.assertFalse(ok)
        self.assertIn("执行适配", message)

    def test_tester_is_not_switchable(self) -> None:
        ok, message = models.set_model("tester", "gpt-6.1-terra")
        self.assertFalse(ok)
        self.assertIn("不调用模型", message)
        roles = {r["role"]: r for r in models.snapshot()["roles"]}
        self.assertFalse(roles["tester"]["switchable"])
        self.assertTrue(roles["planner"]["switchable"])

    def test_snapshot_lists_agents_with_their_models(self) -> None:
        agents = {a["id"]: a for a in models.snapshot()["agents"]}
        self.assertEqual(set(agents), {"codex", "claude", "deepseek"})
        self.assertIn("claude-sonnet-4-5", agents["claude"]["models"])
        self.assertIn("gpt-6.1-terra", agents["codex"]["models"])
        self.assertIn("ask", agents["claude"]["guide"])


class RoutingTests(IsolatedCase):
    """run_codex 是所有角色调模型的唯一入口：按注册表的 cli 分流。"""

    def run_model(self, model: str, sandbox: str, **kw) -> dict:
        return codex_bridge.run_codex("审一下这个 diff", workspace=str(self.tmp),
                                      out_prefix=str(self.tmp / "out" / "r"), sandbox=sandbox,
                                      model=model, **kw)

    def test_reviewer_on_claude_is_read_only(self) -> None:
        self.install_claude()
        result = self.run_model("claude-sonnet-4-5", "read-only", reasoning_effort="low")
        self.assertTrue(result["ok"], result["summary"])
        self.assertEqual(result["agent"], "claude")
        self.assertEqual(Path(result["last"]).read_text(encoding="utf-8"), "PASS\n看过了")
        self.assertEqual(result["tokens"], 125)
        self.assertIn("没有推理档", result["reasoning_effort"])
        call = self.last_call()
        self.assertEqual(call["stdin"], "审一下这个 diff", "提示词走 stdin，不进命令行")
        self.assertEqual(Path(call["cwd"]).resolve(), self.tmp.resolve())
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-4-5")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "default")
        denied = argv[argv.index("--disallowedTools") + 1:]
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit", "Bash"):
            self.assertIn(tool, denied)

    def test_executor_on_claude_can_edit_but_not_run_commands(self) -> None:
        self.install_claude()
        self.assertTrue(self.run_model("claude-sonnet-4-5", "workspace-write")["ok"])
        argv = self.last_call()["argv"]
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--disallowedTools") + 1:], ["Bash"])

    def test_claude_reported_error_is_not_ok(self) -> None:
        self.install_claude()
        with mock.patch.dict(os.environ, {"FAKE_ERROR": "1"}):
            result = self.run_model("claude-sonnet-4-5", "read-only")
        self.assertFalse(result["ok"])
        self.assertIn("额度用完了", result["summary"])

    def test_plain_text_output_still_lands_in_last(self) -> None:
        self.install_claude()
        with mock.patch.dict(os.environ, {"FAKE_PLAIN": "1"}):
            result = self.run_model("claude-sonnet-4-5", "read-only")
        self.assertTrue(result["ok"])
        self.assertIn("PASS", Path(result["last"]).read_text(encoding="utf-8"))

    def test_missing_claude_fails_without_touching_codex(self) -> None:
        with mock.patch.object(codex_bridge, "find_codex", side_effect=AssertionError("不该找 codex")):
            result = self.run_model("claude-sonnet-4-5", "read-only")
        self.assertFalse(result["ok"])
        self.assertIn("找不到 claude", result["summary"])

    def test_cli_without_adapter_never_falls_back_to_codex(self) -> None:
        with mock.patch.object(codex_bridge, "find_codex", side_effect=AssertionError("不该找 codex")):
            result = self.run_model("ds-flash", "workspace-write")
        self.assertFalse(result["ok"])
        self.assertIn("执行适配", result["summary"])
        self.assertTrue(Path(result["stderr"]).is_file(), "失败原因要落盘，任务卡能看到")

    def test_codex_models_still_go_to_codex(self) -> None:
        self.assertEqual(codex_bridge._route("gpt-6.1-terra"), "codex")
        self.assertEqual(codex_bridge._route(""), "codex")
        self.assertEqual(codex_bridge._route("never-registered"), "codex", "老配置零改动")

    def test_unsafe_model_id_never_reaches_command_line(self) -> None:
        from agent_team import claude_bridge
        self.install_claude()
        result = claude_bridge.run_claude("x", workspace=str(self.tmp), out_prefix=str(self.tmp / "u"),
                                          model="x & del *")
        self.assertFalse(result["ok"])
        self.assertFalse((self.tmp / "call.json").exists())


class PlannerWiringTests(IsolatedCase):
    def test_planner_reads_runtime_switch(self) -> None:
        from agent_team import worker
        self.assertEqual(worker._planner_model()[0], "gpt-6.1-terra", "没配就用老默认")
        self.install_claude()
        ok, message = models.set_model("planner", "claude-sonnet-4-5")
        self.assertTrue(ok, message)
        self.assertEqual(worker._planner_model()[0], "claude-sonnet-4-5")

    def test_planner_functions_no_longer_hardcode_the_model(self) -> None:
        import inspect

        from agent_team import worker
        for func in (worker.plan_with_codex, worker.plan_continue_with_codex):
            src = inspect.getsource(func)
            self.assertNotIn('model="gpt-6.1-terra"', src, func.__name__)
            self.assertIn("_planner_model()", src, func.__name__)


if __name__ == "__main__":
    unittest.main()
