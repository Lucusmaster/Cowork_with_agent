#!/usr/bin/env python3
"""阻塞等待一个任务跑完，然后把结果一次说清楚。

  python bin/await_task.py -t T-260101-120000              # 等它到终态
  python bin/await_task.py -t <ID> --timeout 1800 --interval 3

终态：passed / failed / exec-failed / stopped。
退出码：0 通过 / 2 未通过或中止 / 3 超时 / 4 任务不存在。

用途：在会话里"发任务 → 等 → 回结果"，不产生任何定时提醒噪音。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from .paths import workbench_root  # noqa: E402

ROOT = workbench_root()
TASKS = ROOT / "tasks"

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TERMINAL = {"passed", "failed", "exec-failed", "stopped"}
LABEL = {
    "planned": "待执行", "dispatched": "执行中", "executed": "已执行待验收",
    "passed": "通过", "failed": "验收失败", "exec-failed": "执行失败", "stopped": "已中止",
}


def read_card(tid: str) -> dict | None:
    card = TASKS / tid / "task.json"
    try:
        return json.loads(card.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def tail(path: Path, lines: int = 8) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:])


def latest(directory: Path, pattern: str) -> Path | None:
    found = [p for p in directory.glob(pattern) if p.is_file() and p.stat().st_size > 0]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def summarize(tid: str, task: dict) -> int:
    directory = TASKS / tid
    status = task.get("status") or "?"
    print(f"任务      {tid}")
    print(f"标题      {task.get('title') or '-'}")
    print(f"状态      {status}（{LABEL.get(status, status)}）")
    print(f"工作区    {task.get('workspace') or '-'}")
    print(f"轮次      {task.get('rounds') or task.get('round') or 0}")

    verify = latest(directory, "verify-*.txt")
    if verify:
        print(f"\n验收输出（{verify.name} 末几行）:")
        print(tail(verify))
        passed = status == "passed"
        print(f"\n退出结论  {'PASS' if passed else 'FAIL'}")

    review = latest(directory, "review-*.last.md")
    if review:
        text = review.read_text(encoding="utf-8", errors="replace")
        verdict = "NEEDS_FIX" if "NEEDS_FIX" in text.upper() else ("PASS" if "PASS" in text.upper() else "未判定")
        print(f"复核      {verdict}（{review.name}）")

    report = directory / "report.md"
    if report.is_file():
        print(f"报告      {report}")
    else:
        print("报告      还没生成（finalize 未跑）")

    return 0 if status == "passed" else 2


def is_truly_done(tid: str, task: dict) -> bool:
    """状态到终态还不够：复核要跑、报告要生成，才算这一单真的完了。"""
    if (task.get("status") or "") not in TERMINAL:
        return False
    if task.get("finished_at"):
        return True
    return (TASKS / tid / "report.md").is_file()


def wait_for(tid: str, timeout: float = 1800, interval: float = 3,
             quiet: bool = False, tail_wait: float = 300) -> int:
    """阻塞到任务进入终态；返回退出码（0 通过 / 2 未通过 / 3 超时 / 4 不存在）。"""
    started = time.time()
    terminal_at = 0.0
    last_status = ""
    while True:
        task = read_card(tid)
        if task is None:
            print(f"任务不存在：{tid}（看下 python bin/team.py status）", file=sys.stderr)
            return 4
        status = task.get("status") or "?"
        if not quiet and status != last_status:
            print(f"[{time.strftime('%H:%M:%S')}] {tid} → {status}（{LABEL.get(status, status)}）", flush=True)
            last_status = status
        if status in TERMINAL:
            if is_truly_done(tid, task):
                if not quiet:
                    print()
                return summarize(tid, task)
            if not terminal_at:
                terminal_at = time.time()
                if not quiet:
                    print(f"[{time.strftime('%H:%M:%S')}] 验收结束，等复核和报告收尾…", flush=True)
            elif time.time() - terminal_at > tail_wait:
                if not quiet:
                    print("（等收尾超时，按当前状态给结论）")
                return summarize(tid, task)
            time.sleep(interval)
            continue
        if time.time() - started > timeout:
            print(f"\n超时：等了 {int(timeout)} 秒还是 {status}（{LABEL.get(status, status)}）", file=sys.stderr)
            return 3
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description="阻塞等待任务完成")
    ap.add_argument("-t", "--task", required=True)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--interval", type=float, default=3)
    ap.add_argument("--quiet", action="store_true", help="只在结束时输出")
    args = ap.parse_args()
    return wait_for(args.task, args.timeout, args.interval, args.quiet)


if __name__ == "__main__":
    sys.exit(main())
