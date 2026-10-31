"""进程与心跳的公共判断。

两个坑必须踩过才知道：

1. **Windows 上 `os.kill(pid, 0)` 会直接杀掉进程**（CPython 把任何非 Ctrl 信号转成
   `TerminateProcess`），所以"进程还在不在"绝对不能用它来判断。
2. **心跳过期 ≠ 进程死了**。长任务执行期间主循环被占住，心跳可能几十秒不刷新，
   但进程活得好好的。判断"该不该接管锁"和"界面上引擎还在不在"是两件事：
   - 接管锁：进程活着就不许接管（哪怕心跳旧）；
   - 界面显示：进程活着但心跳旧 = "卡住了/在忙"，不是"没启动"。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

HEARTBEAT_TIMEOUT = 90.0     # 心跳多久没更新算旧


def quiet_flags() -> int:
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def pid_alive(pid: int) -> bool:
    """跨平台判断进程是否真的还在。"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, errors="replace",
                creationflags=quiet_flags(), timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True          # 进程在，只是不是我们用户
    except OSError:
        return False


_READ_RETRIES = 6


def read_lock(path: Path) -> dict:
    """读锁文件；文件不存在就是没有引擎，返回空。

    Windows 上 `os.replace` 的瞬间，目标文件会短暂处于 delete-pending 状态，
    这时 `open()` 会拿到 `PermissionError`（sharing violation）。心跳每 10 秒重写一次，
    网页每 1.5 秒读一次，撞上就把引擎误判成"没在跑"，所以这里对 OSError 重试几轮
    （总计 ~150ms，失败就返回空，不影响正常路径）。
    """
    path = Path(path)
    for attempt in range(_READ_RETRIES):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            return {}
        except OSError:
            if attempt == _READ_RETRIES - 1:
                return {}
            time.sleep(0.005 * (attempt + 1))
            continue
        return data if isinstance(data, dict) else {}
    return {}


def lock_state(path: Path, heartbeat_timeout: float = HEARTBEAT_TIMEOUT) -> dict:
    """把锁文件翻译成界面能用的状态。

    {"pid": int, "beat": float, "age": 秒, "alive": 进程在不在,
     "stale": 心跳是否过期, "status": "running"|"busy"|"stalled"|"dead"|"none"}
    """
    info = read_lock(path)
    pid = int(info.get("pid") or 0)
    beat = float(info.get("beat") or 0)
    age = round(time.time() - beat, 1) if beat else -1.0
    alive = pid_alive(pid) if pid else False
    stale = beat <= 0 or age > heartbeat_timeout
    if not pid:
        status = "none"
    elif not alive:
        status = "dead"
    elif stale:
        status = "stalled"
    else:
        status = "running" if age < 30 else "busy"
    return {"pid": pid, "beat": beat, "age": age, "alive": alive,
            "stale": stale, "status": status}