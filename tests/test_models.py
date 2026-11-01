"""角色↔模型绑定：优先级、白名单、运行时覆盖、命令解析。

这一层的价值全在"别让人悄悄跑在错的模型上"：
- 名字拼错 → 切换时当场拒绝（不是烧完一轮才发现模型不存在）；
- 优先级 → 卡上指定 > 运行时切换 > 配置 > 默认，谁也不能越级；
- 协调者不可切 → 它是引擎自己的大脑，如实拒绝而不是假装切了。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_team import agent_clis, models


class ModelsCase(unittest.TestCase):
    """把 CONFIG / OVERRIDES 指到临时目录，别动真实账本。"""

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self.tmpdir.name)
        self.config = self.tmp / "team.json"
        self.overrides = self.tmp / "room" / "model_overrides.json"
        self._saved = {"CONFIG": models.CONFIG, "OVERRIDES": models.OVERRIDES}
        models.CONFIG = self.config
        models.OVERRIDES = self.overrides
        # 本机装没装 claude、登记过什么，都不能影响结果：只留 shutil.which 这一个入口给 only_codex 打桩
        self._isolate = [mock.patch.object(agent_clis, "STORE", self.tmp / "room" / "agent_clis.json"),
                         mock.patch.object(agent_clis, "_common_candidates", return_value=[]),
                         mock.patch.dict(agent_clis.os.environ, {}, clear=False)]
        for p in self._isolate:
            p.start()
        for key in ("CODEX_EXE", "CLAUDE_EXE", "DEEPSEEK_EXE"):
            agent_clis.os.environ.pop(key, None)
        agent_clis._PROBES.clear()
        self.write_config({
            "model_registry": {
                "gpt-6.1-terra": {"cli": "codex", "aliases": ["terra"]},
                "gpt-6-luna": {"cli": "codex", "aliases": ["luna"]},
                "claude-opus-5-5": {"cli": "claude", "aliases": ["claude", "opus"]},
            },
            "agents": [
                {"role": "coordinator", "name": "协调", "model": "gpt-6.1-terra"},
                {"role": "planner", "name": "规划", "model": "gpt-6.1-terra"},
                {"role": "executor", "name": "执行", "model": "gpt-6.1-terra"},
                {"role": "tester", "name": "验收", "model": "gpt-6.1-terra"},
                {"role": "reviewer", "name": "复核", "model": "gpt-6-luna"},
            ],
            "executor": {"model": "gpt-6.1-terra", "reasoning_effort": "low"},
            "reviewer": {"model": "gpt-6-luna", "reasoning_effort": "low"},
        })

    def tearDown(self) -> None:
        for p in reversed(self._isolate):
            p.stop()
        models.CONFIG = self._saved["CONFIG"]
        models.OVERRIDES = self._saved["OVERRIDES"]
        self.tmpdir.cleanup()

    def write_config(self, data: dict) -> None:
        self.config.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def only_codex(self):
        """假装本机只装了 codex，这样"没装 claude"的分支可测。"""
        return mock.patch.object(models.shutil, "which",
                                 side_effect=lambda name: "/usr/bin/codex" if name == "codex" else None)


class RegistryTests(ModelsCase):
    def test_registry_reads_config(self) -> None:
        self.assertIn("gpt-6.1-terra", models.registry())

    def test_unregistered_model_is_auto_collected(self) -> None:
        """老配置里出现过但没注册的型号自动收录，老配置零改动也能用。"""
        self.write_config({"executor": {"model": "some-new-model"}})
        self.assertIn("some-new-model", models.registry())
        self.assertEqual(models.registry()["some-new-model"]["note"], "自动收录")

    def test_fallback_models_are_registered(self) -> None:
        self.write_config({"reviewer": {"model": "a-1", "fallback_models": ["b-2"]}})
        self.assertIn("b-2", models.registry())

    def test_broken_config_does_not_raise(self) -> None:
        self.config.write_text("{ not json", encoding="utf-8")
        self.assertEqual(models.registry(), {})

    def test_alias_and_case_resolution(self) -> None:
        self.assertEqual(models.resolve("terra"), "gpt-6.1-terra")
        self.assertEqual(models.resolve("LUNA"), "gpt-6-luna")
        self.assertEqual(models.resolve("opus"), "claude-opus-5-5")

    def test_unknown_name_returns_none(self) -> None:
        self.assertIsNone(models.resolve("gemini-9"))

    def test_ambiguous_prefix_is_not_guessed(self) -> None:
        self.write_config({"model_registry": {"gpt-6.1-terra": {}, "gpt-6.1-sol": {}}})
        self.assertIsNone(models.resolve("gpt-6.1"), "两个候选时不许瞎猜")


class RoleResolutionTests(ModelsCase):
    def test_role_aliases(self) -> None:
        for alias, role in [("执行", "executor"), ("Executor", "executor"), ("复核", "reviewer"),
                            ("验收", "tester"), ("规划", "planner"), ("协调", "coordinator")]:
            self.assertEqual(models.resolve_role(alias), role, alias)

    def test_config_defaults(self) -> None:
        self.assertEqual(models.model_for("executor"), ("gpt-6.1-terra", "配置"))
        self.assertEqual(models.model_for("reviewer"), ("gpt-6-luna", "配置"))

    def test_runtime_override_beats_config(self) -> None:
        with self.only_codex():
            ok, _ = models.set_model("executor", "luna")
        self.assertTrue(ok)
        self.assertEqual(models.model_for("executor"), ("gpt-6-luna", "临时"))

    def test_task_card_beats_override(self) -> None:
        with self.only_codex():
            models.set_model("executor", "luna")
        task = {"models": {"executor": "gpt-6.1-terra"}}
        self.assertEqual(models.model_for("executor", task), ("gpt-6.1-terra", "卡"))

    def test_legacy_card_block_still_wins(self) -> None:
        """老卡片的 task["executor"]["model"] 写法必须继续生效。"""
        task = {"executor": {"model": "gpt-6.1-terra"}}
        self.assertEqual(models.model_for("executor", task), ("gpt-6.1-terra", "卡"))

    def test_default_when_nothing_configured(self) -> None:
        self.write_config({})
        self.assertEqual(models.model_for("executor"), ("", "默认"))

    def test_effort_from_agents_then_role_block(self) -> None:
        self.write_config({"agents": [{"role": "executor", "effort": "high"}],
                           "executor": {"reasoning_effort": "low"}})
        self.assertEqual(models.effort_for("executor"), "high")
        self.write_config({"executor": {"reasoning_effort": "medium"}})
        self.assertEqual(models.effort_for("executor"), "medium")


class SwitchTests(ModelsCase):
    def test_switch_persists_to_disk(self) -> None:
        with self.only_codex():
            ok, message = models.set_model("执行者", "luna")
        self.assertTrue(ok, message)
        self.assertEqual(json.loads(self.overrides.read_text(encoding="utf-8")),
                         {"executor": "gpt-6-luna"})

    def test_switch_rejects_unknown_model_and_says_what_is_available(self) -> None:
        ok, message = models.set_model("executor", "gemini-9")
        self.assertFalse(ok)
        self.assertIn("不认识", message)
        self.assertIn("gpt-6.1-terra", message, "报错要顺手列出可用型号")
        self.assertFalse(self.overrides.exists(), "失败不能留下覆盖文件")

    def test_switch_rejects_missing_cli(self) -> None:
        """本机没装 claude 却允许切过去 = 下一张卡必然执行失败，所以当场拒绝。"""
        with self.only_codex():
            ok, message = models.set_model("executor", "claude")
        self.assertFalse(ok)
        self.assertIn("claude", message)

    def test_reset_clears_override(self) -> None:
        with self.only_codex():
            models.set_model("executor", "luna")
            ok, _ = models.set_model("executor", "reset")
        self.assertTrue(ok)
        self.assertEqual(models.overrides(), {})
        self.assertEqual(models.model_for("executor"), ("gpt-6.1-terra", "配置"))

    def test_coordinator_is_not_switchable(self) -> None:
        ok, message = models.set_model("coordinator", "luna")
        self.assertFalse(ok)
        self.assertIn("大脑模型", message)

    def test_unknown_role_is_rejected(self) -> None:
        ok, _ = models.set_model("司机", "luna")
        self.assertFalse(ok)

    def test_atomic_write_leaves_no_temp_files(self) -> None:
        with self.only_codex():
            models.set_model("executor", "luna")
            models.set_model("reviewer", "terra")
        leftovers = list(self.overrides.parent.glob(".model-*"))
        self.assertEqual(leftovers, [], f"临时文件没清干净：{leftovers}")

    def test_broken_override_file_is_ignored(self) -> None:
        self.overrides.parent.mkdir(parents=True, exist_ok=True)
        self.overrides.write_text("{ broken", encoding="utf-8")
        self.assertEqual(models.overrides(), {})
        self.assertEqual(models.model_for("executor"), ("gpt-6.1-terra", "配置"))


class CommandParseTests(unittest.TestCase):
    def test_slash_forms(self) -> None:
        self.assertEqual(models.parse_command("/model 执行 luna"), ("set", "executor", "luna"))
        self.assertEqual(models.parse_command("/model executor=terra"), ("set", "executor", "terra"))
        self.assertEqual(models.parse_command("/model 复核 -> luna"), ("set", "reviewer", "luna"))
        self.assertEqual(models.parse_command("/model executor reset"), ("reset", "executor", ""))
        self.assertEqual(models.parse_command("/model list"), ("list", "", ""))
        # 光说"默认"不知道是哪个角色，宁可当没听懂
        self.assertIsNone(models.parse_command("/model 默认"))

    def test_chinese_form(self) -> None:
        self.assertEqual(models.parse_command("切换模型 执行=claude"), ("set", "executor", "claude"))
        self.assertEqual(models.parse_command("换模型：复核 terra"), ("set", "reviewer", "terra"))
        self.assertEqual(models.parse_command("切换模型"), ("list", "", ""))

    def test_natural_language_is_not_hijacked(self) -> None:
        """最重要的负例：普通需求不能被当成切模型。"""
        for text in ["用 python 换实现方式", "帮我做个计算器", "把执行者换成 claude 的思路写进文档",
                     "model 层要不要拆开", "这个模型太慢了"]:
            self.assertIsNone(models.parse_command(text), f"误判成命令了：{text}")

    def test_unknown_role_in_command_is_not_a_command(self) -> None:
        self.assertIsNone(models.parse_command("/model 司机 luna"))


class SnapshotTests(ModelsCase):
    def test_snapshot_has_all_roles_and_sources(self) -> None:
        snap = models.snapshot()
        roles = {row["role"]: row for row in snap["roles"]}
        self.assertEqual(set(roles), set(models.ROLES))
        self.assertEqual(roles["executor"]["model"], "gpt-6.1-terra")
        self.assertEqual(roles["executor"]["source"], "配置")
        self.assertFalse(roles["coordinator"]["switchable"])

    def test_snapshot_marks_unready_registry_entries(self) -> None:
        with self.only_codex():
            snap = models.snapshot()
        ready = {item["id"]: item["ready"] for item in snap["registry"]}
        self.assertTrue(ready["gpt-6-luna"])
        self.assertFalse(ready["claude-opus-5-5"], "没装 claude 就该标成不可用")

    def test_snapshot_never_raises_on_broken_config(self) -> None:
        self.config.write_text("\x00\x01", encoding="utf-8")
        snap = models.snapshot()
        self.assertIn("roles", snap)
        self.assertEqual(snap["registry"], [])


class DispatchWiringTests(unittest.TestCase):
    """派发链必须真的读运行时覆盖——否则"切换"只是改了个装饰品。"""

    def test_dispatch_and_review_resolve_through_models(self) -> None:
        import inspect

        from agent_team import team

        for func, role in ((team.cmd_dispatch, "executor"), (team.cmd_review, "reviewer")):
            src = inspect.getsource(func)
            self.assertIn(f'models.model_for("{role}"', src,
                          f"{func.__name__} 没走运行时解析，切了模型也不会生效")
            self.assertIn("models.effort_for", src)

    def test_review_still_forced_read_only_after_model_switch(self) -> None:
        """换模型不能顺带把只读沙箱换掉（0.9.13 的硬约束不能被绕过）。"""
        import inspect

        from agent_team import team

        src = inspect.getsource(team.cmd_review)
        self.assertIn("_review_sandbox(", src)


if __name__ == "__main__":
    unittest.main()