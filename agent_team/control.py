"""运行中控制：暂停 / 继续 / 停止 / 批准 / 重新执行。

设计：控制指令落在一个小文件里（`room/control.json`），引擎在每个步骤边界消费它。
好处是网页、命令行、以后的手机端都能往同一个地方写，引擎不需要长连接。

```python
control.request("pause", task_id)      # 写指令
action = control.take(task_id)         # 引擎消费（读完即删）
control.set_paused(task_id, "等批准")  # 引擎在等的时候写状态，界面据此显示
control.clear_paused()
control.gate(task_id)                  # 引擎在每个步骤边界调用，阻塞到有结果
```
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from .paths import workbench_root  # noqa: E402

ROOT = workbench_root()
ROOM = ROOT / "room"
CONTROL = ROOM / "control.json"     # 指令（一次一条，消费即删）
PAUSE = ROOM / "pause.json"         # 暂停中标记（含原因与任务号）

ACTIONS = ("pause", "resume", "stop", "approve", "rerun")
LABELS = {
    "pause": "暂停",
    "resume": "继续",
    "stop": "停止",
    "approve": "批准",
    "rerun": "重新执行",
}


class TaskStopped(RuntimeError):
    """用户要求停止当前任务。"""


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def request(action: str, task_id: str | None = None, note: str = "") -> dict:
    """网页/命令行下发一条控制指令。后到的覆盖先到的（同一时刻只认一条）。"""
    action = str(action or "").strip().lower()
    if action not in ACTIONS:
        raise ValueError(f"不支持的控制指令：{action}（只认 {'/'.join(ACTIONS)}）")
    payload = {
        "action": action,
        "task": (task_id or "").strip(),
        "note": note,
        "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts": time.time(),
    }
    _atomic_write(CONTROL, payload)
    return payload


def take(task_id: str | None = None) -> dict | None:
    """引擎消费指令：有就取走并删除；task 为空表示对任何任务生效。"""
    data = _read(CONTROL)
    if not data:
        return None
    target = str(data.get("task") or "")
    if task_id and target and target != task_id:
        return None           # 指令是给别的任务的，留着
    try:
        CONTROL.unlink(missing_ok=True)
    except OSError:
        return None
    return data


def pending() -> dict | None:
    return _read(CONTROL)


def set_paused(task_id: str, reason: str = "", stage: str = "") -> None:
    _atomic_write(PAUSE, {
        "task": task_id or "",
        "stage": stage,
        "reason": reason,
        "since": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ts": time.time(),
    })


def clear_paused() -> None:
    try:
        PAUSE.unlink(missing_ok=True)
    except OSError:
        pass


def paused() -> dict | None:
    data = _read(PAUSE)
    if not data:
        return None
    # 超过 2 小时的暂停视为失效，避免引擎永远卡住
    if time.time() - float(data.get("ts") or 0) > 7200:
        clear_paused()
        return None
    return data


def gate(task_id: str, stage: str = "", on_event=None, timeout: float = 7200,
         heartbeat=None) -> str:
    """引擎在步骤边界调用。返回 "go" / "rerun"，要求停止时抛 TaskStopped。

    - pause：写 PAUSE 标记并阻塞，直到用户在网页点「继续」；
    - stop：抛 TaskStopped；
    - rerun：返回 "rerun"，调用方重做这一步；
    - approve：当作「继续」处理（批准后放行）。
    """
    deadline = time.time() + timeout

    def tick() -> None:
        """暂停/等待期间也要报活，别让界面把引擎判成死了。"""
        if heartbeat is not None:
            try:
                heartbeat()
            except Exception:  # noqa: BLE001
                pass

    while True:
        action = take(task_id)
        if action is None:
            # 没人按暂停，也没有指令：放行
            return "go"
        kind = action.get("action")
        if kind == "pause":
            set_paused(task_id, "用户点了暂停", stage)
            if on_event:
                on_event("paused", stage)
            while True:
                if time.time() > deadline:
                    clear_paused()
                    return "go"
                nxt = take(task_id)
                if nxt is None:
                    tick()
                    time.sleep(0.4)
                    continue
                if nxt.get("action") in ("resume", "approve"):
                    clear_paused()
                    if on_event:
                        on_event("resumed", stage)
                    return "go"
                if nxt.get("action") == "stop":
                    clear_paused()
                    raise TaskStopped("用户停止了这个任务")
                if nxt.get("action") == "pause":
                    continue
                if nxt.get("action") == "rerun":
                    clear_paused()
                    return "rerun"
        if kind in ("resume", "approve"):
            return "go"
        if kind == "stop":
            raise TaskStopped("用户停止了这个任务")
        if kind == "rerun":
            return "rerun"
        return "go"


def wait_approval(task_id: str, on_event=None, timeout: float = 3600,
                  heartbeat=None) -> str:
    """批准闸门：停下来等用户点「批准」或「停止」，期间不跑任何命令。

    返回 "go"（批准）/ "rerun"（要求重新规划）；用户点停止时抛 TaskStopped。
    """
    set_paused(task_id, "等你批准后再动手", "approve")
    if on_event:
        on_event("waiting_approval", "approve")
    deadline = time.time() + timeout
    while time.time() < deadline:
        cmd = take(task_id)
        if cmd is None:
            if heartbeat is not None:
                try:
                    heartbeat()
                except Exception:  # noqa: BLE001
                    pass
            time.sleep(0.4)
            continue
        kind = cmd.get("action")
        if kind in ("approve", "resume"):
            clear_paused()
            if on_event:
                on_event("approved", "approve")
            return "go"
        if kind == "stop":
            clear_paused()
            raise TaskStopped("用户停止了这个任务")
        if kind == "rerun":
            clear_paused()
            return "rerun"
    clear_paused()
    return "go"
