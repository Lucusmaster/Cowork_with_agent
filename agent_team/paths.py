"""决定「账本放哪」：源码目录、安装后的数据目录，以及用户自己的工作区。

三种情况：
- 仓库里直接跑（有 pyproject.toml）→ 用仓库根，`tasks/` `room/` 就在仓库里；
- pip 装过之后跑 → 用 `AGENT_TEAM_HOME` 指的数据目录（默认 `~/.agent-team`），
  绝不去写 `site-packages`；
- 显式设了 `AGENT_TEAM_HOME` → 永远听它的，方便放到别的盘或做多实例隔离。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_HOME = "AGENT_TEAM_HOME"


def _looks_like_checkout(path: Path) -> bool:
    return (path / "pyproject.toml").is_file() and (path / "agent_team").is_dir()


def workbench_root(pkg_file: Path | None = None, home: Path | None = None) -> Path:
    """返回协作台的账本根目录（tasks/ room/ 的父目录）。"""
    env = os.environ.get(ENV_HOME, "").strip()
    if env:
        return Path(env).expanduser().resolve()
    pkg_dir = Path(pkg_file or __file__).resolve().parent
    checkout = pkg_dir.parent
    if _looks_like_checkout(checkout):
        return checkout
    data = (Path(home) if home is not None else Path.home()) / ".agent-team"
    return data.expanduser().resolve()


def ensure_root(root: Path | None = None) -> Path:
    """拿到根目录并确保 tasks/ room/ 存在（写账本前调用）。"""
    base = Path(root) if root else workbench_root()
    for name in ("tasks", "room"):
        (base / name).mkdir(parents=True, exist_ok=True)
    return base