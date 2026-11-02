"""模型容量满载时的降级重试。

回归 T-261102-012458：执行者两轮都在 96k tokens 之后空手而归，日志里只有
`ERROR: Selected model is at capacity. Please try a different model.`——
协作台把它当普通失败，下一轮拿同一张卡、同一个打满的模型再撞一次。

这里守三件事：
- 只认"容量/限流"这一种失败才换模型，别的失败不许拿另一个模型重跑；
- 降级链去重、排掉主模型自身，配置里写别名也认；
- 换过模型就在结果里留证据（`fallback_used` / `fallback_note`），任务卡和群里都看得见。
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import claude_bridge, codex_bridge, fallback, models, team, worker

CAPACITY_LINE = "ERROR: Selected model is at capacity. Please try a different model."

FAKE_CODEX = '''
import os, pathlib, sys
args = sys.argv[1:]
model, last = "", ""
i = 0
while i < len(args):
    if args[i] == "--model":
        model = args[i + 1]; i += 2; continue
    if args[i] == "--output-last-message":
        last = args[i + 1]; i += 2; continue
    i += 1
prompt = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
capacity = os.environ.get("FAKE_CAPACITY_MODEL", "primary-model")
if capacity == "*" or model == capacity:
    sys.stderr.write("ERROR: Selected model is at capacity. Please try a different model.\\n")
    sys.exit(1)
pathlib.Path(last).write_text("done: " + prompt, encoding="utf-8")
print("ok")
'''


def make_fake_codex(folder: Path) -> Path:
    """一个只会两种回答的假 codex：FAKE_CAPACITY_MODEL 报容量满载，别的都成功。"""
    script = folder / "fake_codex.py"
    script.write_text(FAKE_CODEX, encoding="utf-8")
    if os.name == "nt":
        exe = folder / "codex.cmd"
        exe.write_text(f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = folder / "codex"
        exe.write_text(f"#!{sys.executable}\n" + FAKE_CODEX, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return exe


def make_result(tmp: Path, ok: bool = False, exit_code: int = 1,
                stderr: str = "", stdout: str = "", summary: str = "", model: str = "m") -> dict:
    """造一份和 run_codex 同形的结果：stdout/stderr 字段是**文件路径**。"""
    err_path = tmp / f"err-{len(list(tmp.iterdir()))}.txt"
    err_path.write_text(stderr, encoding="utf-8")
    out_path = tmp / f"out-{len(list(tmp.iterdir()))}.txt"
    out_path.write_text(stdout, encoding="utf-8")
    return {"ok": ok, "exit": exit_code, "timedout": False, "seconds": 1,
            "last": "", "stdout": str(out_path), "stderr": str(err_path),
            "summary": summary, "model": model, "tokens": None}


class CapacityDetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_capacity_in_stderr(self) -> None:
        self.assertTrue(fallback.is_capacity_error(make_result(self.tmp, stderr=CAPACITY_LINE)))

    def test_capacity_in_stdout(self) -> None:
        self.assertTrue(fallback.is_capacity_error(
            make_result(self.tmp, stdout="429 rate limit exceeded, retry later")))

    def test_capacity_in_summary(self) -> None:
        self.assertTrue(fallback.is_capacity_error(
            make_result(self.tmp, summary="Selected model is at capacity")))

    def test_capacity_case_insensitive(self) -> None:
        self.assertTrue(fallback.is_capacity_error(
            make_result(self.tmp, stderr="selected model is AT CAPACITY")))

    def test_ordinary_failure_is_not_capacity(self) -> None:
        for text in ("apply_patch verification failed: invalid patch",
                     "SyntaxError: invalid syntax",
                     "ModuleNotFoundError: No module named 'pytest'"):
            with self.subTest(text=text):
                self.assertFalse(fallback.is_capacity_error(make_result(self.tmp, stderr=text)))

    def test_missing_log_file_does_not_raise(self) -> None:
        broken = {"ok": False, "exit": 1, "stderr": "C:\\nope\\missing.err.txt", "stdout": "", "last": ""}
        self.assertFalse(fallback.is_capacity_error(broken))


class AttemptModelsTests(unittest.TestCase):
    def test_primary_first_then_fallbacks(self) -> None:
        self.assertEqual(fallback.attempt_models("a", ["b", "c"]), ["a", "b", "c"])

    def test_empty_and_duplicate_entries_are_dropped(self) -> None:
        self.assertEqual(fallback.attempt_models("a", ["", "a", "A", " b ", "b"]), ["a", "b"])

    def test_no_fallback_is_a_single_attempt(self) -> None:
        self.assertEqual(fallback.attempt_models("a"), ["a"])
        self.assertEqual(fallback.attempt_models("", None), [""])


class RunWithFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _scripted(self, *results):
        calls: list[tuple[str, int]] = []

        def attempt(model: str, index: int) -> dict:
            calls.append((model, index))
            return results[index]

        return attempt, calls

    def test_capacity_falls_back_and_reports_which_model_ran(self) -> None:
        attempt, calls = self._scripted(
            make_result(self.tmp, stderr=CAPACITY_LINE, model="a"),
            make_result(self.tmp, ok=True, exit_code=0, model="b"),
        )
        result = fallback.run_with_fallback(attempt, ["a", "b"])
        self.assertTrue(result["ok"])
        self.assertEqual([name for name, _ in calls], ["a", "b"])
        self.assertEqual(result["fallback_used"], "b")
        self.assertIn("a", result["fallback_note"])
        self.assertIn("b", result["fallback_note"])

    def test_primary_success_has_no_fallback_note(self) -> None:
        attempt, calls = self._scripted(make_result(self.tmp, ok=True, exit_code=0, model="a"))
        result = fallback.run_with_fallback(attempt, ["a", "b"])
        self.assertTrue(result["ok"])
        self.assertEqual(len(calls), 1)
        self.assertNotIn("fallback_note", result)

    def test_ordinary_failure_stops_without_burning_other_models(self) -> None:
        attempt, calls = self._scripted(
            make_result(self.tmp, stderr="apply_patch verification failed"))
        result = fallback.run_with_fallback(attempt, ["a", "b"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 1, "不是容量问题就不该换模型重跑")

    def test_all_models_full_reports_the_whole_chain(self) -> None:
        attempt, calls = self._scripted(
            make_result(self.tmp, stderr=CAPACITY_LINE, model="a"),
            make_result(self.tmp, stderr=CAPACITY_LINE, model="b"),
        )
        result = fallback.run_with_fallback(attempt, ["a", "b"])
        self.assertFalse(result["ok"])
        self.assertEqual(len(calls), 2)
        self.assertIn("全部容量满载", result["fallback_note"])
        self.assertIn("b", result["fallback_note"])

    def test_unavailable_cli_fallback_is_skipped(self) -> None:
        """降级链里配了一个本机没装的 CLI：跳过它，别让整条链按死。"""
        attempt, calls = self._scripted(
            make_result(self.tmp, stderr=CAPACITY_LINE, model="a"),
            {"ok": False, "exit": 127, "timedout": False, "seconds": 0, "last": "",
             "stdout": "", "stderr": "", "summary": "没有这个 CLI", "model": "b", "tokens": None},
            make_result(self.tmp, ok=True, exit_code=0, model="c"),
        )
        result = fallback.run_with_fallback(attempt, ["a", "b", "c"])
        self.assertTrue(result["ok"])
        self.assertEqual([name for name, _ in calls], ["a", "b", "c"])
        self.assertEqual(result["fallback_used"], "c")
        self.assertIn("CLI 跑不了", result["fallback_note"])
        self.assertIn("容量满载", result["fallback_note"], "前一棒的容量满载也该如实写进去")


class BridgeFallbackTests(unittest.TestCase):
    """run_codex 是唯一入口：降级要在这一层完成，任务卡才拿得到 fallback_note。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_codex_retries_on_capacity_and_records_note(self) -> None:
        seen: list[tuple[str, int]] = []

        def fake_once(**kwargs) -> dict:
            seen.append((kwargs["model"], kwargs["attempt"]))
            if len(seen) == 1:
                return make_result(self.tmp, stderr=CAPACITY_LINE, model=kwargs["model"])
            return make_result(self.tmp, ok=True, exit_code=0, model=kwargs["model"])

        with mock.patch.object(codex_bridge, "_run_codex_once", side_effect=fake_once), \
                mock.patch.object(codex_bridge, "_route", return_value="codex"):
            result = codex_bridge.run_codex("prompt", workspace=str(self.tmp),
                                            model="primary", fallback_models=["backup"])

        self.assertTrue(result["ok"])
        self.assertEqual(seen, [("primary", 0), ("backup", 1)], "第二次尝试要带 attempt 序号")
        self.assertEqual(result["fallback_used"], "backup")
        self.assertIn("primary", result["fallback_note"])

    def test_fallback_model_is_routed_to_its_own_cli(self) -> None:
        """降级链里混了 claude 的型号：按它自己的 CLI 跑，不能硬塞给 codex。"""
        claude_calls: list[tuple[str, int]] = []

        def fake_claude(prompt: str, **kwargs) -> dict:
            claude_calls.append((kwargs["model"], kwargs["attempt"]))
            return make_result(self.tmp, ok=True, exit_code=0, model=kwargs["model"])

        with mock.patch.object(codex_bridge, "_run_codex_once",
                               return_value=make_result(self.tmp, stderr=CAPACITY_LINE)), \
                mock.patch.object(codex_bridge, "_route",
                                  side_effect=lambda m: "claude" if m == "claude-x" else "codex"), \
                mock.patch.object(claude_bridge, "_run_claude_once", side_effect=fake_claude):
            result = codex_bridge.run_codex("prompt", workspace=str(self.tmp),
                                            model="codex-y", fallback_models=["claude-x"])

        self.assertTrue(result["ok"])
        self.assertEqual(claude_calls, [("claude-x", 1)])
        self.assertEqual(result["fallback_used"], "claude-x")

    def test_claude_bridge_keeps_same_contract(self) -> None:
        with mock.patch.object(claude_bridge, "_run_claude_once",
                               return_value=make_result(self.tmp, stderr=CAPACITY_LINE)) as once:
            result = claude_bridge.run_claude("prompt", workspace=str(self.tmp),
                                              model="a", fallback_models=["b"])
        self.assertFalse(result["ok"])
        self.assertEqual(once.call_count, 2)
        self.assertIn("全部容量满载", result["fallback_note"])


