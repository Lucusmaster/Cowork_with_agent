"""Windows 上启动子进程时不要弹控制台窗口。

后台引擎每次跑 codex / git / tasklist，如果不加 CREATE_NO_WINDOW，
屏幕上就会闪一下黑框。所有 subprocess 调用统一走这里的 flags。

另外，`run_capped()` 是这里唯一的"带硬超时"入口：`subprocess.run` 的 `timeout=`
在 Windows 上杀不掉孙进程，只要还有孙进程握着 stdout 管道，它收尾时那次
`communicate()`（没有超时）就会一直挂着——2026-11-02 实测执行者 900s 超时后，
dispatch 又挂了 20 分钟没返回，界面上就是"卡在上一轮不动了"。超时必须连进程树一起杀。
"""
from __future__ import annotations

import os
import subprocess

# 0x08000000 = CREATE_NO_WINDOW；0x00000008 = DETACHED_PROCESS
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
NEW_PROCESS_GROUP = 0x00000200


def quiet_flags() -> int:
    """普通子进程：不弹窗。"""
    return CREATE_NO_WINDOW if os.name == "nt" else 0


def detached_flags() -> int:
    """要脱离当前终端常驻的进程。"""
    return DETACHED_PROCESS if os.name == "nt" else 0


def kill_tree(proc: subprocess.Popen) -> None:
    """连子进程一起杀。Windows 上 `proc.kill()` 只杀直接子进程，孙子进程会变孤儿。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=quiet_flags(), timeout=20)
            if proc.poll() is not None:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
    except OSError:
        pass


def run_capped(cmd: list[str], *, cwd: str | None = None, input_text: str | None = None,
               env: dict | None = None, timeout: float = 0,
               encoding: str = "utf-8", errors: str = "replace",
               capture_output: bool = True) -> tuple[int, str, str, bool]:
    """跑一条命令，超时返回 `(124, 已有输出, 已有错误, True)`。

    超时先杀整棵进程树，再最多等 10s 收尾；收尾还收不回来就关掉管道把已有的丢掉，
    绝不把调用方吊死在这里。
    """
    if not timeout:
        raise ValueError("run_capped 必须给 timeout")
    stdin = subprocess.PIPE if input_text is not None else subprocess.DEVNULL
    proc = subprocess.Popen(
        cmd, cwd=cwd, stdin=stdin,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        text=True, encoding=encoding, errors=errors, env=env,
        creationflags=quiet_flags())
    timedout = False
    try:
        out, err = proc.communicate(input_text, timeout=timeout)
    except subprocess.TimeoutExpired:
        timedout = True
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except (OSError, AttributeError):
                    pass
            out, err = "", ""
    rc = 124 if timedout else proc.returncode
    return rc, out or "", err or "", timedout