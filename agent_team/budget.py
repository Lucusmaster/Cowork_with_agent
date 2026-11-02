"""单卡预算：一张卡最多烧多少 token、多少时间。

没有这道闸门时，一次跑偏的任务（返工两轮 + 复核两次）能烧掉几十万 token 而不报警。
这里按卡累计 `rounds[].tokens` 与 `rounds[].seconds`，超过 config/team.json 的
`budget` 上限就停手，把账摆在用户面前让他决定：换便宜模型、拆小任务，还是调高上限。

默认值故意宽松（80 万 token / 2 小时）：正常一张卡几万到十几万 token，
调得太紧会把正常的返工也拦下来。
"""

from __future__ import annotations

import json
from typing import Any

from . import models

DEFAULTS: dict[str, int] = {
    "max_tokens": 800_000,
    "max_seconds": 7_200,
}


def limits() -> dict[str, int]:
    """读 config/team.json 的 `budget`，缺项用默认值。"""
    out = dict(DEFAULTS)
    try:
        cfg = models.config()
    except Exception:  # noqa: BLE001 - 读不到配置就用默认，别让护栏本身崩掉
        return out
    table = cfg.get("budget") if isinstance(cfg, dict) else None
    if isinstance(table, dict):
        for key in out:
            try:
                value = int(table.get(key) or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                out[key] = value
    return out


def usage(task: dict[str, Any]) -> dict[str, int]:
    """一张卡到目前为止用掉多少：token 数、执行秒数、轮次。"""
    tokens = 0
    seconds = 0
    rounds = task.get("rounds") or []
    for item in rounds:
        try:
            tokens += int(item.get("tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            seconds += int(item.get("seconds") or 0)
        except (TypeError, ValueError):
            pass
    return {"tokens": tokens, "seconds": seconds, "rounds": len(rounds)}


def check(task: dict[str, Any]) -> str:
    """超预算就返回一句人话，没超返回空串。"""
    caps = limits()
    used = usage(task)
    if used["tokens"] >= caps["max_tokens"]:
        return (f"这张卡已经用掉 {used['tokens']:,} tokens（上限 {caps['max_tokens']:,}），"
                f"先停下：换便宜模型、把任务拆小，或者调高 config/team.json 的 budget.max_tokens")
    if used["seconds"] >= caps["max_seconds"]:
        return (f"这张卡已经跑掉 {used['seconds'] // 60} 分钟（上限 {caps['max_seconds'] // 60} 分钟），"
                f"先停下：把任务拆小，或者调高 config/team.json 的 budget.max_seconds")
    return ""


def note(task: dict[str, Any]) -> str:
    """给报告/消息用的一行账：用了多少、还剩多少。"""
    caps = limits()
    used = usage(task)
    return (f"已用 {used['tokens']:,} tokens / {used['seconds'] // 60} 分钟"
            f"（上限 {caps['max_tokens']:,} tokens / {caps['max_seconds'] // 60} 分钟）")


def report_line(task_dir) -> str:
    """从卡文件算一行账，读不到就返回空串。"""
    import pathlib

    try:
        task = json.loads((pathlib.Path(task_dir) / "task.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return note(task)