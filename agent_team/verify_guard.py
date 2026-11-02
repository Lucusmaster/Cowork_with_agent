"""验收命令的体检：分清"代码没写对"和"验收脚本自己坏了"。

背景：验收命令是大脑闭着眼睛写的（它读不到文件、也不会执行），所以经常出现
"命令本身就是坏的"这种情况——正则假设了文件里没有的符号、变量名写错、
单位搞混（度 vs 弧度）、`assert` 里引用了不存在的常量。这类失败跟执行者
没关系：执行者怎么改都过不了，派返工只会白烧一轮 token，还会把本来正确的
文件改坏。

判据：命令是"崩了"还是"断言没过"。崩了（Traceback + SyntaxError/AttributeError/
KeyError/…，且不是 AssertionError、也不是"文件还不存在"）说明脚本自身有问题，
应该退回让大脑重写命令；断言没过说明代码确实不达标，才轮到执行者返工。

`FileNotFoundError` 单独排除：建卡/派发时产物往往还不存在，那是"还没实现"，
不是脚本坏了。
"""

from __future__ import annotations

import time
from pathlib import Path

from .winproc import run_capped

# 脚本自身崩掉的特征（顺序无所谓，只要命中一个）
CRASH_MARKERS = (
    "SyntaxError",
    "IndentationError",
    "AttributeError",
    "NameError",
    "UnboundLocalError",
    "KeyError",
    "IndexError",
    "TypeError",
    "ValueError: invalid literal",
    "JSONDecodeError",
    "UnicodeDecodeError",
    "ZeroDivisionError",
    "re.error",
    "unterminated",
    "never closed",
    "invalid escape sequence",  # 只当噪声，真正判崩要看 Traceback
    "positional argument follows keyword argument",
)

# 这些说明"东西还没做出来"，属于正常的未达标，不是脚本自身坏了
NOT_SCRIPT_FAULT = (
    "AssertionError",
    "FileNotFoundError",
    "No such file or directory",
    "cannot find the path specified",
    "No module named",
)

TRACEBACK = "Traceback (most recent call last)"


def crash_reason(output: str) -> str:
    """验收命令是不是自己崩了？是就返回命中特征，否则返回空串。"""
    text = str(output or "")
    if TRACEBACK not in text:
        return ""
    for marker in NOT_SCRIPT_FAULT:
        if marker in text:
            return ""
    for marker in CRASH_MARKERS:
        if marker in text:
            return marker
    return ""


def probe(argv: list[str], cwd: str | Path, timeout: int = 120) -> dict:
    """跑一条验收命令，只关心它是不是自己崩了（不关心断言过没过）。"""
    started = time.time()
    try:
        rc, out, err, timedout = run_capped(list(argv), cwd=str(cwd), timeout=timeout)
    except OSError as exc:  # 命令本身起不来（解释器不在、路径错）
        return {"argv": list(argv), "code": 127, "reason": f"OSError: {exc}",
                "output": str(exc), "timedout": False, "seconds": 0}
    text = (out or "") + (err or "")
    reason = "" if timedout else crash_reason(text)
    return {"argv": list(argv), "code": rc, "reason": reason, "output": text,
            "timedout": timedout, "seconds": int(time.time() - started)}


def probe_all(pairs: list[tuple[str, list[str]]], cwd: str | Path,
              timeout: int = 120) -> list[dict]:
    """逐条试跑，只回"脚本自己崩了"的那些。pairs 是 (原始命令文本, argv)。"""
    broken = []
    for raw, argv in pairs:
        if not argv:
            continue
        result = probe(argv, cwd, timeout=timeout)
        if result["reason"]:
            broken.append({"command": raw, **result})
    return broken


def render_broken(broken: list[dict], round_no: int = 0) -> str:
    """写成 `verify-script-broken.md`：给大脑看的返工说明。"""
    head = f"# 验收命令自身报错（不是执行者的问题）{f' - 第 {round_no} 轮' if round_no else ''}"
    lines = [head, "", "下面这些命令在跑的时候直接崩了，说明**验收命令本身写错了**"
                       "（正则假设了不存在的符号、变量名写错、单位混了等）：", ""]
    for item in broken:
        lines += [f"## `{item['command']}`",
                  f"- 崩在：{item['reason']}（退出码 {item['code']}，{item['seconds']}s）",
                  "```", (item.get("output") or "").strip()[-1500:], "```", ""]
    lines += [
        "## 怎么办",
        "1. 让大脑按文件里**真实存在**的符号/结构重写这几条验收命令（先读文件再写正则）；",
        "2. 重写后重新派发这张卡；",
        "3. 不要给执行者派返工单——这是验收脚本的问题，改代码不可能让它通过。",
        "",
    ]
    return "\n".join(lines)


BROKEN_FILE = "verify-script-broken.md"


def marker_path(task_dir: str | Path) -> Path:
    return Path(task_dir) / BROKEN_FILE