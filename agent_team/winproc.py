"""Windows 上启动子进程时不要弹控制台窗口。

后台引擎每次跑 codex / git / tasklist，如果不加 CREATE_NO_WINDOW，
屏幕上就会闪一下黑框。所有 subprocess 调用统一走这里的 flags。
"""
from __future__ import annotations

import os

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