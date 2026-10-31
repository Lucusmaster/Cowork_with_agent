#!/usr/bin/env python3
"""一句话跑完整个协作：自动拉起服务 → 投递任务 → 阻塞等结果 → 用完停机。

  python bin/run_task.py --text "在 D:\\work\\my-project 里给 foo.py 加 --verbose 开关，测试要过"

行为：
- 服务没跑就自动拉起（跑完再关掉，不留后台）；
- 任务交给引擎，规划/执行/验收/复核全自动；
- 等任务到终态后把证据打出来，退出码 0 通过 / 2 未通过 / 3 超时；
- 会话里只需一条命令，不需要任何定时提醒。

常用参数：
  --timeout 1800   最多等多久（秒）
  --keep           跑完不停服务（想接着看网页）
  --no-start       不自动拉起服务（假定已经在跑）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent

ROOT = BIN.parent
TASKS = ROOT / "tasks"

from . import await_task  # noqa: E402
from . import room  # noqa: E402
from . import start_all  # noqa: E402

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def known_tasks() -> set[str]:
    if not TASKS.is_dir():
        return set()
    return {d.name for d in TASKS.iterdir() if d.is_dir()}


def find_new_task(before: set[str], timeout: float = 90) -> str | None:
    """引擎会在 3 秒内建卡；这里等新卡出现。"""
    started = time.time()
    while time.time() - started < timeout:
        new = known_tasks() - before
        if new:
            # 取最新建的那张
            return max(new, key=lambda name: (TASKS / name / "task.json").stat().st_mtime
                       if (TASKS / name / "task.json").is_file() else 0)
        time.sleep(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="一句话跑完协作任务")
    ap.add_argument("--text", required=True, help="需求原文（要点名仓库目录）")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--timeout", type=float, default=1800)
    ap.add_argument("--interval", type=float, default=3)
    ap.add_argument("--keep", action="store_true", help="跑完不停服务")
    ap.add_argument("--no-start", action="store_true", help="不自动拉起服务")
    args = ap.parse_args()

    started_services = False
    if not args.no_start:
        if not start_all.port_busy(args.port) or not start_all.worker_pid():
            start_all.start(args.port)
            started_services = True
            time.sleep(1)
        else:
            print("服务已在运行，直接投递。")
    else:
        print("按参数要求不拉起服务。")

    before = known_tasks()
    room.post("user", args.text, "task", None)
    print("已投递，等引擎接单…")

    tid = find_new_task(before, timeout=90)
    if not tid:
        print("引擎 90 秒内没建卡（可能被反问、或需求没点名仓库）。用 python bin\\room.py tail 看它回了什么。",
              file=sys.stderr)
        if started_services and not args.keep:
            start_all.stop(args.port)
        return 5

    print(f"任务号 {tid}，开始等结果…\n")
    code = await_task.wait_for(tid, args.timeout, args.interval)

    if started_services and not args.keep:
        active = [name for name in known_tasks()
                  if await_task.read_card(name) and
                  (await_task.read_card(name) or {}).get("status") not in await_task.TERMINAL]
        if active:
            print(f"\n还有 {len(active)} 个任务在跑，服务先留着：{', '.join(active)}")
        else:
            start_all.stop(args.port)
            print("\n服务已停机（本次是它自己拉起来的）。")
    return code


if __name__ == "__main__":
    sys.exit(main())