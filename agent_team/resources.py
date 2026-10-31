"""资源定位：优先用仓库里的 config/ 与 agents/，装成 wheel 时回落到包内副本。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUNDLED = Path(__file__).resolve().parent / "bundled"


def resource(*parts: str) -> Path:
    """仓库优先，包内兜底。"""
    repo = ROOT.joinpath(*parts)
    if repo.exists():
        return repo
    return BUNDLED.joinpath(*parts)
