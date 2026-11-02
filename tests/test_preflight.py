"""下单前置校验（preflight）的行为测试。

钉住的是**判断**，不是实现：哪种情况该拦、哪种只该提醒、修复建议的先后顺序。
`cli_ready` 依赖本机装没装 CLI，这里全部打桩（和 models 的测试同一个路子），
否则这套测试的结果会取决于跑测试的机器装了什么。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from agent_team import models, preflight  # noqa: E402
from agent_team import workspace as wsmod  # noqa: E402


class PreflightCase(unittest.TestCase):
    """一份隔离的配置 + 隔离的账本根；CLI 可用性用 `self.dead` 控制。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.ledger = self.tmp / "ledger"
        (self.ledger / "room").mkdir(parents=True)
        self.config_path = self.ledger / "config" / "team.json"
        self.config_path.parent.mkdir()
        self._saved = (models.CONFIG, models.OVERRIDES, preflight.ROOT, wsmod.ROOT)
        models.CONFIG = self.config_path
        models.OVERRIDES = self.ledger / "room" / "model_overrides.json"
        preflight.ROOT = self.ledger
        wsmod.ROOT = self.ledger          # is_inside_ledger 用的是这个模块常量
        self.dead: set[str] = set()          # 打桩：这些型号的 CLI"没装"
        self.write_config()
        patcher = mock.patch.object(models, "cli_ready", side_effect=self._cli_ready)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        models.CONFIG, models.OVERRIDES, preflight.ROOT, wsmod.ROOT = self._saved
        self._tmp.cleanup()

    def _cli_ready(self, model: str) -> tuple[bool, str]:
        if str(model) in self.dead:
            return False, f"{model}：找不到对应的 CLI"
        return True, ""

    def write_config(self, model: str = "gpt-6.1-terra",
                     fallbacks: tuple[str, ...] = ("gpt-6.1-sol", "gpt-6-luna"),
                     planner: str = "gpt-6.1-terra") -> None:
        config = {
            "model_registry": {
                "gpt-6.1-terra": {"cli": "codex", "aliases": ["terra"]},
                "gpt-6.1-sol": {"cli": "codex", "aliases": ["sol"]},
                "gpt-6-luna": {"cli": "codex", "aliases": ["luna"]},
            },
            "executor": {"model": model, "fallback_models": list(fallbacks)},
            "reviewer": {"model": "gpt-6-luna"},
            "agents": [
                {"role": "executor", "model": model},
                {"role": "planner", "model": planner},
                {"role": "reviewer", "model": "gpt-6-luna"},
            ],
        }
        self.config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")


class ModelCheckTests(PreflightCase):
    def test_unknown_model_is_blocking(self) -> None:
        self.write_config(model="ghost-model")
        real_resolve = models.resolve
        with mock.patch.object(models, "resolve",
                               side_effect=lambda name: None if name == "ghost-model" else real_resolve(name)):
            rows = preflight.check_models()
        bad = [row for row in rows if row["level"] == "bad"]
        self.assertTrue(any("unknown" in row["key"] for row in bad),
                        f"注册表里没有的型号必须拦住：{rows}")

    def test_cli_missing_on_primary_model_is_blocking(self) -> None:
        self.dead = {"gpt-6.1-terra"}
        rows = preflight.check_models()
        bad = [row for row in rows if row["level"] == "bad"]
        self.assertTrue(any(row["key"] == "model.executor.cli" for row in bad), rows)
        self.assertIn("找不到对应的 CLI", bad[0]["detail"])

    def test_bad_fallback_entry_is_only_a_warning(self) -> None:
        self.dead = {"gpt-6.1-sol"}
        rows = preflight.check_models()
        related = [row for row in rows if "fallback.gpt-6.1-sol" in row["key"]]
        self.assertTrue(related, f"降级链里的坏型号要有一条记录：{rows}")
        self.assertEqual(related[0]["level"], "warn")
        self.assertTrue(preflight.run(include_remote=False)["ok"],
                        "主模型还能用，降级链坏一条不该拦住下单")

    def test_planner_without_fallback_is_not_a_problem(self) -> None:
        rows = preflight.check_models()
        planner = [row for row in rows if row["key"].startswith("model.planner") or "规划降级链" in row["name"]]
        self.assertEqual([row for row in planner if row["level"] != "ok"], [],
                         f"planner 没配降级链是正常的，不该报警告：{planner}")

    def test_fallback_health_empty_chain_is_warn(self) -> None:
        self.write_config(fallbacks=())
        rows = preflight.check_fallback_health()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "fallback.executor.empty")
        self.assertEqual(rows[0]["level"], "warn")
        self.assertIn("config/team.json", rows[0]["fix"])

    def test_fallback_health_all_dead_is_bad(self) -> None:
        self.dead = {"gpt-6.1-sol", "gpt-6.1-terra"}
        self.write_config(fallbacks=("gpt-6.1-sol", "gpt-6.1-terra"))
        rows = preflight.check_fallback_health()
        self.assertEqual(rows[0]["key"], "fallback.executor.all_dead")
        self.assertEqual(rows[0]["level"], "bad")

    def test_fallback_health_reports_usable_ones(self) -> None:
        self.write_config(fallbacks=("gpt-6.1-sol", "gpt-6-luna"))
        self.dead = {"gpt-6-luna"}
        rows = preflight.check_fallback_health()
        self.assertEqual(rows[0]["level"], "ok")
        self.assertIn("gpt-6.1-sol", rows[0]["detail"])
        self.assertIn("gpt-6-luna", rows[0]["detail"])       # 不可用的也点名，便于修


