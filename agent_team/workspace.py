#!/usr/bin/env python3
"""工作区校验：确定「改哪个目录」，并把路径越界挡在外面。

协作台踩过最疼的坑是「绑错工作区」：任务卡绑到协作台自己的仓库，
跑完才发现代码进了账本、项目里啥也没改。所以这里一律严格：

- 必须存在、必须是目录、必须能上浮到最近的 ``.git`` 根；
- 不许把协作台自己的目录（含子目录）当工作区；
- 工作区内的相对路径做越界检查（``resolve()`` 后比前缀，防 ``..`` 与符号链接）；
- 任务卡 ID 只允许 ``T-\\d{6}-\\d{6}``。
"""
from __future__ import annotations

import re
from pathlib import Path
from .paths import workbench_root  # noqa: E402

# 协作台仓库根（room/、tasks/ 都在它下面）
ROOT = workbench_root()

# 任务卡 ID 的唯一样式：T-<六位日期>-<六位时间>，例如 T-261031-120000
TASK_ID_PATTERN = r"T-\d{6}-\d{6}"
TASK_ID_RE = re.compile(rf"^{TASK_ID_PATTERN}$")

_QUOTES = ("'", '"', "“", "”", "「", "」", "『", "』")
_FILE_PREFIXES = ("file://", "file:")


def normalize(raw: str) -> str:
    """去掉首尾空格、成对引号、``file:`` 前缀和尾部斜杠（盘符根 ``E:\\`` 的斜杠保留）。"""
    text = str(raw or "").strip()
    # 用户从资源管理器复制路径时常常带着引号，最多剥三层
    for _ in range(3):
        if len(text) >= 2 and text[0] in _QUOTES and text[-1] in _QUOTES:
            text = text[1:-1].strip()
        else:
            break
    lowered = text.lower()
    for prefix in _FILE_PREFIXES:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            # file:///E:/proj 这种三段斜杠的写法，去掉盘符前多出来的那根
            if len(text) > 2 and text[0] == "/" and text[2] == ":":
                text = text[1:]
            break
    # 尾部斜杠会让人以为路径不同，统一去掉；"E:\" 这种根目录按长度兜住
    while len(text) > 3 and text[-1] in ("/", "\\"):
        text = text[:-1]
    return text


def _git_root(path: Path) -> Path | None:
    """从 path 逐级上浮到最近的 .git 根；.git 可能是目录也可能是 worktree 的文件。"""
    current = path
    while True:
        try:
            if (current / ".git").exists():
                return current
        except OSError:
            return None
        if current.parent == current:          # 到盘符根了
            return None
        current = current.parent


def is_inside_ledger(path: Path) -> bool:
    """true 表示这是协作台自己（或它的子目录），不能当工作区。"""
    try:
        target = Path(path).expanduser().resolve()
        ledger = ROOT.resolve()
    except OSError:
        return False
    return target == ledger or ledger in target.parents


def validate(raw: str) -> tuple[Path | None, str]:
    """严格模式校验工作区，返回 ``(工作区, 错误原因)``；合法时错误原因为空串。"""
    text = normalize(raw)
    if not text:
        return None, "没有指定工作区路径"

    raw_path = Path(text).expanduser()
    if not raw_path.is_absolute():
        # 相对路径会跟着进程当前目录跑，历史上就是这么绑错工作区的
        return None, f"工作区必须是绝对路径：{text}"
    try:
        if not raw_path.exists():
            return None, f"工作区不存在：{text}"
        if not raw_path.is_dir():
            return None, f"工作区必须是目录，不能是文件：{text}"
        resolved = raw_path.resolve()
    except OSError as exc:
        return None, f"工作区路径无法解析：{text}（{exc}）"

    if is_inside_ledger(resolved):
        return None, "不能用协作台自己的目录当工作区，请指定你要改的那个项目"

    git_root = _git_root(resolved)
    if git_root is None:
        return None, f"不是 git 仓库（从 {resolved} 往上找不到 .git）：{text}"
    try:
        git_root = git_root.resolve()
    except OSError as exc:
        return None, f"git 仓库根目录无法解析：{git_root}（{exc}）"
    if is_inside_ledger(git_root):
        return None, "不能用协作台自己的目录当工作区，请指定你要改的那个项目"
    return git_root, ""


def is_valid_task_id(value: str) -> bool:
    """任务卡 ID 是否合法（只认 T-######-######）。"""
    return bool(TASK_ID_RE.match(str(value or "").strip()))


def validate_task_id(value: str) -> tuple[bool, str]:
    """任务卡 ID 校验，返回 ``(是否合法, 中文原因)``。"""
    tid = str(value or "").strip()
    if not tid:
        return False, "任务 ID 不能为空"
    if not TASK_ID_RE.match(tid):
        return False, f"任务 ID 不合法：{tid}（只允许 {TASK_ID_PATTERN}，例如 T-261031-120000）"
    return True, ""


def _task_id_in_path(text: str) -> tuple[bool, str]:
    """路径里出现任务卡（tasks/<ID>/… 或 T-<ID>/…）时校验 ID。"""
    parts = [part for part in text.split("/") if part and part != "."]
    if not parts:
        return True, ""
    tid = ""
    if parts[0] == "tasks" and len(parts) >= 2:
        tid = parts[1]
    elif parts[0].upper().startswith("T-"):
        tid = parts[0]
    if not tid:
        return True, ""
    return validate_task_id(tid)


def ensure_workspace_scope(workspace: Path, rel: str) -> tuple[bool, str]:
    """检查 rel 是不是工作区内的合法相对路径，返回 ``(是否在界内, 中文原因)``。"""
    if workspace is None:
        return False, "还没有绑定工作区"
    try:
        root = Path(workspace).expanduser().resolve()
    except OSError as exc:
        return False, f"工作区路径无法解析：{workspace}（{exc}）"
    if not root.is_dir():
        return False, f"工作区不存在或不是目录：{root}"

    text = str(rel or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    if not text:
        return False, "路径不能为空"

    ok, why = _task_id_in_path(text)
    if not ok:
        return False, why

    candidate = Path(text)
    if candidate.is_absolute() or candidate.drive:
        return False, f"只允许工作区内的相对路径：{rel}"
    try:
        target = (root / candidate).resolve()
    except (OSError, RuntimeError) as exc:      # RuntimeError：符号链接成环
        return False, f"路径无法解析：{rel}（{exc}）"
    try:
        target.relative_to(root)
    except ValueError:
        return False, f"路径越界：{rel} 不在工作区 {root} 内"
    return True, ""
