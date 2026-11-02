"""模型容量满载（at capacity）时的降级重试。

Codex / Claude 的 CLI 在模型额度打满时会直接以非零退出，结论里只有一句
`ERROR: Selected model is at capacity. Please try a different model.`，
`--output-last-message` 是空的——T-261102-012458 就是这样连挂两轮：
执行者烧掉 9.7 万 tokens 什么都没交出来，协作台却把它当成"执行失败"，
下一轮拿着同一张卡、同一个打满的模型再撞一次。

这里只做三件小事，别的一概不管：

- `is_capacity_error()`：只认"容量/限流"这一类结论，普通失败照旧直接收手。
  换模型重跑一个真的跑挂的任务，只会多烧一次额度、还可能把工作区改乱。
- `attempt_models()`：把主模型和降级链排成一行，去重、去空、跳过主模型自身。
- `run_with_fallback()`：按顺序试，碰到容量满载才往后走；换成哪个模型跑成功，
  在结果里留一条 `fallback_note`，让任务卡和群里都看得见。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Sequence

# 各家 CLI 的措辞不一样，认这几种就够；别写太宽（"error" 之类）把普通失败也吃进来。
CAPACITY_MARKERS = (
    "at capacity",
    "capacity exceeded",
    "capacity limit",
    "overloaded",
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota exceeded",
    "insufficient quota",
)

_TAIL_CHARS = 8000


def _tail_of(path_or_text: str) -> str:
    """结果里的 stdout/stderr 字段是**文件路径**；读不到就当它本来就是正文。"""
    raw = str(path_or_text or "").strip()
    if not raw:
        return ""
    path = Path(raw)
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")[-_TAIL_CHARS:]
    except (OSError, ValueError):
        return ""
    return raw[-_TAIL_CHARS:]


def is_capacity_error(result: dict) -> bool:
    """这次调用是不是"模型容量满载"？只看结论，不看退出码。

    退出码对这类错误没有区分度：容量满载、提示词写错、任务真的失败都是 1。
    """
    chunks = [
        _tail_of(result.get("stderr") or ""),
        _tail_of(result.get("stdout") or ""),
        _tail_of(result.get("last") or ""),
        str(result.get("summary") or ""),
        str(result.get("error") or ""),
    ]
    text = "\n".join(chunks).lower()
    return any(marker in text for marker in CAPACITY_MARKERS)


def attempt_models(primary: str, fallbacks: Sequence[str] | None = None) -> list[str]:
    """主模型在前、降级链在后；去重（忽略大小写）、去空。"""
    first = str(primary or "").strip()
    chain = [first]
    seen = {first.lower()}
    for item in fallbacks or ():
        name = str(item or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        chain.append(name)
    return chain


def run_with_fallback(attempt: Callable[[str, int], dict], models: Sequence[str],
                      label: str = "") -> dict:
    """按 `models` 顺序试，直到成功或碰到"不是容量问题"的失败。

    `attempt(model, index)` 返回和 `run_codex`/`run_claude` 同形的结果字典。
    成功时如果换过模型，结果里多带 `fallback_used` 和 `fallback_note`；
    全部模型都满载就返回最后一次的结果，`fallback_note` 写清试过哪些。
    """
    chain = list(models) or [""]
    bridge = f"[{label}] " if label else "[bridge] "
    last: dict | None = None
    reasons: list[str] = []          # 往前换的理由，最后要如实写进 fallback_note

    def shown(name: str) -> str:
        return name or "默认模型"

    for index, name in enumerate(chain):
        result = attempt(name, index)
        last = result

        if result.get("ok"):
            if index > 0:
                result["fallback_used"] = name
                if len(reasons) == 1 and "容量满载" in reasons[0]:
                    result["fallback_note"] = f"原定 {chain[0] or '默认模型'} 容量满载，降级为 {shown(name)}"
                else:
                    result["fallback_note"] = f"换过模型：{'；'.join(reasons)}，最终用 {shown(name)}"
            return result

        if not is_capacity_error(result):
            # CLI/适配根本不存在（exit 127）：换个模型还能试，别让一条配错的降级链
            # 把整个任务按死；其它普通失败立刻收手，不拿别的模型去重跑真问题。
            if result.get("exit") == 127 and index + 1 < len(chain):
                reasons.append(f"{shown(name)} 这条 CLI 跑不了")
                print(f"{bridge}{shown(name)} 这条 CLI 跑不了，跳过它试 {shown(chain[index + 1])}")
                continue
            if index > 0:
                result["fallback_note"] = f"降级到 {shown(name)} 仍然失败（不是容量问题），已停下"
            return result

        reasons.append(f"{shown(name)} 容量满载")
        if index + 1 < len(chain):
            print(f"{bridge}{shown(name)} 容量满载，尝试降级到 {shown(chain[index + 1])}")

    if last is not None:
        tried = " → ".join(shown(name) for name in chain)
        if all("容量满载" in reason for reason in reasons):
            last["fallback_note"] = f"已尝试 {len(chain)} 个模型，全部容量满载（{tried}）"
        else:
            last["fallback_note"] = f"已尝试 {len(chain)} 个模型都没跑成（{'；'.join(reasons)}；{tried}）"
        return last
    return {"ok": False, "exit": 1, "timedout": False, "seconds": 0, "last": "", "stdout": "",
            "stderr": "", "summary": "没有可用的模型", "model": "", "tokens": None}


def capacity_note(result: dict) -> str:
    """给任务卡/群里用的一句话；没有降级就不写。"""
    return str(result.get("fallback_note") or "").strip()


__all__ = [
    "CAPACITY_MARKERS", "attempt_models", "capacity_note",
    "is_capacity_error", "run_with_fallback",
]