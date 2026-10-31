"""打包一致性守卫：仓库里的 config/agents 与 wheel 内置副本不能漂移。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class BundledResourceTests(unittest.TestCase):
    """`pip install` 后走的是 agent_team/bundled，两份必须一致，否则行为会变。"""

    def test_team_config_matches_bundled_copy(self) -> None:
        repo = ROOT / "config" / "team.json"
        bundled = ROOT / "agent_team" / "bundled" / "config" / "team.json"
        self.assertTrue(bundled.is_file(), "缺少 agent_team/bundled/config/team.json")
        self.assertEqual(json.loads(repo.read_text(encoding="utf-8")),
                         json.loads(bundled.read_text(encoding="utf-8")),
                         "config/team.json 改了但没同步到 agent_team/bundled/，"
                         "wheel 安装后会读到旧配置")

    def test_bundled_agents_match_repo_agents(self) -> None:
        for name in ("brain.md", "executor.md", "reviewer.md"):
            repo = ROOT / "agents" / name
            bundled = ROOT / "agent_team" / "bundled" / "agents" / name
            self.assertTrue(bundled.is_file(), f"缺少 agent_team/bundled/agents/{name}")
            self.assertEqual(repo.read_text(encoding="utf-8"),
                             bundled.read_text(encoding="utf-8"),
                             f"agents/{name} 改了但没同步到 agent_team/bundled/")

    def test_bundled_config_declares_all_roles(self) -> None:
        data = json.loads((ROOT / "agent_team" / "bundled" / "config" / "team.json")
                          .read_text(encoding="utf-8"))
        roles = {item.get("role") for item in (data.get("agents") or [])}
        self.assertTrue({"coordinator", "planner", "executor", "tester", "reviewer"} <= roles,
                        f"bundled 配置缺少角色：{roles}")


if __name__ == "__main__":
    unittest.main()