class WorkspaceCheckTests(PreflightCase):
    def test_unbound_workspace_is_blocking(self) -> None:
        rows = preflight.check_workspace("")
        self.assertEqual(rows[0]["key"], "workspace.unbound")
        self.assertEqual(rows[0]["level"], "bad")

    def test_missing_directory_is_blocking(self) -> None:
        rows = preflight.check_workspace(str(self.tmp / "nope"))
        self.assertEqual(rows[0]["key"], "workspace.invalid")
        self.assertIn("不存在", rows[0]["detail"])

    def test_relative_path_is_rejected_as_invalid_not_in_ledger(self) -> None:
        rows = preflight.check_workspace("some-project")
        self.assertEqual(rows[0]["key"], "workspace.invalid")
        self.assertIn("绝对路径", rows[0]["detail"])

    def test_workspace_inside_ledger_is_blocking(self) -> None:
        inside = self.ledger / "whatever"
        inside.mkdir()
        rows = preflight.check_workspace(str(inside))
        self.assertEqual(rows[0]["key"], "workspace.in_ledger")
        self.assertIn("台账", rows[0]["detail"])

    def test_writable_workspace_passes_and_leaves_no_probe(self) -> None:
        project = self.tmp / "proj"
        project.mkdir()
        rows = preflight.check_workspace(str(project))
        self.assertEqual(rows[0]["level"], "ok")
        self.assertFalse((project / ".write-probe").exists(), "探针文件必须删掉")
        self.assertEqual(rows[0]["detail"], str(project.resolve()))


class EngineCheckTests(PreflightCase):
    def test_offline_engine_is_blocking(self) -> None:
        rows = preflight.check_engine(None, self.ledger)
        self.assertEqual(rows[0]["key"], "engine.offline")
        self.assertIn("team_launch", rows[0]["fix"])

    def test_dead_engine_is_blocking_even_if_busy_is_stale(self) -> None:
        rows = preflight.check_engine(
            {"engine": {"alive": False}, "root": str(self.ledger), "typing": {"agent": "planner"}},
            self.ledger)
        self.assertEqual(rows[0]["key"], "engine.dead")

    def test_engine_from_another_repo_is_blocking(self) -> None:
        rows = preflight.check_engine(
            {"engine": {"alive": True}, "root": str(self.tmp / "other-repo")}, self.ledger)
        self.assertEqual(rows[0]["key"], "engine.other_repo")
        self.assertIn("other-repo", rows[0]["detail"])

    def test_engine_of_this_repo_passes(self) -> None:
        rows = preflight.check_engine(
            {"engine": {"alive": True}, "root": str(self.ledger)}, self.ledger)
        self.assertEqual(rows[0]["level"], "ok")


class RunTests(PreflightCase):
    def test_blocking_order_is_engine_workspace_models(self) -> None:
        self.write_config(model="ghost-model")
        real_resolve = models.resolve
        with mock.patch.object(models, "resolve",
                               side_effect=lambda name: None if name == "ghost-model" else real_resolve(name)):
            gate = preflight.run(workspace="", state=None)
        keys = [row["key"] for row in gate["blocking"]]
        self.assertFalse(gate["ok"])
        self.assertEqual(keys[0], "engine.offline")
        self.assertEqual(keys[1], "workspace.unbound")
        self.assertTrue(keys[2].startswith("model.executor"), keys)

    def test_local_only_skips_engine_and_workspace(self) -> None:
        gate = preflight.run(workspace="", state=None, include_remote=False)
        keys = [row["key"] for row in gate["items"]]
        self.assertFalse([key for key in keys if key.startswith(("engine.", "workspace."))])
        self.assertTrue(gate["ok"], "本地检查只看模型，工作区/引擎不该出现在结果里")


if __name__ == "__main__":
    unittest.main()