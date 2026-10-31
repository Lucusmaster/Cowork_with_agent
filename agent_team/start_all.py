#!/usr/bin/env python3
"""跨平台启动器：拉起协作台网页 + 工作流引擎；也可用来关停。

  python bin/start_all.py [--port 8899]     # 启动（幂等，已在跑就跳过）
  python bin/start_all.py --stop            # 关停两个进程
  python bin/start_all.py --status          # 只看状态

Windows 上进程以 DETACHED 方式启动，关掉终端也继续跑。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from .winproc import quiet_flags
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ROOM = ROOT / "room"
LOCK = ROOM / "worker.lock"
WORKER = ["-m", "agent_team.worker"]
WEB = ["-m", "agent_team.room_web"]

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def spawn(args: list[str]) -> int:
    """脱离当前终端启动子进程，返回 pid。"""
    proc = subprocess.Popen(
        [sys.executable, *args],
        cwd=str(ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=(os.name != "nt"),
        # 脱离终端 + 不弹控制台窗口：DETACHED_PROCESS | NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        creationflags=(0x00000008 | 0x00000200 | 0x08000000) if os.name == "nt" else 0,
    )
    return proc.pid


def pid_alive(pid: int) -> bool:
    """跨平台判断进程是否真活着（Windows 不要用 os.kill(pid,0)，那会直接干掉进程）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                 capture_output=True, text=True, errors="replace", creationflags=quiet_flags()).stdout
        except OSError:
            return False
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def worker_pid() -> int:
    try:
        info = json.loads(LOCK.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if Path(str(info.get("root") or "")).resolve() != ROOT.resolve():
        return 0   # 别的仓库的锁，不认
    pid = int(info.get("pid") or 0)
    beat = float(info.get("beat") or 0)
    # 引擎每 3 秒刷新一次心跳；心跳超过 90 秒就认为它已经不在了（跨平台，不查进程表）
    if pid and (time.time() - beat) < 90 and pid_alive(pid):
        return pid
    return 0


def kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, text=True, errors="replace", creationflags=quiet_flags())
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def find_web_pid(port: int) -> list[int]:
    """找到占着这个端口的进程号（跨平台，尽量不依赖额外库）。"""
    pids: list[int] = []
    if os.name == "nt":
        proc = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True,
                              text=True, errors="replace", creationflags=quiet_flags())
        for line in (proc.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                try:
                    pids.append(int(parts[4]))
                except ValueError:
                    pass
    else:
        proc = subprocess.run(["sh", "-c", f"lsof -ti tcp:{port} 2>/dev/null || true"],
                              capture_output=True, text=True, errors="replace", creationflags=quiet_flags())
        for token in (proc.stdout or "").split():
            try:
                pids.append(int(token))
            except ValueError:
                pass
    return sorted(set(pids))


def status(port: int) -> None:
    print(f"仓库      {ROOT}")
    print(f"协作台    {'在跑' if port_busy(port) else '未启动'}  http://127.0.0.1:{port}/")
    pid = worker_pid()
    print(f"引擎      {'在跑 pid ' + str(pid) if pid else '未启动'}")
    print(f"日志      {ROOM / 'worker.log'}")
    active = []
    tasks_dir = ROOT / "tasks"
    if tasks_dir.is_dir():
        for directory in tasks_dir.iterdir():
            card = directory / "task.json"
            if not card.is_file():
                continue
            try:
                task = json.loads(card.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if (task.get("status") or "") not in {"passed", "failed", "exec-failed", "stopped"}:
                active.append(f"{directory.name}({task.get('status')})")
    print(f"进行中    {', '.join(active) if active else '无'}")
    state_file = ROOM / "session_state.json"
    try:
        import json as _json
        st = _json.loads(state_file.read_text(encoding="utf-8"))
        reason = st.get("reason") or "-"
        print(f"协作状态  {st.get('collab')}（{st.get('at')}，{reason}）")
    except (OSError, ValueError):
        print("协作状态  还没有记录")


def start(port: int) -> int:
    ROOM.mkdir(parents=True, exist_ok=True)
    if port_busy(port):
        print(f"协作台已在运行: http://127.0.0.1:{port}/")
    else:
        spawn([*WEB, "--port", str(port)])
        for _ in range(20):
            if port_busy(port):
                break
            time.sleep(0.25)
        print(f"协作台已启动: http://127.0.0.1:{port}/" if port_busy(port)
              else "协作台启动失败，看 room/web.out 或直接前台跑 python bin/room_web.py")
    pid = worker_pid()
    if pid:
        print(f"引擎已在运行 (pid {pid})")
    else:
        if LOCK.exists():
            LOCK.unlink(missing_ok=True)
        new_pid = spawn([*WORKER, "--interval", "0.5"])
        print(f"引擎已启动 (pid {new_pid})：发任务后 3 秒内自动开工")
    print()
    print("打开上面的地址，中间输入框发任务即可。")
    return 0


def stop(port: int) -> int:
    pid = worker_pid()
    kill_pid(pid)
    print(f"引擎已停止 (pid {pid})" if pid else "引擎本来就没在跑")
    LOCK.unlink(missing_ok=True)
    web_pids = find_web_pid(port)
    for item in web_pids:
        kill_pid(item)
    print(f"协作台已停止 ({len(web_pids)} 个进程)" if web_pids else "协作台本来就没在跑")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="协作台 + 工作流引擎启动器")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--restart", action="store_true", help="先停再起（改了代码后用）")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()
    if args.status:
        status(args.port)
        return 0
    if args.stop:
        return stop(args.port)
    if args.restart:
        stop(args.port)
        time.sleep(1)
        return start(args.port)
    return start(args.port)


if __name__ == "__main__":
    sys.exit(main())