class RealProcessFallbackTests(unittest.TestCase):
    """真的起一个 codex 子进程：满载 → 降级 → 拿到产物，整条链走一遍。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.ws = self.tmp / "ws"
        self.ws.mkdir()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.env = {
            "CODEX_EXE": str(make_fake_codex(self.bin)),
            "CODEX_HOME": str(self.tmp / "codex-home"),
            "FAKE_CAPACITY_MODEL": "primary-model",
        }

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_capacity_then_success_through_the_real_bridge(self) -> None:
        with mock.patch.dict(os.environ, self.env, clear=False), \
                mock.patch.object(codex_bridge, "_route", return_value="codex"):
            result = codex_bridge.run_codex(
                "画一只鹈鹕", workspace=str(self.ws), out_prefix=str(self.tmp / "run"),
                model="primary-model", fallback_models=["backup-model"])

        self.assertTrue(result["ok"], result.get("summary"))
        self.assertEqual(result["model"], "backup-model")
        self.assertIn("primary-model", result["fallback_note"])
        self.assertEqual(Path(result["last"]).read_text(encoding="utf-8").strip(), "done: 画一只鹈鹕")
        first_err = Path(str(self.tmp / "run") + ".err.txt").read_text(encoding="utf-8")
        self.assertIn("at capacity", first_err, "第一次调用的日志要留下证据")
        second_err = Path(str(self.tmp / "run") + "-attempt1.err.txt")
        self.assertTrue(second_err.is_file(), "第二次尝试要有自己的日志，别覆盖第一次")

    def test_all_models_full_is_reported_not_swallowed(self) -> None:
        env = dict(self.env, FAKE_CAPACITY_MODEL="*")
        with mock.patch.dict(os.environ, env, clear=False), \
                mock.patch.object(codex_bridge, "_route", return_value="codex"):
            result = codex_bridge.run_codex(
                "画一只鹈鹕", workspace=str(self.ws), out_prefix=str(self.tmp / "run2"),
                model="a-model", fallback_models=["b-model"])

        self.assertFalse(result["ok"])
        self.assertIn("全部容量满载", result["fallback_note"])
        self.assertIn("a-model", result["fallback_note"])
        self.assertIn("b-model", result["fallback_note"])


class ConfiguredFallbackTests(unittest.TestCase):
    """配置里的降级链：别名归一、去重、排掉主模型。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.config = self.tmp / "team.json"
        self._saved = models.CONFIG
        models.CONFIG = self.config

    def tearDown(self) -> None:
        models.CONFIG = self._saved
        self.tmpdir.cleanup()

    def _write(self, data: dict) -> None:
        self.config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def test_aliases_are_canonicalized(self) -> None:
        self._write({"model_registry": {"gpt-6-luna": {"cli": "codex", "aliases": ["luna"]}},
                     "executor": {"model": "gpt-6.1-terra", "fallback_models": ["luna"]}})
        self.assertEqual(models.fallback_models("执行"), ["gpt-6-luna"])

    def test_primary_model_is_excluded(self) -> None:
        self._write({"executor": {"model": "a-1", "fallback_models": ["a-1", "b-2"]}})
        self.assertEqual(models.fallback_models("executor", exclude="a-1"), ["b-2"])

    def test_missing_block_is_empty(self) -> None:
        self._write({"reviewer": {"model": "r-1"}})
        self.assertEqual(models.fallback_models("executor"), [])

    def test_broken_config_does_not_raise(self) -> None:
        self.config.write_text("{ not json", encoding="utf-8")
        self.assertEqual(models.fallback_models("executor"), [])


class DefaultVerifyTests(unittest.TestCase):
    """验收命令兜底：按产出挑命令，别再在空目录里默认 unittest。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmpdir.name)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def run_verify(self, command: str) -> int:
        """和 team.py 一样：引号感知地切成 argv，不经过 shell。"""
        argv = team.parse_verify_argv(command)
        return subprocess.run(argv, cwd=str(self.ws), capture_output=True).returncode

    def test_static_product_gets_an_existence_check(self) -> None:
        command = worker.default_verify(self.ws, ["pelican_bicycle.html"])
        self.assertNotEqual(command, worker.DEFAULT_VERIFY)
        self.assertTrue(command.startswith("python -c"))
        self.assertEqual(self.run_verify(command), 1, "文件还没生成时验收要失败")
        (self.ws / "pelican_bicycle.html").write_text("<svg></svg>", encoding="utf-8")
        self.assertEqual(self.run_verify(command), 0, "产物生成了验收就要通过")

    def test_repo_with_tests_keeps_unittest(self) -> None:
        (self.ws / "tests").mkdir()
        self.assertEqual(worker.default_verify(self.ws, ["tools/cli.py"]), worker.DEFAULT_VERIFY)

    def test_python_file_without_tests_gets_py_compile(self) -> None:
        (self.ws / "count_lines.py").write_text("print('ok')\n", encoding="utf-8")
        command = worker.default_verify(self.ws, ["count_lines.py"])
        self.assertTrue(command.startswith("python -m py_compile"))
        self.assertEqual(self.run_verify(command), 0)

    def test_unknown_shape_falls_back_to_unittest(self) -> None:
        self.assertEqual(worker.default_verify(self.ws, ["tools/"]), worker.DEFAULT_VERIFY)

    def test_sanitize_plan_does_not_turn_an_html_task_into_unittest(self) -> None:
        """白名单外的命令被丢掉之后，兜底也得认产出（就是这次踩的坑）。"""
        card = worker.sanitize_plan({
            "title": "鹈鹕骑车", "goal": "静态页", "acceptance": ["文件存在"],
            "verify": ["start pelican_bicycle.html"], "scope": ["pelican_bicycle.html"],
        }, self.ws)
        self.assertNotIn(worker.DEFAULT_VERIFY, card["verify"])
        self.assertTrue(card["verify"][0].startswith("python -c"))


if __name__ == "__main__":
    unittest.main()