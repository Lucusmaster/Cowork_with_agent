#!/usr/bin/env python3
"""多 Agent 协作台（聊天式网页）：http://127.0.0.1:8899

界面按 docs/REFACTOR-PLAN.md 第 4 节做成"聊天产品"，三栏：

  左：工作区信息 + 历史会话（可切换、可新建）
  顶：工作区选择器（输入绝对路径 → 验证 → 失败显示原因）
  中：ChatGPT 式消息流（用户右侧气泡、Agent 左侧带头像与角色色、artifact 可点开预览）
  右：Agent 状态 / 当前任务 / 测试结果 / 代码改动

会话读写走 agent_team/session.py；它还没就绪时自动降级到旧账本 room.read_all()，
保证网页永远能起来（阶段一过渡方案）。

  python -m agent_team.room_web [--port 8899] [--no-autostop]
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from .winproc import quiet_flags
from .paths import workbench_root  # noqa: E402
from . import models as MODELS  # noqa: E402
from . import agent_clis as AGENT_CLIS  # noqa: E402
from . import api_providers as API_PROVIDERS  # noqa: E402
from . import __version__  # noqa: E402


def _models_snapshot() -> dict:
    """右栏要的模型状态；出错也不能让整个 /api/state 挂掉。"""
    try:
        return MODELS.snapshot()
    except Exception:  # noqa: BLE001 - 模型注册表坏了不该拖垮界面
        return {"roles": [], "registry": [], "agents": [], "clis": {}, "overrides": {}}


def _room_note(text: str) -> None:
    """往当前会话记一条系统事件（换模型这类动作要留痕）；没有会话就算了。"""
    try:
        sid = str(STORE.active().get("id") or "")
        if not sid or not str(text).strip():
            return
        STORE.append(sid, role="system", agent="system", content=str(text),
                     artifacts=[], status="complete", meta={"phase": "model"}, kind="event")
    except Exception:  # noqa: BLE001 - 记不上一条事件不该让切换本身失败
        pass

try:
    from . import room
except Exception:  # noqa: BLE001 - room.py 可能在 session.py 未就绪时导入失败，网页也得能起
    room = None

try:
    from . import control
except Exception:  # noqa: BLE001 - 控制模块缺失时网页照常能看，只是按钮点了没反应
    control = None

try:
    from . import procs
except Exception:  # noqa: BLE001 - 进程判断缺失时退化成"看不到引擎"，不耽误看消息
    procs = None

# Windows 控制台默认 GBK，中文会炸；统一按 UTF-8 输出。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = workbench_root()
ROOT_RESOLVED = ROOT.resolve()
TASKS = ROOT / "tasks"
ROOM = ROOT / "room"
PROJECT_FILE = ROOM / "project.json"
TYPING_FILE = ROOM / "typing.json"
NUDGE_FILE = ROOM / "nudge"
SESSION_STATE = ROOM / "session_state.json"
TOKEN_FILE = ROOM / "web-token"          # 网页访问令牌；main() 启动时读/生成
CODE_FILE = ROOM / "web-code"            # 一次性引导码：/?k= 换 Cookie，用掉就换新的
MAX_ARTIFACT = 200 * 1024
CONTROL_ACTIONS = ("pause", "resume", "stop", "approve", "rerun")

# 消息分页：默认只给最近一页，往前翻页走 /api/messages?before=
PAGE_SIZE = 120
MAX_PAGE = 500

# 访问令牌：启动时由 _ensure_token() 填。为空表示"没启用令牌"（安全兜底，不把人锁在外面）。
TOKEN = ""

# 凭证模型：Cookie 是主凭证（浏览器自动带），X-Team-Token 留给 CLI/测试/doctor。
# ?token= 只在 GET / 引导时用一次，校验通过就 302 到干净地址并把令牌种进 Cookie。
COOKIE_NAME = "team_token"
COOKIE_MAX_AGE = 30 * 24 * 3600          # 30 天
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SAFE_FETCH_SITES = frozenset({"same-origin", "none"})   # curl/CLI 没有这个头，走 Origin 那步
UNLOCK_WINDOW = 60.0                     # /api/unlock 失败计数的滑动窗口（秒）
UNLOCK_MAX_FAILS = 5                     # 窗口内失败到这个数就 429

# /api/unlock 的失败计数：只放内存，进程重启就清空（别落盘，免得把令牌写进文件）。
UNLOCK_FAILS: dict[str, list[float]] = {}
UNLOCK_LOCK = threading.Lock()

# 网页在线状态：页面每次轮询都会刷新 last_seen，关页面时尽力发一次 bye。
PRESENCE = {"last_seen": 0.0, "bye_at": 0.0, "ever": False, "deferred": False}

AGENTS = {
    "user": {"name": "你", "role": "提需求 · 定边界", "color": "#58a6ff", "initial": "我"},
    "coordinator": {"name": "协调者", "role": "理解 · 计划 · 汇总", "color": "#a371f7", "initial": "协"},
    "planner": {"name": "规划者", "role": "拆步骤 · 写验收", "color": "#39c5cf", "initial": "规"},
    "executor": {"name": "执行者", "role": "写代码 · 跑命令", "color": "#3fb950", "initial": "执"},
    "tester": {"name": "验收员", "role": "跑测试 · 报失败", "color": "#f0883e", "initial": "验"},
    "reviewer": {"name": "复核员", "role": "只读审查", "color": "#d29922", "initial": "复"},
    "system": {"name": "系统", "role": "调度与状态", "color": "#64748b", "initial": "·"},
}
AGENT_ROLES = ["coordinator", "planner", "executor", "tester", "reviewer"]
LEGACY_ID = "legacy"

STATUS_LABEL = {
    "planned": "已建卡",
    "executing": "执行中",
    "executed": "已执行",
    "exec-failed": "执行失败",
    "failed": "验收失败",
    "passed": "通过",
    "stopped": "已停止",
}
TERMINAL_STATUS = {"passed", "failed", "exec-failed", "stopped"}

# 旧账本字段 from → 新消息 agent
_LEGACY_AGENT = {
    "user": "user",
    "deepseek": "coordinator",
    "codex": "executor",
    "reviewer": "reviewer",
    "relay": "user",
    "system": "system",
}
# 新 agent → 旧账本作者（降级写回时用，保持 worker/旧读取方不变）
_AGENT_AUTHOR = {
    "user": "user",
    "coordinator": "deepseek",
    "planner": "deepseek",
    "executor": "codex",
    "tester": "deepseek",
    "reviewer": "reviewer",
    "system": "system",
}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------- 工作区模块

def _load_workspace_module():
    """agent_team/workspace.py 可用就借它的严格校验，不可用就自己来。"""
    try:
        from . import workspace as mod
    except Exception:  # noqa: BLE001 - 半成品模块也要能降级
        return None
    for name in ("validate", "normalize", "is_inside_ledger", "ensure_workspace_scope"):
        if not callable(getattr(mod, name, None)):
            return None
    return mod


WORKSPACE_MOD = _load_workspace_module()


def _norm_path(raw) -> str:
    text = str(raw or "").strip().strip('"').strip("'").strip()
    if WORKSPACE_MOD is not None:
        try:
            return str(WORKSPACE_MOD.normalize(text))
        except Exception:  # noqa: BLE001
            pass
    if len(text) > 3:
        text = text.rstrip("\\/")
    return text


def _inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return False
    return True


def _ledger_inside(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == ROOT_RESOLVED or ROOT_RESOLVED in resolved.parents


def _workspace_blocked(path: Path) -> tuple[bool, str]:
    """这个目录能不能当工作区：协作台自己的仓库（含它里面的子目录）不行。

    ``POST /api/workspace`` 本来就会拒（见 ``_ledger_inside``），这里提前算出来，
    目录对话框就能直接把「确认」按钮禁掉，而不是让用户点一次、等后端拒一次。
    """
    if not _ledger_inside(path):
        return False, ""
    try:
        same = path.resolve() == ROOT_RESOLVED
    except OSError:
        same = False
    if same:
        return True, "这是协作台自己的目录，不能当选工作区"
    return True, "这个目录在协作台仓库里面，不能当工作区"


def _run_git(args: list[str], workspace: str, timeout: float = 4.0) -> str:
    """只跑固定参数数组，绝不拼接 shell 字符串。"""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=workspace, capture_output=True, encoding="utf-8",
            errors="replace", timeout=timeout, creationflags=quiet_flags())
    except (OSError, subprocess.SubprocessError):
        return ""
    return (proc.stdout or "").strip()


def _git_toplevel(path: Path) -> str:
    return _run_git(["rev-parse", "--show-toplevel"], str(path))


def _fallback_validate(raw: str) -> tuple[Path | None, str]:
    text = _norm_path(raw)
    if not text:
        return None, "请填写工作区绝对路径"
    path = Path(text)
    if not path.is_absolute():
        return None, f"请填绝对路径，现在填的是：{text}"
    if not path.exists():
        return None, f"路径不存在：{text}"
    if not path.is_dir():
        return None, f"这不是目录：{text}"
    if _ledger_inside(path):
        return None, "不能把协作台自己的目录当工作区，换一个仓库路径"
    top = _git_toplevel(path)
    # Git 只是提供分支、diff 和产物摘要；没有 Git 也可以把任意可写目录作为新项目。
    return (Path(top) if top else path), ""


def _validate_workspace(raw: str) -> tuple[Path | None, str]:
    if WORKSPACE_MOD is not None:
        try:
            path, error = WORKSPACE_MOD.validate(_norm_path(raw))
        except Exception:  # noqa: BLE001
            path, error = None, ""
        else:
            if path is None:
                return None, str(error or "工作区不可用")
            return Path(path), ""
    return _fallback_validate(raw)


def _scope_ok(base: Path, rel: str) -> tuple[bool, str]:
    if WORKSPACE_MOD is not None:
        try:
            ok, why = WORKSPACE_MOD.ensure_workspace_scope(base, rel)
            return bool(ok), str(why or "")
        except Exception:  # noqa: BLE001
            pass
    try:
        target = (base / rel).resolve()
    except OSError as exc:
        return False, f"路径不可用：{exc}"
    return (True, "") if _inside(base, target) else (False, "路径越界：不在允许范围内")


# ---------------------------------------------------------------- 目录索引（选工作区用）
BROWSE_LIMIT = 200

# 这些目录对“挑仓库”没帮助：列出来但排到最后、标成 noise，前端会压暗
_BROWSE_NOISE = frozenset({
    "__pycache__", ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "env",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    ".cache", ".next", ".nuxt", "dist", "build", "target", ".gradle",
})


def _browse_roots() -> tuple[list[str], str]:
    """起点：Windows 给盘符；其它系统给主目录和 /。"""
    home = str(Path.home())
    if os.name == "nt":
        drives = [f"{c}:\\" for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.isdir(f"{c}:\\")]
        return (drives or [home], home)
    return (["~", "/"], home)


def _dir_summary(path: Path) -> dict:
    """轻量摘要：是不是 git 仓库、这一层有多少文件/子目录（不递归）。"""
    is_git = (path / ".git").exists()
    files = dirs = 0
    denied = False
    try:
        with os.scandir(path) as it:
            for item in it:
                try:
                    if item.is_dir(follow_symlinks=False):
                        dirs += 1
                    else:
                        files += 1
                except OSError:
                    continue
    except PermissionError:
        denied = True
    except OSError:
        denied = True
    return {"is_git": is_git, "files": files, "dirs": dirs, "denied": denied}


def _dir_hint(summary: dict, branch: str = "") -> str:
    if summary["denied"]:
        return "没有权限读取"
    bits = []
    if summary["is_git"]:
        bits.append("git 仓库" + ((" · " + branch) if branch else ""))
    if not summary["files"] and not summary["dirs"]:
        bits.append("空目录")
    else:
        if summary["files"]:
            bits.append(f"{summary['files']} 个文件")
        if summary["dirs"]:
            bits.append(f"{summary['dirs']} 个子目录")
    return " · ".join(bits)


def _browse_selected(path: Path) -> dict:
    """对当前路径的体检：确认之前就让人看清这是不是要的那个仓库。"""
    info = {"exists": False, "is_git": False, "git_branch": "", "file_count": 0,
            "entry_count": 0, "writable": False, "has_tasks": False,
            "blocked": False, "block_reason": ""}
    try:
        if not path.exists() or not path.is_dir():
            return info
    except OSError:
        return info
    info["exists"] = True
    info["blocked"], info["block_reason"] = _workspace_blocked(path)
    summary = _dir_summary(path)
    info["is_git"] = summary["is_git"]
    info["file_count"] = summary["files"]
    info["entry_count"] = summary["files"] + summary["dirs"]
    try:
        info["writable"] = bool(os.access(path, os.W_OK))
    except OSError:
        info["writable"] = False
    if summary["is_git"]:
        branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], str(path))
        if branch in ("", "HEAD"):           # 空仓库还没提交：HEAD 只是别名，读真分支名
            branch = _run_git(["symbolic-ref", "--short", "HEAD"], str(path))
        info["git_branch"] = branch
    try:
        info["has_tasks"] = _latest_task(str(path)) is not None
    except Exception:  # noqa: BLE001 - 账本读不了不算错
        info["has_tasks"] = False
    return info


def _browse_payload(raw_path) -> tuple[dict, int]:
    """GET /api/browse 的实现：只列目录，永远不吐文件内容。"""
    text = str(raw_path or "").strip().strip('"').strip("'").strip()
    if text and any("\x80" <= ch <= "\xff" for ch in text):
        try:      # 有的客户端不会 percent-encode，中文路径会以 latin-1 串进来，救一下
            text = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    roots, home = _browse_roots()
    if not text:
        entries = [{"name": r.rstrip("\\/") or r, "path": r, "is_git": False,
                    "hint": "系统盘" if os.name == "nt" else "起点", "noise": False,
                    "file_count": 0, "entry_count": 0, "writable": False,
                    "blocked": False, "block_reason": ""}
                   for r in roots]
        return ({"ok": True, "path": "", "parent": "", "entries": entries,
                 "drives": roots, "home": home, "truncated": False, "dir_count": len(entries),
                 "selected": {"exists": False, "is_git": False, "git_branch": "",
                              "file_count": 0, "entry_count": 0, "writable": False,
                              "has_tasks": False, "blocked": False, "block_reason": ""}}, 200)

    path = Path(os.path.abspath(os.path.expanduser(text)))
    try:
        if not path.exists():
            return ({"ok": False, "error": "目录不存在", "path": str(path)}, 404)
        if not path.is_dir():
            return ({"ok": False, "error": "这不是目录（只能选目录）", "path": str(path)}, 400)
        with os.scandir(path) as it:
            raw_entries = list(it)
    except PermissionError:
        return ({"ok": False, "error": "没有权限读取这个目录", "path": str(path)}, 403)
    except OSError as exc:
        return ({"ok": False, "error": f"读不了这个目录：{exc}", "path": str(path)}, 400)

    dirs: list[tuple[bool, str, str]] = []
    for item in raw_entries:
        try:
            if not item.is_dir(follow_symlinks=False):
                continue                 # 只列目录；文件名一个字都不往外给
        except OSError:
            continue
        name = item.name
        noise = name.startswith(".") or name.lower() in _BROWSE_NOISE
        dirs.append((noise, name, item.path))
    dirs.sort(key=lambda row: (row[0], row[1].lower()))     # 正常目录在前，噪音在后，各自按名字

    entries = []
    for noise, name, full in dirs[:BROWSE_LIMIT]:
        summary = _dir_summary(Path(full))
        blocked, block_reason = _workspace_blocked(Path(full))
        entries.append({"name": name, "path": os.path.normpath(full),
                        "is_git": summary["is_git"], "hint": _dir_hint(summary),
                        "noise": noise, "file_count": summary["files"],
                        "entry_count": summary["files"] + summary["dirs"],
                        "writable": not summary["denied"],
                        "blocked": blocked, "block_reason": block_reason})

    parent = "" if path.parent == path else os.path.normpath(str(path.parent))
    return ({"ok": True, "path": os.path.normpath(str(path)), "parent": parent,
             "entries": entries, "drives": roots, "home": home,
             "truncated": len(dirs) > BROWSE_LIMIT, "dir_count": len(dirs),
             "selected": _browse_selected(path)}, 200)


# ---------------------------------------------------------------- 会话适配层

_NEEDED_SESSION_FUNCS = (
    "sessions", "create_session", "active_session", "set_active",
    "append", "messages", "update", "context", "title_for",
)


def _legacy_item(raw: dict) -> dict:
    who = str(raw.get("from") or "system")
    agent = _LEGACY_AGENT.get(who, "coordinator")
    role = "user" if agent == "user" else ("system" if agent == "system" else "assistant")
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    mid = _as_int(raw.get("id"))
    item = {
        "id": f"legacy-{mid:06d}",
        "session_id": LEGACY_ID,
        "role": role,
        "agent": agent,
        "task_id": meta.get("task") or None,
        "content": str(raw.get("text") or ""),
        "artifacts": [],
        "status": "complete",
        "created_at": str(raw.get("at") or ""),
        "kind": str(raw.get("kind") or "msg"),
    }
    if meta:
        item["meta"] = meta
    return item


def _ledger_rows() -> list[dict]:
    """极端降级：连 room.py 都导入不了时，直接读旧账本 JSONL。"""
    try:
        text = (ROOM / "messages.jsonl").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _append_ledger(author: str, text: str, kind: str, meta: dict | None) -> dict:
    """极端降级：按旧格式追加一行到 room/messages.jsonl。"""
    rows = _ledger_rows()
    record = {
        "id": max((_as_int(r.get("id")) for r in rows), default=0) + 1,
        "at": _now(),
        "from": author,
        "kind": kind,
        "text": text,
    }
    if meta:
        record["meta"] = meta
    try:
        ROOM.mkdir(parents=True, exist_ok=True)
        with (ROOM / "messages.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError:
        pass
    return record


def _legacy_messages(limit: int = 200) -> list[dict]:
    rows: list[dict] = []
    if room is not None:
        try:
            rows = room.read_all()
        except Exception:  # noqa: BLE001
            rows = []
    if not rows:
        rows = _ledger_rows()
    return [_legacy_item(raw) for raw in rows][-limit:]


def _normalize_session(raw: dict) -> dict:
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or "").strip() or "新会话",
        "workspace": _norm_path(raw.get("workspace") or ""),
        "created_at": str(raw.get("created_at") or ""),
        "updated_at": str(raw.get("updated_at") or ""),
        "message_count": _as_int(raw.get("message_count")),
        "archived_at": str(raw.get("archived_at") or ""),
        "legacy": bool(raw.get("legacy")),
    }


def _normalize_message(raw: dict) -> dict:
    role = str(raw.get("role") or "").strip() or "assistant"
    agent = str(raw.get("agent") or "").strip()
    if not agent:
        agent = {"user": "user", "system": "system"}.get(role, "coordinator")
    status = str(raw.get("status") or "complete").strip() or "complete"
    artifacts = raw.get("artifacts") or []
    if isinstance(artifacts, str):
        artifacts = [artifacts]
    elif not isinstance(artifacts, list):
        artifacts = []
    item = {
        "id": str(raw.get("id") if raw.get("id") is not None else ""),
        "session_id": str(raw.get("session_id") or ""),
        "role": role,
        "agent": agent,
        "task_id": raw.get("task_id") or None,
        "content": str(raw.get("content") or ""),
        "artifacts": [str(a) for a in artifacts if str(a).strip()],
        "status": status,
        "created_at": str(raw.get("created_at") or ""),
    }
    if isinstance(raw.get("meta"), dict):
        item["meta"] = raw["meta"]
    return item


class ChatStore:
    """session.py 的薄适配层：模块就绪走新会话，没就绪退回旧账本。

    只调用契约里的函数名和参数（docs/REFACTOR-PLAN.md 第 3 节），
    任何一层失败都往下退，保证网页永远能打开。
    """

    def __init__(self) -> None:
        self.mod = None
        self.broken = ""
        try:
            from . import session as mod
        except Exception as exc:  # noqa: BLE001 - 半成品模块（缺依赖/语法错）也要能降级
            self.broken = f"{type(exc).__name__}: {exc}"
            return
        missing = [name for name in _NEEDED_SESSION_FUNCS if not callable(getattr(mod, name, None))]
        if missing:
            self.broken = "session.py 缺少函数：" + "、".join(missing)
            return
        self.mod = mod

    @property
    def ready(self) -> bool:
        return self.mod is not None

    def _raw_sessions(self) -> list[dict]:
        if self.mod is None:
            return []
        try:
            rows = self.mod.sessions()
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(rows, list):
            return []
        return [_normalize_session(r) for r in rows if isinstance(r, dict)]

    def sessions(self) -> list[dict]:
        rows = self._raw_sessions()
        rows.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
        if rows:
            return rows
        # session.py 就绪时索引为空说明还没建过会话（active() 会建 chat-001），
        # 不要再凭空造一个假会话，否则点了切不过去。
        return [] if self.mod is not None else [_legacy_session()]

    def active(self, session_id: str = "") -> dict:
        rows = self._raw_sessions()
        if session_id:
            for row in rows:
                if row.get("id") == session_id:
                    return row
        if self.mod is not None:
            try:
                current = self.mod.active_session()
            except Exception:  # noqa: BLE001
                current = None
            if isinstance(current, dict) and current.get("id"):
                return _normalize_session(current)
        if rows:
            return rows[0]
        return _legacy_session()

    def create(self, workspace: str, title: str) -> dict:
        if self.mod is None:
            raise RuntimeError("session.py 未就绪")
        created = self.mod.create_session(workspace=workspace or None, title=title or "")
        return _normalize_session(created if isinstance(created, dict) else {})

    def select(self, session_id: str) -> dict:
        if self.mod is None:
            raise RuntimeError("session.py 未就绪")
        current = self.mod.set_active(session_id)
        return _normalize_session(current if isinstance(current, dict) else {})

    def archive(self, session_id: str, archived: bool = True) -> dict:
        """归档 / 恢复（session.py 负责改 archived_at、必要时切 active）。"""
        if self.mod is None:
            raise RuntimeError("会话模块（session.py）还没就绪")
        fn = getattr(self.mod, "archive", None)
        if not callable(fn):
            raise RuntimeError("session.py 版本太旧，没有 archive()，先升级再归档")
        entry = fn(session_id, archived=archived)
        return _normalize_session(entry if isinstance(entry, dict) else {})

    def delete(self, session_id: str) -> dict:
        """删除（消息文件由 session.py 挪进 sessions/trash/）。"""
        if self.mod is None:
            raise RuntimeError("会话模块（session.py）还没就绪")
        fn = getattr(self.mod, "delete", None)
        if not callable(fn):
            raise RuntimeError("session.py 版本太旧，没有 delete()，先升级再删除")
        entry = fn(session_id)
        out = _normalize_session(entry if isinstance(entry, dict) else {})
        if isinstance(entry, dict) and entry.get("trash"):
            out["trash"] = str(entry["trash"])
        return out

    def trash(self) -> list[dict]:
        """回收站里的会话：``delete()`` 挪进 ``sessions/trash/`` 的那些文件。

        读不了就当空列表（回收站坏了不该让整个页面挂掉），字段固定成界面要用的那几个。
        """
        if self.mod is None:
            return []
        fn = getattr(self.mod, "list_trash", None)
        if not callable(fn):
            return []
        try:
            rows = fn()
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(rows, list):
            return []
        out: list[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("trash_name") or "").strip()
            if not name:
                continue
            out.append({
                "trash_name": name,
                "session_id": str(row.get("session_id") or ""),
                "title": str(row.get("title") or "").strip() or "（没有标题的会话）",
                "deleted_at": str(row.get("deleted_at") or ""),
                "message_count": _as_int(row.get("message_count")),
                "workspace": _norm_path(row.get("workspace") or ""),
            })
        return out

    def restore(self, trash_name: str) -> dict:
        """把回收站里的一份会话文件挪回主列表（session.py 负责改文件名/index）。"""
        if self.mod is None:
            raise RuntimeError("会话模块（session.py）还没就绪")
        fn = getattr(self.mod, "restore", None)
        if not callable(fn):
            raise RuntimeError("session.py 版本太旧，没有 restore()，先升级再还原")
        entry = fn(str(trash_name or "").strip())
        out = _normalize_session(entry if isinstance(entry, dict) else {})
        if isinstance(entry, dict) and entry.get("trash_name"):
            out["trash_name"] = str(entry["trash_name"])
        return out

    def empty_trash(self) -> dict:
        """清空回收站：``session.py`` 负责真删文件，这里只把结果收拾成界面要的形状。

        ``session.py`` 版本旧（没有 ``empty_trash()``）时明确报错，而不是假装删了。
        """
        if self.mod is None:
            raise RuntimeError("会话模块（session.py）还没就绪")
        fn = getattr(self.mod, "empty_trash", None)
        if not callable(fn):
            raise RuntimeError("session.py 版本太旧，没有 empty_trash()，先升级再清空")
        result = fn()
        data = result if isinstance(result, dict) else {}
        failed = [str(name) for name in (data.get("failed") or []) if str(name)]
        return {"removed": _as_int(data.get("removed")), "failed": failed}

    def rename(self, session_id: str, title: str) -> dict:
        """改会话标题（改完锁住，之后新增消息不会再被首条消息覆盖）。"""
        if self.mod is None:
            raise RuntimeError("会话模块（session.py）还没就绪")
        fn = getattr(self.mod, "rename", None)
        if not callable(fn):
            raise RuntimeError("session.py 版本太旧，没有 rename()，先升级再改名")
        entry = fn(str(session_id or "").strip(), str(title or "").strip())
        return _normalize_session(entry if isinstance(entry, dict) else {})

    def active_id(self) -> str:
        """归档/删除之后该打开哪个会话：以索引里的 active 为准（不猜、不建会话）。

        一个未归档的都不剩就返回空串 —— 那是「空状态」，别偷偷建 chat-001。
        """
        rows = [r for r in self._raw_sessions()
                if not r.get("archived_at") and not r.get("legacy")]
        if not rows:
            return ""
        fn = getattr(self.mod, "active_session", None)
        if callable(fn):
            try:
                current = fn()          # 有候选就不会新建，只是把索引里的 active 读出来
            except Exception:  # noqa: BLE001
                current = None
            if isinstance(current, dict) and current.get("id"):
                return str(current["id"])
        latest = max(rows, key=lambda r: str(r.get("updated_at") or ""))
        return str(latest.get("id") or "")

    def messages(self, session_id: str, after_id: str = "", limit: int = 200) -> list[dict]:
        if self.mod is not None and session_id:
            try:
                rows = self.mod.messages(session_id, after_id=after_id or None, limit=limit)
            except Exception:  # noqa: BLE001
                rows = None
            if isinstance(rows, list):
                return [_normalize_message(r) for r in rows if isinstance(r, dict)][-limit:]
        rows = _legacy_messages(limit)
        if after_id:
            rows = [m for m in rows if str(m.get("id") or "") > after_id]
        return rows

    def append(
        self, session_id: str, *, role: str, agent: str, content: str,
        task_id: str | None = None, artifacts: list[str] | None = None,
        status: str = "complete", meta: dict | None = None, kind: str = "",
    ) -> dict:
        if self.mod is not None and session_id:
            created = None
            try:
                created = self.mod.append(
                    session_id, role=role, agent=agent, content=content,
                    task_id=task_id, artifacts=artifacts or [], status=status, meta=meta)
            except TypeError:
                # 早期版本 session.append 还没有 meta 参数
                created = self.mod.append(
                    session_id, role=role, agent=agent, content=content,
                    task_id=task_id, artifacts=artifacts or [], status=status)
            if isinstance(created, dict) and created:
                return _normalize_message(created)
            raise RuntimeError("session.append 没有返回消息")
        # 降级：写回旧账本（worker / 旧读取方不用改）
        author = _AGENT_AUTHOR.get(agent, "deepseek")
        if room is not None:
            raw = room.post(author, content, kind or "msg", meta)
        else:
            raw = _append_ledger(author, content, kind or "msg", meta)
        return _legacy_item(raw)


STORE = ChatStore()


def _legacy_session() -> dict:
    messages = _legacy_messages(400)
    project = _project_info()
    return {
        "id": LEGACY_ID,
        "title": "旧账本（兼容视图）",
        "workspace": _norm_path(project.get("workspace") or ""),
        "created_at": messages[0]["created_at"] if messages else "",
        "updated_at": messages[-1]["created_at"] if messages else "",
        "message_count": len(messages),
    }


# ---------------------------------------------------------------- 文件/任务工具

def _project_info() -> dict:
    try:
        data = json.loads(PROJECT_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _project_workspace() -> str:
    return _norm_path(_project_info().get("workspace") or "")


def _typing_raw() -> dict:
    """typing.json 原样（不做新旧过滤）：判断"引擎忙不忙"要看它。"""
    try:
        data = json.loads(TYPING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _typing_info() -> dict:
    data = _typing_raw()
    try:
        if time.time() - float(data.get("at") or 0) > 180:   # 超过 3 分钟别一直转
            return {}
    except (TypeError, ValueError):
        return {}
    return data


def _engine_busy() -> tuple[bool, str]:
    """引擎现在能不能安全重启：有没跑完的任务卡、或正在回复/规划（typing）就不动它。

    换协调者之后要不要重启引擎由这里说了算：忙的时候**只切换不重启**——切换本来
    就下一条消息生效；空闲时才顺手重启，省得用户自己动手。
    """
    try:
        from . import start_all
        cards = start_all.active_cards()
    except Exception:                       # noqa: BLE001 - 判断失败就当空闲，别卡住换模型
        cards = []
    if cards:
        return True, f"还有任务在跑（{'、'.join(cards)}）"
    raw = _typing_raw()
    if raw:
        try:
            age = time.time() - float(raw.get("at") or 0)
        except (TypeError, ValueError):
            age = 0
        if age < 300:
            return True, "引擎正在回复/规划"
    return False, ""


def _session_state_info() -> dict:
    try:
        data = json.loads(SESSION_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _ensure_token() -> str:
    """网页访问令牌：room/web-token 有就复用，没有就生成 32 位 hex 并落盘。

    写成文件是为了让"换个浏览器/重开页面"还能用同一个令牌；删掉文件重启就换新的。
    """
    try:
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) >= 16:
        return existing
    token = secrets.token_hex(16)
    try:
        ROOM.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token, encoding="utf-8")
        try:
            os.chmod(TOKEN_FILE, 0o600)        # 只有自己能读
        except OSError:
            pass
    except OSError:
        pass                                    # 写不进去也别拦着启动，内存里照样有令牌
    return token


def _rotate_code() -> str:
    """换一个新的引导码并落盘，返回它（写不进去也照样返回内存里的这个）。"""
    code = secrets.token_hex(16)
    try:
        CODE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CODE_FILE.write_text(code, encoding="utf-8")
        try:
            os.chmod(CODE_FILE, 0o600)
        except OSError:
            pass
    except OSError:
        pass
    return code


def _ensure_code() -> str:
    """一次性引导码：`/?k=<code>` 换 HttpOnly Cookie，**用掉立刻作废并换新码**。

    为什么不让令牌直接进 URL：链接会留在浏览器历史、被 Referer 带出去、也能被本机
    随便一个进程读命令行。所以对外只发一次性码——被人捡到也只能用一次，用完就换。
    """
    try:
        existing = CODE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        existing = ""
    if len(existing) >= 16:
        return existing
    return _rotate_code()


def _code_matches(supplied: str) -> bool:
    """引导码比较：长度先挡一道，再常量时间比，避免按字符计时的探测。"""
    code = _ensure_code()
    supplied = str(supplied or "").strip()
    if not supplied or len(supplied) != len(code):
        return False
    return secrets.compare_digest(supplied, code)


_ENGINE_IDLE = {"pid": 0, "beat": 0.0, "age": -1.0, "alive": False,
                "stale": True, "status": "none"}

# typing.json 的 who 对应流水线阶段（规划阶段 default who 就是 coordinator）
_PHASE_BY_WHO = {
    "coordinator": "plan",
    "planner": "plan",
    "executor": "execute",
    "tester": "verify",
    "reviewer": "review",
}

_BUSY_IDLE = {"busy": False, "since": "", "request": "", "phase": "",
              "who": "", "task_stale": False}


def _engine_info() -> dict:
    """引擎状态：进程在不在看 alive，心跳旧只算 stalled，绝不是"没在跑"。

    判断交给 procs.lock_state（Windows 上 os.kill(pid, 0) 会真的杀掉进程，
    这种判断一行都不能自己写）。
    """
    if procs is None:
        return dict(_ENGINE_IDLE)
    try:
        state = procs.lock_state(ROOM / "worker.lock")
    except Exception:  # noqa: BLE001 - 锁文件坏了不该让状态接口挂掉
        return dict(_ENGINE_IDLE)
    if not isinstance(state, dict):
        return dict(_ENGINE_IDLE)
    status = str(state.get("status") or "none")
    try:
        age = round(float(state.get("age") if state.get("age") is not None else -1.0), 1)
    except (TypeError, ValueError):
        age = -1.0
    try:
        beat = round(float(state.get("beat") or 0), 3)
    except (TypeError, ValueError):
        beat = 0.0
    return {
        "pid": _as_int(state.get("pid")),
        "beat": beat,
        "age": age,
        "alive": bool(state.get("alive")),
        "stale": bool(state.get("stale")),
        "status": status,
    }


def _busy_info(session: dict, messages: list[dict], task: dict | None) -> dict:
    """"正在规划/还没有任务卡"的占位信息：给右栏用，别让用户觉得界面卡死。"""
    raw = _typing_raw()
    if not raw:
        return dict(_BUSY_IDLE)
    who = str(raw.get("who") or "")
    try:
        at = float(raw.get("at") or 0)
    except (TypeError, ValueError):
        at = 0.0
    since = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(at)) if at else ""

    request_msg = next((m for m in reversed(messages) if m.get("role") == "user"), None)
    if request_msg is None:
        try:
            # 轮询只带了最近一页，需求可能在更早的地方：全量找最后一条 user
            sid = str(session.get("id") or "")
            request_msg = next((m for m in reversed(STORE.messages(sid, "", 0))
                                if m.get("role") == "user"), None)
        except Exception:  # noqa: BLE001 - 读会话失败也不能让状态接口挂掉
            request_msg = None
    request = str((request_msg or {}).get("content") or "").strip()

    task_stale = False
    if task:
        task_stale = bool(task.get("finished_at")) or str(task.get("status") or "") in TERMINAL_STATUS
        if not task_stale and request_msg:
            created = str(task.get("created_at") or "")
            asked = str(request_msg.get("created_at") or "")
            if created and asked and created < asked:
                task_stale = True        # 卡片是上一条需求的，这条还在规划
    return {
        "busy": True,
        "since": since,
        "request": request,
        "phase": _PHASE_BY_WHO.get(who, who),
        "who": who,
        "task_stale": task_stale,
    }


def _page_limit(raw, default: int = PAGE_SIZE) -> int:
    """一页多少条：默认 120，传 0/乱码也用默认，超过 500 一律按 500 算。"""
    limit = _as_int(raw, 0)
    if limit <= 0:
        limit = default
    return min(limit, MAX_PAGE)


def _messages_page(session_id: str, before_id: str, limit: int) -> dict:
    """往前翻一页：before 是客户端当前最老那条的 id；给空就返回最新一页。"""
    limit = max(1, min(limit, MAX_PAGE))
    sid = str(session_id or "") or str(STORE.active().get("id") or "")
    rows: list[dict] = []
    if STORE.ready:
        try:
            rows = STORE.messages(sid, "", 0)      # 翻页是点击行为，全量读一次可接受
        except Exception:  # noqa: BLE001
            rows = []
    if not rows:
        rows = _legacy_messages(0)
    total = len(rows)
    end = total
    if before_id:
        position = next((i for i, row in enumerate(rows) if str(row.get("id")) == str(before_id)), None)
        if position is not None:
            end = position
    start = max(0, end - limit)
    return {
        "ok": True,
        "session_id": sid,
        "messages": rows[start:end],
        "has_more": start > 0,
        "limit": limit,
        "total": total,
    }


def _control_state() -> tuple[dict | None, dict | None]:
    """(暂停标记, 还没被引擎消费的指令)。控制模块不可用时都是 None。"""
    if control is None:
        return None, None
    try:
        paused = control.paused()
    except Exception:  # noqa: BLE001 - 控制文件坏了不该让整个状态接口挂掉
        paused = None
    try:
        pending = control.pending()
    except Exception:  # noqa: BLE001
        pending = None
    return paused, pending


def _tail(path: Path, lines: int = 22) -> str:
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(content[-lines:]).strip()


def _tasks_summary() -> list[dict]:
    out: list[dict] = []
    if not TASKS.is_dir():
        return out
    for directory in TASKS.iterdir():
        card = directory / "task.json"
        if not card.is_file():
            continue
        try:
            task = json.loads(card.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(task, dict):
            continue
        out.append({
            "id": task.get("id", directory.name),
            "title": task.get("title", ""),
            "status": task.get("status", "planned"),
            "rounds": len(task.get("rounds") or []),
            "workspace": str(task.get("workspace") or ""),
            "at": task.get("updated_at", task.get("created_at", "")),
        })
    out.sort(key=lambda t: (str(t["at"]), str(t["id"])), reverse=True)
    return out


_GIT_CACHE: dict[str, tuple[float, dict]] = {}
_GIT_LOCK = threading.Lock()


def _git_snapshot_uncached(workspace: str) -> dict:
    status = _run_git(["status", "--short"], workspace)
    files: list[str] = []
    for line in status.splitlines():
        name = line[3:].strip() if len(line) > 3 else line.strip()
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        name = name.strip().strip('"')
        if name:
            files.append(name)
    stat = _run_git(["diff", "--stat", "HEAD"], workspace)
    if not stat:
        stat = f"{len(files)} 个文件有改动（尚未提交）" if files else "工作区干净（没有未提交改动）"
    return {"files": files[:40], "diff_stat": stat[-1200:]}


def _git_snapshot(workspace: str) -> dict:
    """每 2.5 秒最多跑一次 git，1 秒轮询不至于把机器拖死。"""
    if not workspace:
        return {"files": [], "diff_stat": "还没有绑定工作区"}
    path = Path(workspace)
    try:
        if not path.is_dir():
            return {"files": [], "diff_stat": "工作区路径不存在"}
    except OSError:
        return {"files": [], "diff_stat": "工作区路径不可读"}
    now = time.time()
    with _GIT_LOCK:
        hit = _GIT_CACHE.get(workspace)
    if hit and now - hit[0] < 2.5:
        return hit[1]
    data = _git_snapshot_uncached(workspace)
    with _GIT_LOCK:
        _GIT_CACHE[workspace] = (now, data)
    return data


def _is_product_path(name: str) -> bool:
    """这条改动算不算「产物」。账本目录和缓存垃圾不算（它们不是用户要的东西）。"""
    rel = str(name or "").replace("\\", "/").lstrip("./")
    if not rel:
        return False
    if rel.startswith(("tasks/", "room/", "config/", "agents/", "bin/", ".tmp/", ".codex-home/")):
        return False
    parts = rel.split("/")
    if any(part in ("__pycache__", ".git", "node_modules", ".pytest_cache", ".mypy_cache") for part in parts):
        return False
    return not rel.endswith((".pyc", ".pyo", ".pyd", ".log", ".tmp", ".swp"))


def _task_products(task: dict) -> list[dict]:
    """这张卡在工作区里真正产出的文件：``[{path, ref, in_scope}]``。

    ``ref`` 用 ``workspace:`` 前缀（``_resolve_artifact`` 认这个格式），
    标出是不是落在任务卡声明的 scope 内——越界的只是提醒，不拦。
    """
    workspace = str(task.get("workspace") or _project_workspace() or "")
    if not workspace:
        return []
    scope = [str(item).strip().replace("\\", "/").rstrip("/")
             for item in (task.get("scope") or [])]
    scope = [item for item in scope if item]
    out: list[dict] = []
    seen: set[str] = set()
    for name in _git_snapshot(workspace)["files"]:
        rel = str(name).replace("\\", "/").strip().strip('"')
        if rel in seen or not _is_product_path(rel):
            continue
        seen.add(rel)
        in_scope = (not scope) or any(rel == item or rel.startswith(item + "/") for item in scope)
        out.append({"path": rel, "ref": f"workspace:{rel}", "in_scope": in_scope})
    return out[:40]


def _open_in_system(path: str, action: str) -> tuple[bool, str]:
    """把产物交给系统：``open`` = 用默认程序打开，``reveal`` = 资源管理器里选中它。

    只接 ``_resolve_artifact`` 已经放行的路径（工作区内 / 仓库内的文件），不额外开洞。
    """
    if action == "reveal":
        if os.name != "nt":
            return False, "只有 Windows 上支持在资源管理器里定位"
        try:
            # explorer 一定返回 1，不判退出码；quiet_flags 免得弹黑框
            subprocess.Popen(["explorer", "/select,", path], creationflags=quiet_flags())
            return True, ""
        except OSError as exc:
            return False, f"打不开资源管理器：{exc}"
    try:
        if os.name == "nt":
            os.startfile(path)          # noqa: S606 - 用户主动点的「用默认程序打开」
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path], creationflags=quiet_flags())
        else:
            subprocess.Popen(["xdg-open", path], creationflags=quiet_flags())
        return True, ""
    except OSError as exc:
        return False, f"打不开：{exc}"


def _task_detail(tid: str) -> dict | None:
    tid = (tid or "").strip()
    if not tid or "/" in tid or "\\" in tid or tid in (".", ".."):
        return None
    directory = TASKS / tid
    card = directory / "task.json"
    if not card.is_file():
        return None
    try:
        task = json.loads(card.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(task, dict):
        return None

    def newest(pattern: str) -> Path | None:
        """按修改时间取最新、且非空的文件（按名字排序会被 01 与 01-0 坑到）。"""
        try:
            items = [p for p in directory.glob(pattern) if p.is_file() and p.stat().st_size > 0]
        except OSError:
            return None
        return max(items, key=lambda p: p.stat().st_mtime) if items else None

    verify_file = newest("verify-*.txt")
    review_file = newest("review-*.last.md")
    # 产物（工作区里真生成的文件）排在证据前面：用户找的是产物，不是我们的日志
    products = _task_products(task)
    artifacts = [{"path": p["ref"], "kind": "product", "in_scope": p["in_scope"]} for p in products]
    for pattern, kind in (("plan.md", "plan"), ("exec-*.last.md", "exec"),
                          ("verify-*.txt", "verify"), ("review-*.last.md", "review"),
                          ("report.md", "report")):
        found = newest(pattern)
        if found:
            artifacts.append({"path": f"tasks/{tid}/{found.name}", "kind": kind})

    workspace = str(task.get("workspace") or "")
    detail = {
        "id": task.get("id", tid),
        "title": task.get("title", ""),
        "goal": task.get("goal", ""),
        "status": task.get("status", "planned"),
        "workspace": workspace,
        "created_at": str(task.get("created_at") or ""),
        "updated_at": str(task.get("updated_at") or ""),
        "finished_at": str(task.get("finished_at") or ""),
        "acceptance": task.get("acceptance") or [],
        "rounds": task.get("rounds") or [],
        "reviews": task.get("reviews") or [],
        "verify_tail": _tail(verify_file) if verify_file else "",
        "review_text": _tail(review_file, 26) if review_file else "",
        "artifacts": artifacts,
        "products": products,
        "diff_stat": "",
    }
    detail["diff_stat"] = _git_snapshot(workspace)["diff_stat"]
    detail["changes"] = _changes(detail)
    return detail


def _verify_headline(text: str) -> str:
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    return lines[-1][:200]


def _changes(task: dict | None) -> dict:
    workspace = str((task or {}).get("workspace") or _project_workspace())
    snapshot = _git_snapshot(workspace)
    return {
        "files": snapshot["files"],
        "diff_stat": snapshot["diff_stat"],
        "last_verify": _verify_headline(str((task or {}).get("verify_tail") or "")),
    }


def _latest_task(workspace: str = "") -> dict | None:
    """当前工作区最近的一张任务卡；没绑工作区就不猜，免得把别的项目塞进右栏。"""
    if not TASKS.is_dir() or not workspace:
        return None
    wanted = os.path.normcase(workspace)
    cards: list[tuple[float, str]] = []
    for directory in TASKS.iterdir():
        card = directory / "task.json"
        if not card.is_file():
            continue
        try:
            cards.append((card.stat().st_mtime, directory.name))
        except OSError:
            continue
    cards.sort(reverse=True)
    fallback = None
    for _, name in cards[:20]:
        detail = _task_detail(name)
        if not detail:
            continue
        if os.path.normcase(str(detail.get("workspace") or "")) != wanted:
            continue
        if str(detail.get("status") or "") not in TERMINAL_STATUS:
            return detail
        fallback = fallback or detail
    return fallback


def _current_task(session: dict, messages: list[dict]) -> dict | None:
    for msg in reversed(messages):
        tid = msg.get("task_id")
        if not tid:
            continue
        detail = _task_detail(str(tid))
        if detail:
            return detail
    workspace = str((session or {}).get("workspace") or "") or _project_workspace()
    return _latest_task(workspace)


def _workspaces(sessions: list[dict], extra: str = "") -> list[dict]:
    seen: dict[str, dict] = {}

    def add(raw_path: str) -> None:
        path = _norm_path(raw_path)
        if not path:
            return
        key = os.path.normcase(path) if os.name == "nt" else path
        if key in seen:
            return
        seen[key] = {
            "path": path,
            "name": Path(path).name or path,
            "exists": Path(path).is_dir(),
            "sessions": sum(1 for s in sessions if _norm_path(s.get("workspace") or "") == path),
            "active": False,
        }

    add(extra)
    for session in sessions:
        add(str(session.get("workspace") or ""))
    return list(seen.values())


def _agent_from(raw_agent: str, author: str, role: str) -> str:
    for candidate in (raw_agent, author):
        text = str(candidate or "").strip()
        if not text:
            continue
        if text in AGENT_ROLES or text in ("user", "system"):
            return text
        if text in _LEGACY_AGENT:
            return _LEGACY_AGENT[text]
    if role in ("user", "system"):
        return role
    return "user" if not (raw_agent or author) else "coordinator"


def _agents_state(task: dict | None, typing: dict, messages: list[dict]) -> dict:
    roles = {role: "idle" for role in AGENT_ROLES}
    who = str((typing or {}).get("who") or "").strip()
    if who:
        mapped = _agent_from(who, "", "")
        if mapped in roles:
            roles[mapped] = "working"
    # 旧账本里的事件能看出阶段（session.py 未就绪时主要靠这个）
    started: set[str] = set()
    events = [m for m in messages if m.get("kind") == "event" or m.get("role") == "system"][-40:]
    for msg in events:
        meta = msg.get("meta") or {}
        role = {"dispatch": "executor", "verify": "tester", "review": "reviewer",
                "plan": "planner", "brain": "coordinator"}.get(str(meta.get("phase") or ""))
        if not role:
            continue
        if meta.get("state") == "run":
            started.add(role)
        elif meta.get("state") in ("ok", "bad"):
            started.discard(role)
    for role in started:
        roles[role] = "working"
    legacy = {
        "deepseek": roles["coordinator"],
        "codex": roles["executor"],
        "reviewer": roles["reviewer"],
        "dispatch": roles["executor"],
        "verify": roles["tester"],
        "review": roles["reviewer"],
    }
    return {**roles, **legacy}


def _state_payload(session_id: str = "", after_id: str = "", task_id: str = "",
                   limit=None) -> dict:
    # 一个会话都没有时别在轮询里偷偷建 chat-001：界面要能看到空状态，
    # 真正建会话的时机是用户发出第一条消息（/api/post 会调 STORE.active()）。
    sessions = STORE.sessions()
    if sessions:
        active = STORE.active(session_id)
    else:
        active = {}
    sid = str(active.get("id") or "")
    if sid and not any(s.get("id") == sid for s in sessions):
        sessions = [active, *sessions]
    sessions.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    page_limit = _page_limit(limit)
    messages = STORE.messages(sid, after_id, page_limit) if sid else []
    typing = _typing_info()
    engine = _engine_info()
    task = _task_detail(task_id) if task_id else _current_task(active, messages)
    project = _project_info()
    changes = _changes(task)
    paused, pending = _control_state()
    busy = _busy_info(active, messages, task)
    if not engine.get("alive"):
        busy = dict(_BUSY_IDLE)          # 引擎都没了，别拿残留的 typing.json 骗用户"在规划"
    workspaces = _workspaces(sessions, str(project.get("workspace") or ""))
    current_ws = _project_workspace() or str(active.get("workspace") or "")
    for item in workspaces:
        item["active"] = bool(current_ws) and _norm_path(item["path"]) == _norm_path(current_ws)
    return {
        "ok": True,
        "session": active,
        "sessions": sessions,
        "trash": STORE.trash(),                  # 回收站：左栏底部「回收站 · N」+ 还原面板
        "workspaces": workspaces,
        "messages": messages,
        "messages_limit": page_limit,
        "agents": _agents_state(task, typing, messages),
        "models": _models_snapshot(),          # 每个角色当前用的模型 + 可切列表
        "typing": typing,
        "task": task,
        "changes": changes,
        "paused": paused,                        # 暂停中：{task, stage, reason, since}
        "control": pending,                      # 还没被引擎消费的控制指令
        "engine": engine,                        # alive 才算在跑，心跳旧只是 stalled
        "busy": busy,                            # 规划中占位：{busy, since, request, phase}
        "workspace": current_ws,
        "project": project,                       # 兼容旧前端
        "total_messages": max(_as_int(active.get("message_count"), len(messages)), len(messages)),
        "tasks": _tasks_summary(),
        "now": _now(),
        "root": str(ROOT),
        "store": {"ready": STORE.ready, "note": STORE.broken},
    }


# ---------------------------------------------------------------- artifact 预览

def _resolve_artifact(ref: str) -> tuple[Path | None, str]:
    ref = str(ref or "").strip().strip('"')
    if not ref:
        return None, "缺少文件路径"
    workspace = _project_workspace()
    if ref.startswith("workspace:"):
        rel = ref[len("workspace:"):].lstrip("/\\")
        if not rel:
            return None, "缺少文件路径"
        if not workspace:
            return None, "还没有绑定工作区"
        base = Path(workspace)
        if Path(rel).is_absolute():
            target = Path(rel)
            if not _inside(base, target):
                return None, "路径越界：不在工作区内"
        else:
            ok, why = _scope_ok(base, rel)
            if not ok:
                return None, why or "路径越界：不在工作区内"
            target = base / rel
        if not target.is_file():
            return None, f"文件不存在：{rel}"
        return target, ""

    candidate = Path(ref)
    if candidate.is_absolute():
        allowed = [ROOT_RESOLVED]
        if workspace:
            allowed.append(Path(workspace))
        if not any(_inside(root, candidate) for root in allowed):
            return None, "只能预览协作台目录或工作区内的文件"
        if not candidate.is_file():
            return None, f"文件不存在：{ref}"
        return candidate, ""
    ok, why = _scope_ok(ROOT, ref)
    if not ok:
        return None, why or "路径越界：不在允许范围内"
    target = ROOT / ref
    if not target.is_file():
        return None, f"文件不存在：{ref}"
    return target, ""


def _artifact_payload(ref: str) -> dict:
    target, error = _resolve_artifact(ref)
    if target is None:
        return {"ok": False, "error": error, "path": str(ref or "")}
    try:
        size = target.stat().st_size
        with target.open("r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(MAX_ARTIFACT + 1)
    except OSError as exc:
        return {"ok": False, "error": f"读取失败：{exc}", "path": str(target)}
    truncated = len(content) > MAX_ARTIFACT or size > MAX_ARTIFACT
    return {"ok": True, "path": str(target), "content": content[:MAX_ARTIFACT], "truncated": truncated}


# ---------------------------------------------------------------- 页面

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<link rel="icon" href="data:,">
<title>多 Agent 协作台</title>
<style>/* ============ 设计底座：token（改这里，别在规则里写死） ============ */
:root{
  /* 表面 */
  --bg:#0a0d12; --panel:#0f131a; --panel2:#141922; --panel3:#1a212b;
  --line:#1f2630; --line2:#2a3340;
  /* 文字（全部 ≥4.5:1，最差表面 --panel3）*/
  --fg:#dbe2ea; --dim:#8f9bab; --dim2:#828e9d;
  /* 品牌语义色（语义不动，只提亮不算换色）*/
  --brain:#a371f7; --exec:#3fb950; --review:#d29922; --user:#58a6ff; --sys:#64748b;
  --ok:#3fb950; --bad:#f85149; --warn:#d29922;
  /* 字号阶梯：5 档 */
  --fs-xs:11px; --fs-sm:12px; --fs-md:13px; --fs-lg:15px; --fs-xl:19px;
  /* 间距：4px 栅格（0/4/8/12/16/24/32，1–2px 只给细线）*/
  --sp-0:0; --sp-1:4px; --sp-2:8px; --sp-3:12px; --sp-4:16px; --sp-5:24px; --sp-6:32px;
  /* 圆角：4 档 */
  --r-1:6px; --r-2:10px; --r-3:14px; --r-pill:999px;
  /* 动效：一个时长 + 一条缓动 */
  --dur:140ms; --ease:cubic-bezier(.2,.7,.3,1);
  /* 键盘焦点环 */
  --ring:#58a6ff;
  /* 高科技 HUD：双霓虹（信号场画在内容之上，叠加光，不动面板底色）*/
  --neon-a:#22e0ff; --neon-b:#ff3ea5;
  /* 细线 / 滚动条 */
  --hair:1px; --scroll:#232c38; --scroll-hover:#2f3a49;
  color-scheme:dark;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
html{tab-size:4}
body{background:var(--bg);color:var(--fg);overflow:hidden;
  font:var(--fs-md)/1.6 "Segoe UI","Microsoft YaHei",-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;cursor:pointer}
.mono{font-family:"Cascadia Mono",Consolas,"SF Mono",monospace}
.app{display:none;grid-template-columns:264px minmax(0,1fr) 344px;height:100vh}
body.ready .app{display:grid}

/* ---------- 高科技 HUD：装饰层盒子与开关（画什么由 fx.css 管，这里只给位置和开关） ----------
   信号场画在内容「之上」（z 60，模态在 75+ 所以弹窗是干净的），走叠加光，
   所以面板不用改成半透明——正文对比度维持原样，这点很重要。 */
.fx-layer{position:fixed;inset:0;width:100%;height:100%;display:none;pointer-events:none}
/* fx.css 的 conic 描边是 ::after + inset:0，宿主这边得是定位元素，不然会跑到视口上 */
.composer-box{position:relative}
body.fx-on #fx-field{display:block;z-index:60}
body.fx-on #fx-spark{display:block;z-index:62}
/* 亮的部分只放「没有文字的地方」：视口顶部的扫描线 + 四角括角 + 输入框外圈辉光。
   正文区只留极淡的点阵纹理，这样最暗的辅助文字（--dim2）也还在 4.5:1 以上。 */
.hud-frame{position:fixed;inset:0;z-index:70;pointer-events:none;display:none}
body.fx-on .hud-frame{display:block}
.hud-frame .tl{position:absolute;top:0;left:0;right:0;height:2px;overflow:hidden;
  background:linear-gradient(90deg,transparent,var(--neon-a) 26%,var(--neon-b) 74%,transparent);
  opacity:.55}
.hud-frame .tl i{position:absolute;top:0;left:0;height:2px;width:20%;
  background:linear-gradient(90deg,transparent,#ffffffb8 50%,transparent);
  animation:hud-sweep 7s cubic-bezier(.45,.05,.55,.95) infinite}
@keyframes hud-sweep{0%{transform:translateX(-140%)}60%,100%{transform:translateX(620%)}}
body.fx-max .hud-frame .tl{opacity:.85}
.hud-frame .c{position:absolute;width:20px;height:20px;opacity:.4}
.hud-frame .c::before,.hud-frame .c::after{content:"";position:absolute;background:var(--neon-a)}
.hud-frame .c::before{left:0;top:0;width:20px;height:1px}
.hud-frame .c::after{left:0;top:0;width:1px;height:20px}
.hud-frame .c.tr,.hud-frame .c.br{opacity:.5}
.hud-frame .c.tr::before,.hud-frame .c.tr::after,
.hud-frame .c.br::before,.hud-frame .c.br::after{background:var(--neon-b)}
.hud-frame .c.tr::before,.hud-frame .c.tr::after{left:auto;right:0}
.hud-frame .c.br::before,.hud-frame .c.br::after{left:auto;right:0;top:auto;bottom:0}
.hud-frame .c.bl::before,.hud-frame .c.bl::after{top:auto;bottom:0}
.hud-frame .c.tl{top:5px;left:5px}
.hud-frame .c.tr{top:5px;right:5px}
.hud-frame .c.bl{bottom:5px;left:5px}
.hud-frame .c.br{bottom:5px;right:5px}
/* 输入框外圈：常态一条淡青底光，聚焦时亮一档（都在框外面，不压文字） */
body.fx-on .composer-box::before{content:"";position:absolute;left:12px;right:12px;bottom:-7px;height:1px;
  background:linear-gradient(90deg,transparent,color-mix(in srgb,var(--neon-a) 45%,transparent),transparent);
  opacity:.65;pointer-events:none;transition:opacity var(--dur) var(--ease)}
body.fx-on .composer-box:focus-within::before{
  opacity:1;height:2px;bottom:-8px;
  background:linear-gradient(90deg,transparent,var(--neon-a),var(--neon-b),transparent)}
@media (prefers-reduced-motion:reduce){.hud-frame .tl i{animation:none;display:none}}
/* 面板缝里走一道光：这两条 1px 边线上方没有文字，可以亮 */
body.fx-on .side{border-right-color:color-mix(in srgb,var(--neon-a) 34%,var(--line))}
body.fx-on .inspector{border-left-color:color-mix(in srgb,var(--neon-b) 28%,var(--line))}
body.fx-std .side,body.fx-std .inspector{transition:border-color var(--dur) var(--ease)}
.fx-btn{height:26px;padding:0 var(--sp-2) 0 7px;border-radius:var(--r-pill);
  border:var(--hair) solid var(--line2);background:var(--panel2);color:var(--dim);
  font-size:var(--fs-xs);display:inline-flex;align-items:center;gap:6px;flex:none}
.fx-btn:hover{color:var(--fg);border-color:#3d4a5c}
.fx-btn .fx-led{width:6px;height:6px;border-radius:var(--r-pill);background:var(--dim2);
  transition:background-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease)}
/* 图标（sprite）：一套描边，颜色跟 currentColor 走 */
.ico{width:14px;height:14px;flex:none;fill:none;stroke:currentColor;stroke-width:1.5;
  stroke-linecap:round;stroke-linejoin:round;vertical-align:-2px;overflow:visible}
body.fx-std .fx-btn .fx-led{background:var(--neon-a);box-shadow:0 0 6px var(--neon-a)}
body.fx-max .fx-btn .fx-led{background:var(--neon-b);box-shadow:0 0 7px var(--neon-b)}
@media (prefers-reduced-motion:reduce){.fx-btn .fx-led{transition:none}}

/* ---------- 交互四态（统一：hover / active / focus-visible / disabled） ---------- */
button,.sess,.art,.pick-row,.pick-btn,.use,.cta-btn,.ws-pick,.newchat,.earlier,
.crumbs button{
  transition:background-color var(--dur) var(--ease),border-color var(--dur) var(--ease),
             color var(--dur) var(--ease),opacity var(--dur) var(--ease)}
button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible,
[tabindex]:focus-visible,[role="option"]:focus-visible{
  outline:2px solid var(--ring);outline-offset:2px}
button:active:not(:disabled){filter:brightness(1.15)}
button:disabled{color:var(--dim2);background:var(--panel2);border-color:var(--line);
  cursor:not-allowed;filter:none}
.sess:active,.art:active,.pick-row:active{background:var(--panel3)}

/* ---------- 浏览器表面 ---------- */
::selection{background:#58a6ff52;color:var(--fg);
  background:color-mix(in srgb,var(--user) 32%,transparent)}
::placeholder{color:var(--dim2);opacity:1}
input,textarea{caret-color:var(--user)}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:var(--scroll);border-radius:var(--r-pill);
  border:2px solid var(--panel)}
::-webkit-scrollbar-thumb:hover{background:var(--scroll-hover)}
::-webkit-scrollbar-track{background:transparent}
*{scrollbar-color:var(--scroll) transparent;scrollbar-width:thin}

/* ---------- 左栏 ---------- */
.side{background:var(--panel);border-right:var(--hair) solid var(--line);display:flex;
  flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-4) var(--sp-4) var(--sp-2)}
.brand .mark{width:30px;height:30px;border-radius:var(--r-2);display:grid;place-items:center;
  background:linear-gradient(135deg,#a371f7,#58a6ff);color:#0a0d12;font-weight:800;
  font-size:var(--fs-md)}
.brand .t{font-size:var(--fs-md);font-weight:650;letter-spacing:.2px}
.brand .s{font-size:var(--fs-xs);color:var(--dim2);line-height:1.5}
.newchat{margin:var(--sp-2) var(--sp-3) var(--sp-4);height:34px;border-radius:var(--r-2);
  border:var(--hair) solid var(--line2);background:var(--panel2);display:flex;align-items:center;
  gap:var(--sp-2);padding:0 var(--sp-3);font-size:var(--fs-md)}
.newchat:hover{background:var(--panel3);border-color:#3d4a5c}
.side-label{padding:0 var(--sp-4);font-size:var(--fs-xs);letter-spacing:.09em;
  text-transform:uppercase;color:var(--dim2);margin:var(--sp-0) 0 var(--sp-2);
  display:flex;align-items:center}
.side-label .count{margin-left:auto;letter-spacing:0;color:var(--dim2)}
.ws-card{margin:0 var(--sp-3) var(--sp-4);background:var(--panel2);
  border:var(--hair) solid var(--line);border-radius:var(--r-2);padding:var(--sp-3);
  min-width:0}
.ws-card .wname{font-size:var(--fs-md);font-weight:600;display:flex;align-items:center;
  gap:var(--sp-2);min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ws-card .wpath{font-size:var(--fs-xs);color:var(--dim2);word-break:break-all;
  margin-top:var(--sp-1);font-family:"Cascadia Mono",Consolas,monospace;line-height:1.5}
.ws-card .wmeta{font-size:var(--fs-xs);color:var(--dim);margin-top:var(--sp-2);display:flex;
  gap:var(--sp-3)}
.dot{width:7px;height:7px;border-radius:var(--r-pill);background:var(--dim2);
  display:inline-block;
  flex:0 0 auto}
.dot.on{background:var(--ok);box-shadow:0 0 0 var(--sp-1) #3fb95022}
.sessions{flex:1;min-height:0;overflow-y:auto;padding:0 var(--sp-2) var(--sp-3)}
.sess{position:relative;width:100%;text-align:left;background:transparent;
  border:var(--hair) solid transparent;border-radius:var(--r-2);
  padding:var(--sp-2);display:block;color:inherit;cursor:pointer}
.sess:hover{background:var(--panel2)}
.sess.sel{background:var(--panel3);border-color:var(--line2)}
.sess:focus-visible{outline:2px solid var(--ring);outline-offset:-2px}
.sess-top{display:flex;gap:var(--sp-2);align-items:baseline}
.sess:not(.archived) .sess-top{padding-right:var(--sp-6)}   /* 给「⋯」留位，标题和时间不挪 */
.sess-title{font-size:var(--fs-md);font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;flex:1;min-width:0}
/* 行内改名：输入框跟标题同字号同高，列表不会跳一下 */
.sess-rename{flex:1;min-width:0;height:21px;font-size:var(--fs-md);font-weight:600;
  font-family:inherit;color:var(--fg);background:var(--panel3);
  border:var(--hair) solid var(--ring);border-radius:var(--r-1);
  padding:0 var(--sp-1);line-height:19px}
.sess-time{font-size:var(--fs-xs);color:var(--dim2);flex:0 0 auto}
.sess-sub{font-size:var(--fs-xs);color:var(--dim2);margin-top:var(--sp-1);
  display:flex;align-items:center;gap:var(--sp-2)}
.sess-meta{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
/* 行尾「⋯」：hover / 键盘焦点 / 菜单打开时才出现 */
.sess-more{position:absolute;top:var(--sp-1);right:var(--sp-1);width:22px;height:22px;
  border-radius:var(--r-1);border:var(--hair) solid transparent;background:var(--panel2);
  color:var(--dim);font-size:var(--fs-md);line-height:1;display:grid;place-items:center;
  opacity:0;padding:0}
.sess:hover .sess-more,.sess:focus-within .sess-more,.sess-more:focus-visible,
.sess-more.on{opacity:1}
.sess-more:hover{color:var(--fg);border-color:var(--line2);background:var(--panel3)}
/* 已归档：不整行降透明度（那会把文字对比度一起拉低，实测 3.49:1），
   只把标题/副行换成暗一档的文字色（6.60:1 / 5.59:1）+ 小标 */
.sess.archived .sess-title{color:var(--dim)}
.arch-tag{margin-left:var(--sp-1);font-size:var(--fs-xs);color:var(--dim2);
  border:var(--hair) solid var(--line2);border-radius:var(--r-1);padding:0 var(--sp-1);
  flex:0 0 auto}
.sess-restore{flex:0 0 auto;margin-left:auto;height:20px;
  border-radius:var(--r-1);border:var(--hair) solid var(--line2);background:var(--panel2);
  color:var(--dim);font-size:var(--fs-xs);padding:0 var(--sp-2)}
.sess-restore:hover{color:var(--fg);border-color:#3d4a5c}
/* 已归档分组：可折叠 */
.arch-group{margin-top:var(--sp-2);border-top:var(--hair) solid var(--line);
  padding-top:var(--sp-2)}
.arch-head{width:100%;display:flex;align-items:center;gap:var(--sp-1);background:none;
  border:0;color:var(--dim2);font-size:var(--fs-xs);letter-spacing:.06em;
  text-transform:uppercase;padding:var(--sp-1) var(--sp-2);border-radius:var(--r-1)}
.arch-head:hover{background:var(--panel2);color:var(--dim)}
.arch-head .chev{font-size:var(--fs-xs);transition:transform var(--dur) var(--ease)}
.arch-group.open .arch-head .chev{transform:rotate(90deg)}
.arch-list{display:none;margin-top:var(--sp-1)}
.arch-group.open .arch-list{display:block}
/* 会话菜单（跟着行出现的浮层） */
.sess-menu{position:fixed;z-index:78;min-width:132px;background:var(--panel2);
  border:var(--hair) solid var(--line2);border-radius:var(--r-2);
  box-shadow:0 var(--sp-2) 24px #00000080;padding:var(--sp-1)}
.sess-menu[hidden]{display:none}
.sess-menu .mi{display:flex;align-items:center;gap:var(--sp-1);width:100%;
  background:none;border:0;border-radius:var(--r-1);color:var(--fg);
  font-size:var(--fs-sm);padding:var(--sp-1) var(--sp-2);text-align:left}
.sess-menu .mi:hover,.sess-menu .mi.cur{background:var(--panel3)}
.sess-menu .mi.danger{color:var(--bad)}
.sess-menu .mi.danger:hover{background:#f8514914;color:#ffb4ad}
.menu-sep{height:var(--hair);background:var(--line2);margin:var(--sp-1) 0}
/* 产物右键菜单：跟会话菜单同一套观感，只是挂在文件名上 */
.art-menu{position:fixed;z-index:88;min-width:186px;background:var(--panel2);
  border:var(--hair) solid var(--line2);border-radius:var(--r-2);
  box-shadow:0 var(--sp-2) 24px #00000080;padding:var(--sp-1)}
.art-menu[hidden]{display:none}
.art-menu .mi{display:flex;align-items:center;gap:var(--sp-1);width:100%;
  background:none;border:0;border-radius:var(--r-1);color:var(--fg);
  font-size:var(--fs-sm);padding:var(--sp-1) var(--sp-2);text-align:left}
.art-menu .mi:hover{background:var(--panel3)}
.art-menu .mi[disabled]{opacity:.45}
/* 右栏 Agent 行里的模型徽章：小、可点、不喧宾夺主 */
.amrow{display:flex;align-items:center;gap:var(--sp-1);margin-top:2px}
.agent-row .am{background:none;border:var(--hair) solid var(--line2);border-radius:999px;
  color:var(--dim);font-size:var(--fs-xs);padding:0 var(--sp-1);line-height:1.6;
  max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.agent-row .am:hover{color:var(--fg);border-color:var(--accent);background:var(--panel3)}
.agent-row .am.fixed{border-style:dashed;cursor:default}
.agent-row .am.fixed:hover{border-color:var(--line2);background:none;color:var(--dim)}
.agent-row .ams{color:var(--dim2);font-size:var(--fs-xs)}
/* 换模型菜单：跟产物菜单同一套观感，多一行"当前"勾选 */
.model-menu{position:fixed;z-index:89;min-width:210px;max-height:320px;overflow:auto;
  background:var(--panel2);border:var(--hair) solid var(--line2);border-radius:var(--r-2);
  box-shadow:0 var(--sp-2) 24px #00000080;padding:var(--sp-1)}
.model-menu[hidden]{display:none}
.model-menu .mi{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-2);
  width:100%;background:none;border:0;border-radius:var(--r-1);color:var(--fg);
  font-size:var(--fs-sm);padding:var(--sp-1) var(--sp-2);text-align:left}
.model-menu .mi:hover{background:var(--panel3)}
.model-menu .mi[disabled]{opacity:.4;cursor:not-allowed}
.model-menu .mi .tag{color:var(--dim2);font-size:var(--fs-xs)}
.model-menu .mi.on{color:var(--accent)}
.model-menu .mh{padding:var(--sp-1) var(--sp-2);color:var(--dim2);font-size:var(--fs-xs)}
.model-menu.wide{width:min(420px,calc(100vw - 16px));max-height:min(560px,calc(100vh - 16px))}
.model-menu .mono{font-family:ui-monospace,Consolas,monospace;word-break:break-all}
.model-menu .mi .tag.warn,.model-menu .p.warn{color:var(--warn)}
.model-menu .p.dim,.model-menu .pick.dim{color:var(--dim2)}
.model-menu .mi.back{color:var(--dim)}
.model-menu .setup{display:flex;flex-direction:column;gap:var(--sp-1);padding:0 var(--sp-2) var(--sp-2);
  font-size:var(--fs-sm)}
.model-menu .setup code{font-family:ui-monospace,Consolas,monospace;background:var(--panel3);
  padding:0 4px;border-radius:var(--r-1)}
.model-menu .ask{white-space:pre-wrap;margin:0;padding:var(--sp-1) var(--sp-2);background:var(--panel3);
  border-radius:var(--r-1);font-size:var(--fs-xs);color:var(--dim)}
.model-menu textarea{width:100%;box-sizing:border-box;resize:vertical;background:var(--bg);color:var(--fg);
  border:var(--hair) solid var(--line2);border-radius:var(--r-1);padding:var(--sp-1);
  font:var(--fs-xs) ui-monospace,Consolas,monospace}
.model-menu textarea:focus-visible{outline:none;box-shadow:var(--ring)}
.model-menu .setup input,.model-menu .setup select{width:100%;box-sizing:border-box;background:var(--bg);
  color:var(--fg);border:var(--hair) solid var(--line2);border-radius:var(--r-1);
  padding:var(--sp-1) var(--sp-2);font:inherit;font-size:var(--fs-sm)}
.model-menu .setup input:focus-visible,.model-menu .setup select:focus-visible{outline:none;box-shadow:var(--ring)}
.model-menu .setup label.step{display:flex;flex-direction:column;gap:var(--sp-1)}
.model-menu .setup .row{display:flex;gap:var(--sp-1);flex-wrap:wrap}
.model-menu .pick{display:flex;align-items:flex-start;gap:var(--sp-1);font-size:var(--fs-xs);line-height:1.5}
/* 空状态 */
.sess-empty{padding:var(--sp-3) var(--sp-2);line-height:1.8}
.side-foot{margin-top:auto;padding:var(--sp-2) var(--sp-3);
  border-top:var(--hair) solid var(--line);font-size:var(--fs-xs);color:var(--dim2);
  line-height:1.5}
/* 左栏底部「回收站 · N」：删掉的会话从这里捡回来 */
.side-trash{display:flex;align-items:center;gap:var(--sp-2);width:100%;
  background:none;border:0;border-top:var(--hair) solid var(--line);
  padding:var(--sp-2) var(--sp-4);color:var(--dim2);font-size:var(--fs-sm);
  text-align:left;flex:0 0 auto}
.side-trash .ic{display:flex;align-items:center;opacity:.8;line-height:1}
.side-trash .ic .ico{width:13px;height:13px}
.side-trash .lbl{flex:1;min-width:0}
.side-trash .count{flex:0 0 auto;min-width:20px;text-align:center;font-size:var(--fs-xs);
  color:var(--dim2);border:var(--hair) solid var(--line2);border-radius:var(--r-pill);
  padding:0 var(--sp-2)}
.side-trash:hover{background:var(--panel2);color:var(--fg)}
.side-trash:hover .count{color:var(--dim);border-color:#3d4a5c}
.side-trash.has .count{color:var(--fg);border-color:#3d4a5c}

/* ---------- 中栏 ---------- */
.main{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--bg)}
.topbar{flex:0 0 auto;border-bottom:var(--hair) solid var(--line);
  padding:var(--sp-3) var(--sp-5) var(--sp-2);
  background:linear-gradient(180deg,#0e131b,#0b0f15)}
.topbar-row{display:flex;gap:var(--sp-2);align-items:center;max-width:860px;margin:0 auto}
.topbar-title{margin-bottom:var(--sp-2);gap:var(--sp-3)}
.sess-name{font-size:var(--fs-md);font-weight:650;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;min-width:0;flex:1}
.live{display:inline-flex;align-items:center;gap:var(--sp-2);height:22px;padding:0 var(--sp-2);
  border-radius:var(--r-pill);border:var(--hair) solid var(--line2);background:var(--panel2);
  font-size:var(--fs-xs);color:var(--dim);flex:0 0 auto}
.live .dot{width:6px;height:6px}
.live.busy{color:#9be3b1;border-color:#3fb95055;background:#3fb95014}
.live.busy .dot{background:var(--ok);animation:pulse 1.5s infinite;
  box-shadow:0 0 0 var(--sp-1) #3fb95022}
@keyframes pulse{50%{opacity:.4}}
.ws-input{flex:1;height:32px;min-width:0;border-radius:var(--r-2);
  border:var(--hair) solid var(--line2);background:var(--panel);color:var(--fg);
  padding:0 var(--sp-3);font:inherit;font-size:var(--fs-md);outline:none}
.ws-input:focus{border-color:var(--ring)}
.btn-primary{height:32px;border-radius:var(--r-2);border:var(--hair) solid #2e5a86;
  background:#16283c;color:#cfe4ff;padding:0 var(--sp-3);font-size:var(--fs-md);flex:0 0 auto}
.btn-primary:hover:not(:disabled){background:#1b3550}
.btn-primary:disabled{background:var(--panel2);border-color:var(--line2);color:var(--dim2)}
.ws-status{max-width:860px;margin:var(--sp-1) auto 0;font-size:var(--fs-sm);color:var(--dim2);
  min-height:17px;word-break:break-all}
.ws-status.ok{color:var(--ok)}
.ws-status.bad{color:var(--bad)}
.ws-card .ws-pick{margin-top:var(--sp-2);width:100%;height:28px;border-radius:var(--r-1);
  border:var(--hair) solid #2e5a86;background:#16283c;color:#cfe4ff;font-size:var(--fs-sm)}
.ws-card .ws-pick:hover{background:#1b3550}
.insp-ws{margin-bottom:var(--sp-3);padding:var(--sp-2);border:var(--hair) solid var(--line);
  border-radius:var(--r-2);background:var(--panel2)}
.insp-ws .p{font-family:"Cascadia Mono",Consolas,monospace;font-size:var(--fs-xs);
  color:var(--dim);word-break:break-all;line-height:1.5}
.insp-ws .pick-btn{margin-top:var(--sp-2)}
/* 「清单外」提醒：只提醒，不拦（用户要的是标出来，不是拒绝干活） */
.insp-warn{margin:0 0 var(--sp-2);padding:var(--sp-2);border-radius:var(--r-1);
  border:var(--hair) solid #d2992255;background:#d2992212;
  font-size:var(--fs-xs);line-height:1.6;color:#e5c07b;word-break:break-all}
.stream{flex:1;min-height:0;overflow-y:auto;
  padding:var(--sp-5) var(--sp-5) var(--sp-1)}
.msg{display:flex;gap:var(--sp-3);max-width:860px;margin:0 auto var(--sp-4);width:100%}
/* 交接：新消息到达时按发言人颜色扫一次、头像外扩一圈。只在特效开着时演，减少动态时不演 */
body.fx-on .msg.arrive .bubble{position:relative;overflow:hidden}
body.fx-on .msg.arrive .bubble::after{content:"";position:absolute;inset:0;pointer-events:none;
  background:linear-gradient(100deg,transparent 20%,color-mix(in srgb,var(--who) 26%,transparent) 50%,transparent 80%);
  transform:translateX(-110%);animation:msg-sweep 900ms var(--ease) 1 forwards}
body.fx-on .msg.arrive .av{animation:av-ring 900ms var(--ease) 1}
@keyframes msg-sweep{to{transform:translateX(110%)}}
@keyframes av-ring{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--who) 70%,transparent)}
  100%{box-shadow:0 0 0 10px transparent}}
@media (prefers-reduced-motion:reduce){body.fx-on .msg.arrive .bubble::after{display:none}
  body.fx-on .msg.arrive .av{animation:none}}
.msg .av{width:28px;height:28px;border-radius:var(--r-2);display:grid;place-items:center;
  font-weight:700;font-size:var(--fs-md);color:#06090f;flex:0 0 auto;margin-top:var(--sp-0)}
.msg .body{min-width:0;max-width:min(680px,84%)}
.msg .meta{display:flex;align-items:center;gap:var(--sp-2);font-size:var(--fs-sm);
  color:var(--dim2);margin-bottom:var(--sp-1);flex-wrap:wrap}
.msg .meta .who{font-weight:650}
.msg .meta .role{font-size:var(--fs-xs);padding:0 var(--sp-1);border-radius:var(--r-1);
  border:var(--hair) solid var(--line2);color:var(--dim2);white-space:nowrap}
.msg .bubble{background:var(--panel2);border:var(--hair) solid var(--line);
  border-radius:var(--r-3);padding:var(--sp-3) var(--sp-4);font-size:var(--fs-md);
  line-height:1.7;white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.msg.user{flex-direction:row-reverse}
.msg.user .body{max-width:min(640px,82%)}
.msg.user .meta{justify-content:flex-end}
.msg.user .bubble{background:#14263c;border-color:#26456b;border-radius:var(--r-3);
  color:#e6f0fb}
/* 气泡「尾巴」：贴着头像那一角收成小圆角（其余三只角仍是 14px），指回发送者 */
.msg:not(.user) .bubble{border-top-left-radius:var(--r-1)}
.msg.user .bubble{border-top-right-radius:var(--r-1)}
.msg.failed .bubble{border-color:#f8514966;background:#1e1214}
.msg.streaming .bubble{opacity:.9}
.msg .bubble pre.code{background:#0a0f16;border:var(--hair) solid var(--line);
  border-radius:var(--r-2);padding:var(--sp-2);margin:var(--sp-2) 0;
  font-family:"Cascadia Mono",Consolas,monospace;font-size:var(--fs-sm);line-height:1.6;
  overflow-x:auto;white-space:pre;color:#c2d1e0}
.msg .bubble code{font-family:"Cascadia Mono",Consolas,monospace;font-size:var(--fs-md);
  background:#0a0f16;border:var(--hair) solid var(--line);border-radius:var(--r-1);
  padding:0 var(--sp-1)}
.fail{display:flex;gap:var(--sp-2);align-items:flex-start;margin-top:var(--sp-2);
  font-size:var(--fs-sm);color:#ff9d95;background:#2a1214;border:var(--hair) solid #f8514944;
  border-radius:var(--r-2);padding:var(--sp-2) var(--sp-2);word-break:break-word}
.notice{max-width:860px;margin:0 auto var(--sp-4);display:flex;align-items:center;
  gap:var(--sp-3);color:var(--dim2);font-size:var(--fs-sm)}
.notice:before,.notice:after{content:"";height:var(--hair);background:var(--line);flex:1}
.notice span{max-width:72%;text-align:center;white-space:pre-wrap;word-break:break-word}
.artifacts{display:flex;flex-wrap:wrap;gap:var(--sp-2);margin-top:var(--sp-2)}
/* 产物 = 蓝色的文件名（像 Codex 那样）：单击打开、右击出菜单。
   不再做成灰盒子按钮——盒子里看不出哪个是产物、哪个是日志。 */
.art{border:0;background:none;border-radius:var(--r-1);
  padding:0 var(--sp-1) 0 0;font-size:var(--fs-sm);color:#6cb6ff;
  display:inline-flex;gap:5px;align-items:center;max-width:100%;cursor:pointer}
.art:hover{color:#a5d6ff;text-decoration:underline;text-underline-offset:2px}
.art:focus-visible{outline:2px solid var(--ring);outline-offset:1px}
.art .ico{color:inherit;opacity:.9;flex:0 0 auto}
.art .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}
/* 清单外（任务没声明要改的）产物：换成警示色 + 小标签，提醒但不禁掉 */
.art[data-scope="out"]{color:var(--warn)}
.art[data-scope="out"]:hover{color:#ffd479}
.art .tag{flex:0 0 auto;font-size:10px;padding:0 4px;border-radius:var(--r-pill);
  background:#d2992222;color:var(--warn);line-height:15px}
.typing{max-width:860px;margin:0 auto var(--sp-4);display:flex;align-items:center;
  gap:var(--sp-2);color:#8aa0b8;font-size:var(--fs-sm)}
.typing .dots{display:inline-flex;gap:var(--sp-1)}
.typing .dots i{width:5px;height:5px;border-radius:var(--r-pill);background:var(--brain);
  animation:blink 1.1s infinite ease-in-out}
.typing .dots i:nth-child(2){animation-delay:.18s}
.typing .dots i:nth-child(3){animation-delay:.36s}
@keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}
  40%{opacity:1;transform:translateY(-2px)}}
.welcome{max-width:620px;margin:var(--sp-6) auto 0;text-align:center;color:var(--dim)}
.welcome-cta{margin-top:var(--sp-4)}
.welcome-cta:empty{display:none}
.welcome-cta .cta-btn{height:34px;border-radius:var(--r-2);border:var(--hair) solid #2e5a86;
  background:#16283c;color:#cfe4ff;font-size:var(--fs-md);padding:0 var(--sp-4)}
.welcome-cta .cta-btn:hover{background:#1b3550}
.welcome-cta .cta-hint{display:block;margin-top:var(--sp-2);font-size:var(--fs-xs);
  color:var(--dim2)}
.welcome h1{font-size:var(--fs-xl);font-weight:650;color:var(--fg);
  margin-bottom:var(--sp-3)}
.welcome p{font-size:var(--fs-md);line-height:1.8;color:var(--dim2)}
.composer{flex:0 0 auto;padding:var(--sp-3) var(--sp-5) var(--sp-4);
  border-top:var(--hair) solid var(--line);background:var(--panel)}
.composer-box{max-width:860px;margin:0 auto;display:flex;gap:var(--sp-2);align-items:flex-end;
  background:var(--bg);border:var(--hair) solid var(--line2);border-radius:var(--r-3);
  padding:var(--sp-2) var(--sp-2) var(--sp-2) var(--sp-3);transition:border-color var(--dur) var(--ease)}
.composer-box:focus-within{border-color:#3d5a7d}
#input{flex:1;resize:none;border:0;outline:none;background:transparent;color:var(--fg);
  font:inherit;max-height:150px;min-height:24px;padding:var(--sp-1) 0}
.send{width:32px;height:32px;border-radius:var(--r-2);border:0;background:var(--user);
  color:#06111f;display:grid;place-items:center;flex:0 0 auto}
.send:hover:not(:disabled){filter:brightness(1.1)}
.send:disabled{background:var(--panel3);color:var(--dim2)}
.composer-hint{max-width:860px;margin:var(--sp-2) auto 0;display:flex;gap:var(--sp-3);
  align-items:center;font-size:var(--fs-xs);color:var(--dim2);min-height:16px}
.composer-hint kbd{border:var(--hair) solid var(--line2);border-radius:var(--r-1);
  padding:0 var(--sp-1);font-size:var(--fs-xs);color:var(--dim)}
.composer-hint .ws{margin-left:auto;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;max-width:48%}

/* ---------- 右栏 ---------- */
.inspector{background:var(--panel);border-left:var(--hair) solid var(--line);
  min-height:0;display:flex;flex-direction:column}
.insp{flex:1;overflow-y:auto;padding:var(--sp-4) var(--sp-4) var(--sp-5)}
.panel{margin-bottom:var(--sp-4)}
.panel h3{font-size:var(--fs-xs);letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim2);display:flex;align-items:center;gap:var(--sp-2);
  margin-bottom:var(--sp-2)}
.panel h3 .n{margin-left:auto;letter-spacing:0;text-transform:none;color:var(--dim2);
  font-size:var(--fs-xs);font-weight:400}
.agent-row{display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-2);
  border-radius:var(--r-2)}
.agent-row:hover{background:var(--panel2)}
.agent-row .dot{width:7px;height:7px}
.agent-row.busy .dot{background:var(--ok);box-shadow:0 0 0 var(--sp-1) #3fb9501f;
  animation:pulse 1.5s infinite}
.agent-row .an{font-size:var(--fs-md);font-weight:600}
.agent-row .ar{font-size:var(--fs-xs);color:var(--dim2)}
.agent-row .as{margin-left:auto;font-size:var(--fs-xs);color:var(--dim)}
.agent-row.busy .as{color:var(--ok)}
.task-card{border:var(--hair) solid var(--line);background:var(--panel2);
  border-radius:var(--r-2);padding:var(--sp-3)}
.task-card .tid{font-size:var(--fs-xs);color:var(--dim2);
  font-family:"Cascadia Mono",Consolas,monospace}
.task-card .ttitle{font-size:var(--fs-md);font-weight:600;margin:var(--sp-1) 0 var(--sp-2);
  line-height:1.5;word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:var(--sp-1);align-items:center}
.chip{font-size:var(--fs-xs);padding:var(--sp-0) var(--sp-2);border-radius:var(--r-pill);
  border:var(--hair) solid var(--line2);color:var(--dim);line-height:1.6;white-space:nowrap}
.chip.passed{color:var(--ok);border-color:#3fb95055;background:#3fb95014}
.chip.failed,.chip.exec-failed,.chip.stopped{color:var(--bad);border-color:#f8514955;
  background:#f8514914}
.chip.planned,.chip.executed,.chip.executing{color:var(--user);border-color:#58a6ff55;
  background:#58a6ff14}
.kv{display:grid;grid-template-columns:62px 1fr;gap:var(--sp-1) var(--sp-2);
  font-size:var(--fs-sm);margin-top:var(--sp-2)}
.kv .k{color:var(--dim2)}
.kv .v{color:var(--dim);word-break:break-all}
pre.block{background:var(--bg);border:var(--hair) solid var(--line);border-radius:var(--r-2);
  padding:var(--sp-2);font-family:"Cascadia Mono",Consolas,monospace;font-size:var(--fs-sm);
  line-height:1.6;color:#9fb0c3;max-height:190px;overflow:auto;white-space:pre-wrap;
  word-break:break-word}
ul.files{list-style:none;display:flex;flex-direction:column;gap:var(--sp-1);
  margin-bottom:var(--sp-2)}
ul.files li{font-size:var(--fs-sm);color:var(--dim);padding-left:var(--sp-3);
  position:relative;word-break:break-all}
ul.files li:before{content:"•";position:absolute;left:var(--sp-0);color:var(--dim2)}
.empty{color:var(--dim2);font-size:var(--fs-sm);text-align:center;
  padding:var(--sp-4) var(--sp-2);line-height:1.8}

/* ---------- 控制按钮（暂停/继续/停止/重新执行） ---------- */
.ctrl{display:grid;grid-template-columns:1fr 1fr;gap:var(--sp-2);margin-top:var(--sp-2)}
.ctrl button{height:27px;border-radius:var(--r-2);border:var(--hair) solid var(--line2);
  background:var(--panel);color:var(--dim);font-size:var(--fs-sm);padding:0 var(--sp-2);
  white-space:nowrap}
.ctrl button:hover:not(:disabled){color:var(--fg);border-color:#3d4a5c}
.ctrl button:disabled{background:var(--panel);border-color:var(--line);color:var(--dim2)}
.ctrl button.hl{color:#cfe4ff;border-color:#2e5a86;background:#16283c}
.ctrl button.hl:hover:not(:disabled){background:#1b3550;border-color:#3d6f9e}
.ctrl button.danger{color:#ffb4ad;border-color:#f8514955;background:#f8514910}
.ctrl button.danger:hover:not(:disabled){color:#ffd7d3;border-color:#f8514999}
.ctrl-hint{margin-top:var(--sp-2);font-size:var(--fs-xs);color:var(--dim2);line-height:1.5}
.paused-banner{margin-top:var(--sp-2);font-size:var(--fs-sm);color:#f3d08a;background:#2a2213;
  border:var(--hair) solid #d2992255;border-radius:var(--r-2);padding:var(--sp-2);
  line-height:1.5;word-break:break-word}
.paused-banner b{color:#ffd27a;font-weight:600}
.live.paused{color:#f3d08a;border-color:#d2992255;background:#d2992214}
.live.paused .dot{background:var(--warn);box-shadow:0 0 0 var(--sp-1) #d2992222}
/* 引擎三态：活着就是绿/黄，只有进程真没了才灰 */
.live.engine-running{color:#9be3b1;border-color:#3fb95055;background:#3fb95014}
.live.engine-running .dot{background:var(--ok);box-shadow:0 0 0 var(--sp-1) #3fb95022}
.live.engine-stalled{color:#f3d08a;border-color:#d2992255;background:#d2992214}
.live.engine-stalled .dot{background:var(--warn);box-shadow:0 0 0 var(--sp-1) #d2992222}
.live.engine-dead{color:var(--dim);border-color:var(--line2);background:var(--panel2)}
.live.engine-dead .dot{background:#6b7787}
.topbar-title .live{margin-left:auto}
.earlier{position:sticky;top:0;z-index:2;display:block;margin:0 auto var(--sp-4);height:27px;
  padding:0 var(--sp-3);border-radius:var(--r-pill);border:var(--hair) solid var(--line2);
  background:var(--panel2);color:var(--dim);font-size:var(--fs-sm)}
.earlier:hover:not(:disabled){color:var(--fg);border-color:#3d4a5c}
.earlier:disabled{color:var(--dim2);background:var(--panel2);border-color:var(--line)}
.earlier[hidden]{display:none}
.gap-note{position:sticky;top:0;z-index:3;display:flex;align-items:center;gap:var(--sp-2);
  margin:0 auto var(--sp-3);padding:var(--sp-2) var(--sp-3);border-radius:var(--r-2);
  border:var(--hair) solid #d2992255;background:#2a2213;color:#f3d08a;
  font-size:var(--fs-sm);line-height:1.5;max-width:640px;flex-wrap:wrap}
.gap-note[hidden]{display:none}
.gap-note button{height:22px;border-radius:var(--r-1);border:var(--hair) solid #d2992266;
  background:#241d10;color:#ffd27a;font-size:var(--fs-xs);padding:0 var(--sp-2)}
.gap-note button:hover:not(:disabled){border-color:#d29922aa;color:#ffe1a3}
.gap-note button:disabled{color:#c9ac74;background:#241d10;border-color:#d2992244}
.gap-note .muted{color:#c2a873}
.task-card.pending{border-color:#d2992244;background:#1b1a14}
.task-card.pending .ttitle{color:#f0d9a8}

/* ---------- 访问令牌 ---------- */
.unlock{position:fixed;inset:0;display:grid;place-items:center;padding:var(--sp-6);z-index:70;
  background:radial-gradient(1100px 560px at 50% -10%,#141b27,#0a0d12 72%)}
.unlock[hidden]{display:none}
.unlock-card{width:min(430px,100%);background:var(--panel);border:var(--hair) solid var(--line2);
  border-radius:var(--r-3);padding:var(--sp-5) var(--sp-5) var(--sp-4);
  box-shadow:0 var(--sp-5) 60px #00000066}
.unlock-mark{width:34px;height:34px;border-radius:var(--r-2);display:grid;place-items:center;
  background:linear-gradient(135deg,#a371f7,#58a6ff);color:#0a0d12;font-weight:800;
  font-size:var(--fs-lg);margin-bottom:var(--sp-3)}
.unlock-card h2{font-size:var(--fs-lg);font-weight:650;margin-bottom:var(--sp-2)}
.unlock-card p{font-size:var(--fs-sm);color:var(--dim2);line-height:1.8;
  margin-bottom:var(--sp-3)}
.unlock-card input{width:100%;height:34px;border-radius:var(--r-2);
  border:var(--hair) solid var(--line2);background:var(--bg);color:var(--fg);
  padding:0 var(--sp-3);font-size:var(--fs-md);outline:none;
  font-family:"Cascadia Mono",Consolas,monospace}
.unlock-card input:focus{border-color:var(--ring)}
.unlock-card button{width:100%;height:34px;margin-top:var(--sp-2);border-radius:var(--r-2);
  border:var(--hair) solid #2e5a86;background:#16283c;color:#cfe4ff;font-size:var(--fs-md)}
.unlock-card button:hover:not(:disabled){background:#1b3550}
.unlock-err{margin-top:var(--sp-2);font-size:var(--fs-sm);color:var(--bad);
  min-height:16px;line-height:1.5}

/* ---------- 弹层 ---------- */
.modal{position:fixed;inset:0;background:#05070ac7;display:grid;place-items:center;
  padding:var(--sp-6);z-index:50}
.modal[hidden]{display:none}
.modal-card{width:min(940px,100%);max-height:100%;display:flex;flex-direction:column;
  background:var(--panel);border:var(--hair) solid var(--line2);border-radius:var(--r-3);
  overflow:hidden;box-shadow:0 var(--sp-5) 60px #00000066}
.modal-head{display:flex;align-items:center;gap:var(--sp-3);padding:var(--sp-2) var(--sp-3);
  border-bottom:var(--hair) solid var(--line);background:var(--panel2)}
.modal-head .p{font-family:"Cascadia Mono",Consolas,monospace;font-size:var(--fs-sm);
  color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.modal-head button{height:26px;border-radius:var(--r-1);border:var(--hair) solid var(--line2);
  background:var(--panel);padding:0 var(--sp-2);font-size:var(--fs-sm);color:var(--dim)}
.modal-head button:hover{color:var(--fg)}
.modal-body{flex:1;overflow:auto;padding:var(--sp-3);margin:0;
  font-family:"Cascadia Mono",Consolas,monospace;font-size:var(--fs-sm);line-height:1.6;
  color:#c2d1e0;white-space:pre-wrap;word-break:break-word;background:var(--bg)}
.toast{position:fixed;left:50%;bottom:80px;transform:translateX(-50%);background:#2a1214;
  border:var(--hair) solid #f8514966;color:#ffb4ad;font-size:var(--fs-sm);
  padding:var(--sp-2) var(--sp-3);border-radius:var(--r-2);z-index:60;opacity:0;
  transition:opacity var(--dur) var(--ease);pointer-events:none}
.toast.show{opacity:1}

/* ---------- 选工作区：目录索引 + 体检 + 确认 ---------- */
.picker{position:fixed;inset:0;z-index:75;display:flex;align-items:center;justify-content:center;
  padding:var(--sp-5);background:#05070ac7}
.picker[hidden]{display:none}
.picker-card{width:min(720px,100%);max-height:min(84vh,660px);display:flex;
  flex-direction:column;background:var(--panel);border:var(--hair) solid var(--line2);
  border-radius:var(--r-3);overflow:hidden;box-shadow:0 var(--sp-5) 70px #00000080}
.picker-head{display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-3) var(--sp-3);
  border-bottom:var(--hair) solid var(--line);background:var(--panel2)}
.picker-head .t{font-size:var(--fs-md);font-weight:650}
.picker-head .s{font-size:var(--fs-xs);color:var(--dim2)}
.picker-head .x{margin-left:auto;height:26px;width:28px;border-radius:var(--r-1);
  border:var(--hair) solid var(--line2);background:var(--panel);color:var(--dim);
  font-size:var(--fs-lg);line-height:1}
.picker-head .x:hover{color:var(--fg)}
.picker-bar{display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-2) var(--sp-3);
  border-bottom:var(--hair) solid var(--line);background:var(--panel2)}
.pick-btn{height:26px;border-radius:var(--r-1);border:var(--hair) solid var(--line2);
  background:var(--panel);color:var(--dim);font-size:var(--fs-sm);padding:0 var(--sp-2);
  flex:0 0 auto}
.pick-btn:hover:not(:disabled){color:var(--fg);border-color:#3d4a5c}
.pick-btn:disabled{background:var(--panel2);border-color:var(--line);color:var(--dim2)}
.topbar-row .pick-btn{height:32px;border-radius:var(--r-2);padding:0 var(--sp-3);
  font-size:var(--fs-md)}
.crumbs{display:flex;align-items:center;gap:var(--sp-0);flex:1;min-width:0;overflow-x:auto;
  white-space:nowrap;font-size:var(--fs-sm);color:var(--dim2);padding-bottom:var(--sp-0)}
.crumbs button{background:none;border:0;color:var(--dim);font:inherit;padding:var(--sp-1);
  border-radius:var(--r-1);flex:0 0 auto}
.crumbs button:hover{color:var(--fg);background:var(--panel3)}
.crumbs button:last-child{color:var(--fg)}
.crumbs .sep{color:var(--dim2);flex:0 0 auto;font-size:var(--fs-xs)}
.pick-drives{display:flex;gap:var(--sp-2);flex-wrap:wrap;padding:0 var(--sp-3) var(--sp-2);
  border-bottom:var(--hair) solid var(--line);background:var(--panel2)}
.pick-drives:empty{display:none}
.pick-drives .pick-btn.on{color:#cfe4ff;border-color:#2e5a86;background:#16283c}
.pick-list{flex:1;min-height:140px;overflow-y:auto;padding:var(--sp-1);contain:content}
.pick-row{display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-2);
  border-radius:var(--r-2);cursor:pointer;border:var(--hair) solid transparent;outline:none}
.pick-row:hover{background:var(--panel2)}
.pick-row.cur{background:var(--panel3);border-color:var(--line2)}
.pick-row:focus-visible{outline:2px solid var(--ring);outline-offset:-2px}
.pick-row.noise .nm{color:var(--dim)}
.pick-row.noise .hh,.pick-row.noise .ic{color:var(--dim2)}
.pick-row .ic{width:15px;text-align:center;color:var(--dim2);flex:0 0 auto;
  font-size:var(--fs-md)}
.pick-row .nm{font-size:var(--fs-md);flex:1;min-width:0;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
.pick-row .hh{font-size:var(--fs-xs);color:var(--dim2);flex:0 0 auto;max-width:48%;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pick-row .use{height:21px;border-radius:var(--r-1);border:var(--hair) solid var(--line2);
  background:var(--panel);color:var(--dim);font-size:var(--fs-xs);padding:0 var(--sp-2);
  flex:0 0 auto;opacity:0;transition:opacity var(--dur) var(--ease),
  color var(--dur) var(--ease),border-color var(--dur) var(--ease)}
.pick-row:hover .use,.pick-row:focus-visible .use,.pick-row:focus-within .use,
.pick-row.cur .use{opacity:1}
.pick-row .use:hover{color:var(--fg);border-color:#3d4a5c}
/* 协作台自己的目录：能进去看，但「选它」是禁的（后端也会拒，这里提前挡住白点一次） */
.pick-row.blocked{opacity:.62;cursor:default}
.pick-row.blocked:hover{background:transparent}
.pick-row.blocked .nm{color:var(--dim)}
.pick-row.blocked .hh{color:var(--warn)}
.pick-row.blocked .use{opacity:1;color:var(--dim2);cursor:not-allowed}
.pick-empty{padding:var(--sp-5) var(--sp-4);text-align:center;font-size:var(--fs-md);
  color:var(--dim2);line-height:1.8}
.pick-more{padding:var(--sp-2);text-align:center;font-size:var(--fs-xs);color:var(--dim2)}
.pick-foot{display:flex;align-items:center;gap:var(--sp-3);padding:var(--sp-3);
  border-top:var(--hair) solid var(--line);background:var(--panel2)}
.pick-selinfo{flex:1;min-width:0}
.pick-path{font-family:"Cascadia Mono",Consolas,monospace;font-size:var(--fs-sm);
  color:var(--fg);word-break:break-all;line-height:1.5}
.pick-health{font-size:var(--fs-sm);color:var(--dim2);margin-top:var(--sp-1);line-height:1.5}
.pick-health.ok{color:var(--ok)}
.pick-health.bad{color:var(--bad)}
.pick-health.warn{color:var(--warn)}
.pick-go{height:34px;font-size:var(--fs-md);padding:0 var(--sp-3)}
body.picking .main,body.picking .side,body.picking .inspector{pointer-events:none}

/* ---------- 删除确认框 ---------- */
/* 层级：确认框是「二级弹窗」，必须压在所有面板之上（回收站 81 / 选择器 75 / 菜单 78）。
   80 那一档以前是够的，后来回收站挪到 81，确认框就钻到遮罩下面去了——点「清空」看不到它。 */
.confirm{position:fixed;inset:0;z-index:90;display:flex;align-items:center;
  justify-content:center;padding:var(--sp-4);background:#05070ac7}
.confirm[hidden]{display:none}
.confirm-card{width:min(440px,100%);background:var(--panel);border:var(--hair) solid var(--line2);
  border-radius:var(--r-3);padding:var(--sp-4);box-shadow:0 var(--sp-5) 60px #00000080}
.confirm-title{font-size:var(--fs-lg);font-weight:650;margin-bottom:var(--sp-2)}
.confirm-line{font-size:var(--fs-sm);color:var(--dim);line-height:1.6;
  word-break:break-word;margin-bottom:var(--sp-1)}
.confirm-line b{color:var(--fg);font-weight:600}
.confirm-warn{margin:var(--sp-2) 0 var(--sp-4);font-size:var(--fs-sm);color:#f3d08a;
  background:#2a2213;border:var(--hair) solid #d2992255;border-radius:var(--r-2);
  padding:var(--sp-2) var(--sp-3);line-height:1.6}
.confirm-actions{display:flex;gap:var(--sp-2);justify-content:flex-end}
.confirm-actions button{height:32px;border-radius:var(--r-2);font-size:var(--fs-md);
  padding:0 var(--sp-4);border:var(--hair) solid var(--line2);background:var(--panel2);
  color:var(--fg)}
.confirm-actions button:hover{background:var(--panel3)}
.btn-danger{border-color:#f8514966;background:#f8514922;color:#ffb4ad}
.btn-danger:hover{background:#f8514933;border-color:#f8514999;color:#ffd7d3}

/* ---------- 回收站（删掉的会话还能捡回来） ---------- */
.trash{position:fixed;inset:0;z-index:81;display:flex;align-items:center;
  justify-content:center;padding:var(--sp-4);background:#05070ac7}
.trash[hidden]{display:none}
.trash-card{width:min(560px,100%);max-height:min(76vh,620px);display:flex;
  flex-direction:column;background:var(--panel);border:var(--hair) solid var(--line2);
  border-radius:var(--r-3);overflow:hidden;box-shadow:0 var(--sp-5) 60px #00000080}
.trash-head{display:flex;align-items:flex-start;gap:var(--sp-2);
  padding:var(--sp-4) var(--sp-4) var(--sp-3);
  border-bottom:var(--hair) solid var(--line);background:var(--panel2)}
.trash-hd{flex:1;min-width:0}
.trash-head .t{font-size:var(--fs-lg);font-weight:650}
.trash-head .s{font-size:var(--fs-xs);color:var(--dim2);margin-top:var(--sp-1);
  line-height:1.5}
.trash-head .x{flex:0 0 auto;height:26px;width:28px;border-radius:var(--r-1);
  border:var(--hair) solid var(--line2);background:var(--panel);color:var(--dim);
  font-size:var(--fs-lg);line-height:1}
.trash-head .x:hover{color:var(--fg)}
.trash-list{flex:1;min-height:0;overflow-y:auto;padding:var(--sp-2) var(--sp-2) var(--sp-3)}
.trash-row{display:flex;align-items:center;gap:var(--sp-2);padding:var(--sp-2);
  border:var(--hair) solid transparent;border-radius:var(--r-2)}
.trash-row:hover{background:var(--panel2);border-color:var(--line)}
.trash-row .info{flex:1;min-width:0}
.trash-row .ttl{font-size:var(--fs-md);font-weight:600;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.trash-row .sub{font-size:var(--fs-xs);color:var(--dim2);margin-top:var(--sp-0);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.trash-row .back{flex:0 0 auto;height:24px;border-radius:var(--r-1);
  border:var(--hair) solid #2e5a86;background:#16283c;color:#cfe4ff;
  font-size:var(--fs-sm);padding:0 var(--sp-3)}
.trash-row .back:hover{background:#1b3550}
.trash-empty{padding:var(--sp-6) var(--sp-4);text-align:center;color:var(--dim2);
  font-size:var(--fs-sm);line-height:1.9}
/* 卡片底部：左边说清后果，右边才是动作（默认禁用，免得空回收站还让人点） */
.trash-foot{display:flex;align-items:center;gap:var(--sp-3);flex:0 0 auto;
  padding:var(--sp-3) var(--sp-4);border-top:var(--hair) solid var(--line);
  background:var(--panel2)}
.trash-foot .hint{flex:1;min-width:0;font-size:var(--fs-xs);color:var(--dim2);
  line-height:1.5}
.trash-foot button{flex:0 0 auto;height:28px;padding:0 var(--sp-3);border-radius:var(--r-1);
  font-size:var(--fs-sm)}
.trash-foot button[disabled]{opacity:.45}

/* ---------- 响应式 ---------- */
/* 1180：三栏收窄，次要统计先让位（结构不动） */
@media (max-width:1180px){
  .app{grid-template-columns:224px minmax(0,1fr) 296px}
  .topbar{padding:var(--sp-3) var(--sp-4) var(--sp-2)}
  .stream{padding:var(--sp-4) var(--sp-4) var(--sp-1)}
  .composer{padding:var(--sp-3) var(--sp-4) var(--sp-4)}
  .insp{padding:var(--sp-3) var(--sp-3) var(--sp-5)}
  .panel h3 .n{display:none}
  .msg .body{max-width:min(640px,92%)}
  .kv{grid-template-columns:56px 1fr}
  .composer-hint .ws{max-width:38%}
}
/* 620：只剩中栏（DOM 不变，两栏隐藏），输入框/按钮不溢出 */
@media (max-width:620px){
  .app{grid-template-columns:minmax(0,1fr)}
  .side,.inspector{display:none}
  .topbar{padding:var(--sp-2) var(--sp-2)}
  .stream{padding:var(--sp-3) var(--sp-2) var(--sp-1)}
  .composer{padding:var(--sp-2) var(--sp-2) var(--sp-2)}
  .topbar-row{flex-wrap:wrap}
  .ws-input{flex:1 1 160px}
  .topbar-row .pick-btn,.topbar-row .btn-primary{flex:0 0 auto}
  .msg{gap:var(--sp-2)}
  .msg .body,.msg.user .body{max-width:100%}
  .msg .meta{font-size:var(--fs-xs)}
  .composer-hint{gap:var(--sp-2);flex-wrap:wrap}
  .composer-hint .ws{max-width:100%;margin-left:0}
  .welcome{margin-top:var(--sp-4)}
  .welcome h1{font-size:var(--fs-lg)}
  .picker{padding:var(--sp-2)}
  .picker-card{max-height:92vh}
  .picker-head .s{display:none}
  .pick-row .hh{display:none}
  .pick-foot{gap:var(--sp-2);padding:var(--sp-2)}
  .pick-go{padding:0 var(--sp-2);font-size:var(--fs-sm)}
  .unlock{padding:var(--sp-4)}
  .modal{padding:var(--sp-2)}
  .confirm{padding:var(--sp-3)}
  .confirm-actions{flex-wrap:wrap}
  .confirm-actions button{flex:1 1 100%}
}

/* ---------- 减少动效 ---------- */
@media (prefers-reduced-motion:reduce){
  *,*::before,*::after{animation-duration:.001ms!important;
    animation-iteration-count:1!important;transition-duration:.001ms!important;
    scroll-behavior:auto!important}
  .typing .dots i{animation:none;opacity:.6;transform:none}
  .live.busy .dot,.agent-row.busy .dot{animation:none;box-shadow:none}
  .toast{transition:none}
}/* __FX_CSS_BEGIN__ */
/* ======== HUD 特效层（源文件 .tmp/fx/fx.css，改完用 .tmp/fx/splice.py 重新拼） ======== */
/* ============================================================================
   fx.css — 战术 HUD 特效层（零依赖：纯 CSS + 两个 canvas，由 fx.js 驱动）
   ----------------------------------------------------------------------------
   契约
   - 全部特效挂在 body.fx-on 下；档位 body.fx-std（默认）/ body.fx-max；
     body.fx-off 或不带 fx-on 时：装饰层不生成、动画全停。
   - 装饰层一律 pointer-events:none；z-index 由宿主参数决定：
     #fx-field 用 --fx-z（默认 0），#fx-spark 用 --fx-z-spark（默认 90）。
   - 颜色只走宿主 token；新增色只有 --neon-a / --neon-b 两个，别处全用 var()。
   - prefers-reduced-motion:reduce → 所有循环动画（ping / conic 旋转）停掉，
     静态描边和静态点阵保留；点击反馈退化成纯 opacity。
   ========================================================================== */

/* 只在这里新增两个颜色，命名固定 */
:root{
  --neon-a:#22e0ff;   /* 青 */
  --neon-b:#ff3ea5;   /* 品红 */

  /* 装饰层 z-index（宿主可覆盖；fx.js 的 init 还会写内联 style 覆盖这里） */
  --fx-z:60;
  --fx-z-spark:62;
}

/* 可动画的 conic 角度（Chromium 支持 @property，注册后才能补间） */
@property --fx-spin { syntax: "<angle>"; inherits: false; initial-value: 0deg; }


/* ============================ 1. 画布层 ================================== */

#fx-field,
#fx-spark{
  position:fixed;
  inset:0;
  display:block;
  width:100%;
  height:100%;
  pointer-events:none;
  border:0;
  margin:0;
  padding:0;
  background:transparent;
  /* 双缓冲外的合成提示：固定层，避免宿主滚动时重排 */
  contain:strict;
}
#fx-field{ z-index:var(--fx-z); }
#fx-spark{ z-index:var(--fx-z-spark); }

/* 不带 fx-on / 显式 off：画布彻底不参与渲染（fx.js 也会 display:none） */
body:not(.fx-on) #fx-field,
body:not(.fx-on) #fx-spark,
body.fx-off #fx-field,
body.fx-off #fx-spark{
  display:none !important;
}


/* ============================ 2. 状态心跳 ping ============================ */
/* 只对 .is-hot 生效：.live .dot / #engine .dot 都走这条。1.8s 一圈。 */

body.fx-on .is-hot .dot{ position:relative; }

body.fx-on .is-hot .dot::after{
  content:"";
  position:absolute;
  inset:0;
  border-radius:inherit;
  border:1px solid color-mix(in srgb, var(--neon-a) 62%, transparent);
  pointer-events:none;
  transform-origin:center;
  animation:fx-ping 1.8s cubic-bezier(.2,.7,.3,1) infinite;
  will-change:transform,opacity;
}

@keyframes fx-ping{
  0%  { transform:scale(1);   opacity:.85; }
  68% { transform:scale(2.8); opacity:0; }
  100%{ transform:scale(2.8); opacity:0; }
}


/* ====================== 3. 状态边框：旋转 conic 描边 ====================== */
/* 1px 内描边，青↔品红顺时针 4s 一圈。用 padding+mask 抠出环，不用 filter。 */

body.fx-on :is(.sess.sel, .composer-box:focus-within, .toast.show,
                .trash-card, .picker-card, .confirm-card)::after{
  content:"";
  position:absolute;
  inset:0;
  z-index:2;
  padding:1px;
  border-radius:inherit;
  pointer-events:none;
  background:conic-gradient(from var(--fx-spin),
      transparent 0deg 296deg,
      color-mix(in srgb, var(--neon-a) 78%, transparent) 326deg,
      color-mix(in srgb, var(--neon-b) 84%, transparent) 350deg,
      transparent 360deg);
  -webkit-mask:
      linear-gradient(#000 0 0) content-box,
      linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;
          mask:linear-gradient(#000 0 0) content-box, linear-gradient(#000 0 0);
          mask-composite:exclude;
  animation:fx-ring 4s linear infinite;
}

@keyframes fx-ring{
  to{ --fx-spin:360deg; }
}

/* max 档：环更亮一点（不加模糊，避免掉帧） */
body.fx-max :is(.sess.sel, .composer-box:focus-within, .toast.show,
                .trash-card, .picker-card, .confirm-card)::after{
  background:conic-gradient(from var(--fx-spin),
      transparent 0deg 288deg,
      color-mix(in srgb, var(--neon-a) 92%, transparent) 322deg,
      color-mix(in srgb, var(--neon-b) 96%, transparent) 350deg,
      transparent 360deg);
}


/* ========================= 4. 四角 HUD 括角 =============================== */
/* 12px 折线：左上 + 右下 青；右上 + 左下 品红。只给 .panel 和三种卡片。 */

body.fx-on :is(.panel, .trash-card, .picker-card, .confirm-card){
  position:relative;
}

/* conic 描边要贴着自身盒子画，所以这行必须有（宿主原本没给 composer-box 设 position） */
body.fx-on .composer-box{
  position:relative;
}

body.fx-on :is(.panel, .trash-card, .picker-card, .confirm-card)::before{
  content:"";
  position:absolute;
  inset:0;
  z-index:3;
  border-radius:inherit;
  pointer-events:none;
  opacity:.30;
  background:
    linear-gradient(var(--neon-a), var(--neon-a)) 0 0 / 12px 1px no-repeat,
    linear-gradient(var(--neon-a), var(--neon-a)) 0 0 / 1px 12px no-repeat,
    linear-gradient(var(--neon-b), var(--neon-b)) 100% 100% / 12px 1px no-repeat,
    linear-gradient(var(--neon-b), var(--neon-b)) 100% 100% / 1px 12px no-repeat,
    linear-gradient(var(--neon-b), var(--neon-b)) 100% 0 / 12px 1px no-repeat,
    linear-gradient(var(--neon-b), var(--neon-b)) 100% 0 / 1px 12px no-repeat,
    linear-gradient(var(--neon-a), var(--neon-a)) 0 100% / 12px 1px no-repeat,
    linear-gradient(var(--neon-a), var(--neon-a)) 0 100% / 1px 12px no-repeat;
}

body.fx-max :is(.panel, .trash-card, .picker-card, .confirm-card)::before{
  opacity:.42;
}


/* ==================== 5. 点击反馈（reduced-motion 用） ==================== */
/* 常规档走 canvas 迸溅（fx.js）；reduced-motion 下 fx.js 只加这个类。 */

body.fx-on .fx-tap{
  opacity:.66;
  transition:opacity 110ms linear;
}


/* ============================ 6. 静止 / 降级 ============================= */

/* reduced-motion：循环动画全停，留下不动的静态描边 */
@media (prefers-reduced-motion: reduce){
  body.fx-on .is-hot .dot::after{
    animation:none;
    transform:scale(1.35);
    opacity:.45;
  }
  body.fx-on :is(.sess.sel, .composer-box:focus-within, .toast.show,
                  .trash-card, .picker-card, .confirm-card)::after{
    animation:none;
  }
  body.fx-on .fx-tap{
    opacity:.66;
    transition:opacity 110ms linear;
  }
}

/* 用户要求减少透明度/对比度等极端环境：不额外加戏，保持静止即可 */
@media (forced-colors: active){
  body.fx-on .is-hot .dot::after,
  body.fx-on :is(.sess.sel, .composer-box:focus-within, .toast.show,
                  .trash-card, .picker-card, .confirm-card)::after,
  body.fx-on :is(.panel, .trash-card, .picker-card, .confirm-card)::before{
    content:none;
  }
}
/* __FX_CSS_END__ */
</style></head>
<body>
<canvas class="fx-layer" id="fx-field" aria-hidden="true"></canvas>
<canvas class="fx-layer" id="fx-spark" aria-hidden="true"></canvas>
<!-- 图标：自绘 1.5px 描边一套，别再用 emoji 当图标 -->
<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false"><defs>
  <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M9.5 7V4.5h5V7M6.5 7l1 13h9l1-13M10 11v5.5M14 11v5.5"/></symbol>
  <symbol id="i-file" viewBox="0 0 24 24"><path d="M7 3.5h7l4 4v13H7z"/><path d="M14 3.5v4h4"/></symbol>
  <symbol id="i-folder" viewBox="0 0 24 24"><path d="M3.5 7.5a2 2 0 0 1 2-2h3.6l2 2h7.4a2 2 0 0 1 2 2v7.5a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2z"/></symbol>
  <symbol id="i-pkg" viewBox="0 0 24 24"><path d="M12 3.2l8 4.4v8.8l-8 4.4-8-4.4V7.6z"/><path d="M4 7.6l8 4.4 8-4.4M12 12v8.8"/></symbol>
</defs></svg>
<!-- HUD 边框层：顶部扫描线 + 四角括角（只走 CSS，不压任何文字） -->
<div class="hud-frame" aria-hidden="true">
  <div class="tl"><i></i></div>
  <span class="c tl"></span><span class="c tr"></span>
  <span class="c bl"></span><span class="c br"></span>
</div>
<div class="app">
  <aside class="side">
    <div class="brand">
      <span class="mark">协</span>
      <div><div class="t">多 Agent 协作台</div><div class="s">聊天式工作台</div></div>
    </div>
    <button class="newchat" id="new-chat"><span>＋</span> 新建会话</button>
    <div class="side-label">当前工作区</div>
    <div class="ws-card" id="ws-card"></div>
    <div class="side-label">历史会话<span class="count" id="session-count">0</span></div>
    <div class="sessions" id="sessions"></div>
    <button class="side-trash" id="trash-entry" aria-haspopup="dialog" title="看看删掉的会话">
      <span class="ic"><svg class="ico" aria-hidden="true"><use href="#i-trash"/></svg></span><span class="lbl">回收站</span>
      <span class="count" id="trash-count">0</span>
    </button>
    <div class="side-foot" id="store-note"></div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div class="topbar-row topbar-title">
        <div class="sess-name" id="crumb">新会话</div>
        <div class="live" id="live"><span class="dot"></span><span id="live-txt">空闲</span></div>
        <div class="live engine-dead" id="engine" title="">
          <span class="dot"></span><span id="engine-txt">引擎没在跑</span></div>
        <button class="fx-btn" id="fx-toggle" type="button" aria-pressed="true"
                title="高科技特效：关 / 标准 / 加强（点一下换一档）">
          <span class="fx-led"></span><span class="fx-lbl">特效·标准</span>
        </button>
      </div>
      <div class="topbar-row">
        <input class="ws-input" id="ws-input" spellcheck="false"
               placeholder="工作区绝对路径，例如 E:\my-project（回车验证并绑定）">
        <button class="btn-primary" id="ws-btn">验证并绑定</button>
        <button class="pick-btn" id="ws-browse" title="浏览目录、看体检结果再确认">浏览…</button>
      </div>
      <div class="ws-status" id="ws-status"></div>
    </div>

    <div class="stream" id="stream">
      <div class="welcome" id="welcome">
        <h1>把需求说成一句话就行</h1>
        <p>协调者理解意图、规划者拆步骤、执行者写代码、验收员跑测试、复核员审查。<br>
           这里只显示对话和结果，任务卡与验收证据在右边。</p>
        <div class="welcome-cta" id="welcome-cta"></div>
      </div>
    </div>

    <div class="composer">
      <div class="composer-box">
        <textarea id="input" rows="1" placeholder="说一句话就行：给登录页加记住我 / 测试挂了帮我修"></textarea>
        <button class="send" id="send" title="发送（Enter）">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor"
               stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4z"/></svg>
        </button>
      </div>
      <div class="composer-hint">
        <span><kbd>Enter</kbd> 发送</span>
        <span><kbd>Shift</kbd>+<kbd>Enter</kbd> 换行</span>
        <span class="ws" id="composer-ws"></span>
      </div>
    </div>
  </main>

  <aside class="inspector"><div class="insp" id="insp"></div></aside>
</div>

<div class="modal" id="modal" hidden>
  <div class="modal-card">
    <div class="modal-head">
      <span class="p" id="modal-path"></span>
      <button id="modal-close">关闭</button>
    </div>
    <pre class="modal-body" id="modal-body"></pre>
  </div>
</div>
<div class="toast" id="toast"></div>

<div class="sess-menu" id="sess-menu" role="menu" aria-label="会话操作" hidden>
  <button class="mi" id="menu-rename" role="menuitem" tabindex="-1">重命名</button>
  <button class="mi" id="menu-archive" role="menuitem" tabindex="-1">归档</button>
  <div class="menu-sep"></div>
  <button class="mi danger" id="menu-delete" role="menuitem" tabindex="-1">删除</button>
</div>
<div class="art-menu" id="art-menu" role="menu" aria-label="产物操作" hidden>
  <button class="mi" data-act="preview" role="menuitem" tabindex="-1">打开（预览）</button>
  <button class="mi" data-act="open" role="menuitem" tabindex="-1">用默认程序打开</button>
  <button class="mi" data-act="reveal" role="menuitem" tabindex="-1">在资源管理器里显示</button>
  <div class="menu-sep"></div>
  <button class="mi" data-act="copy" role="menuitem" tabindex="-1">复制完整路径</button>
</div>
<div class="model-menu" id="model-menu" role="menu" aria-label="换模型" hidden></div>

<div class="confirm" id="confirm" hidden role="dialog" aria-modal="true"
     aria-labelledby="confirm-title">
  <div class="confirm-card">
    <div class="confirm-title" id="confirm-title">删除会话</div>
    <div class="confirm-line" id="confirm-target"></div>
    <div class="confirm-warn" id="confirm-warn">删除后这个会话不再出现在列表里，但会留在左栏底部的
      「回收站」里，随时能还原</div>
    <div class="confirm-actions">
      <button id="confirm-cancel">取消</button>
      <button class="btn-danger" id="confirm-ok">删除</button>
    </div>
  </div>
</div>

<div class="trash" id="trash" hidden role="dialog" aria-modal="true" aria-labelledby="trash-title">
  <div class="trash-card">
    <div class="trash-head">
      <div class="trash-hd">
        <div class="t" id="trash-title">回收站</div>
        <div class="s">删掉的会话留在这儿，还原后回到左栏列表</div>
      </div>
      <button class="x" id="trash-close" title="关闭（Esc）">×</button>
    </div>
    <div class="trash-list" id="trash-list"></div>
    <div class="trash-foot">
      <span class="hint" id="trash-hint">「还原」能捡回来；「清空」是彻底删除，找不回来</span>
      <button class="btn-danger" id="trash-empty" disabled>清空回收站</button>
    </div>
  </div>
</div>

<div class="picker" id="picker" hidden role="dialog" aria-modal="true" aria-labelledby="picker-title">
  <div class="picker-card">
    <div class="picker-head">
      <span class="t" id="picker-title">选择工作区</span>
      <span class="s">点目录进去 · 「选它」只选中不进入 · Esc 关闭</span>
      <button class="x" id="picker-close" title="关闭（Esc）">×</button>
    </div>
    <div class="picker-bar">
      <button class="pick-btn" id="picker-up" title="上一级（Backspace）">↑ 上一级</button>
      <div class="crumbs" id="pick-crumbs"></div>
    </div>
    <div class="pick-drives" id="pick-drives"></div>
    <div class="pick-list" id="pick-list" role="listbox" aria-label="目录列表" tabindex="-1"></div>
    <div class="pick-foot">
      <div class="pick-selinfo">
        <div class="pick-path" id="pick-path"></div>
        <div class="pick-health" id="pick-health"></div>
      </div>
      <button class="btn-primary pick-go" id="pick-ok" disabled>确认使用这个工作区</button>
    </div>
  </div>
</div>

<div class="unlock" id="unlock" hidden>
  <div class="unlock-card">
    <div class="unlock-mark">协</div>
    <h2>需要访问令牌</h2>
    <p>协作台只给本机用。把 <span class="mono">room/web-token</span> 里的内容粘贴进来，
      或者用启动时打印的那个一次性链接打开一次（打开后浏览器会记住，链接本身不再需要）。</p>
    <input id="unlock-input" spellcheck="false" autocomplete="off" placeholder="粘贴访问令牌">
    <button id="unlock-btn">进入工作台</button>
    <div class="unlock-err" id="unlock-err"></div>
  </div>
</div>

<script>
// __FX_JS_BEGIN__
/* ======== HUD 特效层（源文件 .tmp/fx/fx.js） ======== */
/* ============================================================================
   fx.js — 战术 HUD 特效层（零依赖 vanilla，只有 canvas 2D + CSS）
   ----------------------------------------------------------------------------
   机制来源（react-bits 原组件都是 React/WebGL，这里只复刻观感，不搬代码）：
     环境场       ← Backgrounds/Particles + Backgrounds/DotGrid + Backgrounds/Scanner
     点击迸溅     ← Animations/ClickSpark
     文字解码     ← TextAnimations/DecryptedText + ScrambledText
     底部透视网格 ← Backgrounds/GridScan
   详见同目录 README.md。

   API（签名固定，别改）：
     FX.init({ level:'std'|'max'|'off', zField:0, zSpark:90, getState:()=>state })
     FX.setLevel('off'|'std'|'max')
     FX.scramble(el, text)
     FX.spark(x, y, opts)
     FX.destroy()
   附加的只读/辅助出口（自测用，宿主可以不用）：
     FX.status() / FX.setState({busy,engineAlive})

   性能约束（自查过）：单 rAF 循环画两个 canvas；30fps 上限；dpr≤2；
   document.hidden 停；粒子上限 std 28 / max 64；单帧全部绘制 <2ms 量级。
   ========================================================================== */
(function () {
  'use strict';

  if (window.FX && window.FX.__fx) return;

  /* ------------------------------- 常量 ---------------------------------- */
  var TAU = Math.PI * 2;
  var FRAME_MS = 1000 / 30;      // canvas 帧间隔（30fps 上限）
  var DPR_CAP = 2;
  var POLL_MS = 600;             // getState 轮询
  var SPARK_LIFE = 300;          // 迸溅 300ms 淡出
  var SPARK_CAP = 160;
  var MAX_LINK_ALPHA = 0.10;     // 粒子/连线 alpha 上限

  var LEVELS = {
    off: null,
    std: { gap: 32, particles: 28, link: 104, spark: 8,  perspective: false },
    max: { gap: 24, particles: 64, link: 116, spark: 14, perspective: true  }
  };

  /* ------------------------------- 状态 ---------------------------------- */
  var inited = false;
  var level = 'std';
  var cfg = LEVELS.std;
  var opts = { getState: null, zField: 0, zSpark: 90 };
  var st = { busy: false, engineAlive: true };
  var reduced = false;

  var dom = { field: null, spark: null, fctx: null, sctx: null, made: false };
  var view = { w: 0, h: 0, dpr: 1 };
  var colors = { a: [34, 224, 255], b: [255, 62, 165], dim: [130, 142, 157] };
  var patCache = {};             // 'gap|alive|dpr' -> CanvasPattern
  var particles = [];
  var sparks = [];
  var parallax = { x: 0, y: 0, tx: 0, ty: 0 };
  var loop = { on: false, raf: 0, prev: 0, frames: 0, drawn: 0 };
  var sparkDirty = false;
  var pollId = 0;
  var resizeId = 0;
  var listeners = [];
  var scrambleTimers = [];

  /* ------------------------------- 小工具 -------------------------------- */
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }

  function rgba(c, a) {
    return 'rgba(' + c[0] + ',' + c[1] + ',' + c[2] + ',' + (Math.round(a * 1000) / 1000) + ')';
  }

  function mixRgb(p, q, t) {
    return [
      Math.round(p[0] + (q[0] - p[0]) * t),
      Math.round(p[1] + (q[1] - p[1]) * t),
      Math.round(p[2] + (q[2] - p[2]) * t)
    ];
  }

  function readCss(name, fallback) {
    try {
      var v = getComputedStyle(document.documentElement).getPropertyValue(name);
      v = (v || '').trim();
      return v || fallback;
    } catch (e) {
      return fallback;
    }
  }

  function toRgb(str) {
    var s = String(str || '').trim();
    var m = /^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i.exec(s);
    if (m) {
      var h = m[1];
      if (h.length === 3) h = h.replace(/./g, function (c) { return c + c; });
      var n = parseInt(h.slice(0, 6), 16);
      return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    }
    var r = /rgba?\(([^)]+)\)/i.exec(s);
    if (r) {
      var p = r[1].split(',');
      return [parseInt(p[0], 10) || 0, parseInt(p[1], 10) || 0, parseInt(p[2], 10) || 0];
    }
    return [120, 132, 148];
  }

  /* 三次贝塞尔缓动：只用来算解码进度（450ms, cubic-bezier(.16,1,.3,1)） */
  function cubicBezier(x1, y1, x2, y2) {
    var cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
    var cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
    function sx(t) { return ((ax * t + bx) * t + cx) * t; }
    function sy(t) { return ((ay * t + by) * t + cy) * t; }
    return function (x) {
      if (x <= 0) return 0;
      if (x >= 1) return 1;
      var t = x, i, e, d;
      for (i = 0; i < 8; i++) {
        e = sx(t) - x;
        if (Math.abs(e) < 1e-4) return sy(t);
        d = (3 * ax * t + 2 * bx) * t + cx;
        if (Math.abs(d) < 1e-6) break;
        t -= e / d;
        if (t < 0 || t > 1) break;
      }
      var lo = 0, hi = 1;
      t = x;
      for (i = 0; i < 14; i++) {
        var v = sx(t);
        if (Math.abs(v - x) < 1e-4) break;
        if (v > x) hi = t; else lo = t;
        t = (lo + hi) / 2;
      }
      return sy(t);
    };
  }

  var easeScramble = cubicBezier(0.16, 1, 0.3, 1);

  /* --------------------------- DOM / 画布装配 ---------------------------- */
  function mkCanvas(id, cssVar) {
    var el = document.createElement('canvas');
    el.id = id;
    el.setAttribute('aria-hidden', 'true');
    el.className = 'fx-canvas';
    if (cssVar) el.style.zIndex = cssVar;
    document.body.appendChild(el);
    dom.made = true;
    return el;
  }

  function ensureDom() {
    if (!document.body) return false;
    dom.field = document.getElementById('fx-field');
    if (!dom.field) dom.field = mkCanvas('fx-field');
    dom.spark = document.getElementById('fx-spark');
    if (!dom.spark) dom.spark = mkCanvas('fx-spark');

    dom.field.style.pointerEvents = 'none';
    dom.spark.style.pointerEvents = 'none';

    dom.fctx = dom.field.getContext('2d');
    dom.sctx = dom.spark.getContext('2d');
    return !!(dom.fctx && dom.sctx);
  }

  function applyZ() {
    if (dom.field) dom.field.style.zIndex = String(opts.zField);
    if (dom.spark) dom.spark.style.zIndex = String(opts.zSpark);
  }

  function readColors() {
    colors.a = toRgb(readCss('--neon-a', '#22e0ff'));
    colors.b = toRgb(readCss('--neon-b', '#ff3ea5'));
    colors.dim = toRgb(readCss('--dim2', '#828e9d'));
  }

  /* ------------------------------ 图案缓存 ------------------------------- */
  /* 点阵和 scanline 各做一张 gap×gap（device px）小图 + CanvasPattern：
     每帧只是 2 次 pattern 填充，不逐点画，避免 1400+ 次 fillRect。 */
  function dotPattern(gap, alive) {
    var key = 'dot|' + gap + '|' + (alive ? 1 : 0) + '|' + view.dpr;
    if (patCache[key]) return patCache[key];
    var d = view.dpr;
    var size = Math.max(2, Math.round(gap * d));
    var cv = document.createElement('canvas');
    cv.width = size;
    cv.height = size;
    var g = cv.getContext('2d');
    var dot = Math.max(2, Math.round(2 * d));   // 2 CSS px 的点（同样的 alpha 下靠面积显眼，不用提亮）
    var c = alive ? colors.a : colors.dim;
    g.fillStyle = rgba(c, alive ? 1 : 0.75);
    g.fillRect(Math.round((size - dot) / 2), Math.round((size - dot) / 2), dot, dot);
    var pat = dom.fctx.createPattern(cv, 'repeat');
    if (pat && typeof pat.setTransform === 'function') {
      try {
        var k = 1 / d;
        pat.setTransform(new DOMMatrix([k, 0, 0, k, 0, 0]));
      } catch (e) { /* 老引擎：退化成 gap*dpr 的稀疏点阵，仍可看 */ }
    }
    patCache[key] = pat;
    return pat;
  }

  function linePattern() {
    var key = 'scan|' + view.dpr;
    if (patCache[key]) return patCache[key];
    var d = view.dpr;
    var cv = document.createElement('canvas');
    cv.width = Math.max(1, Math.round(2 * d));    // 2px 周期
    cv.height = Math.max(1, Math.round(2 * d));
    var g = cv.getContext('2d');
    g.fillStyle = 'rgba(255,255,255,0.012)';      // 极淡 scanline
    g.fillRect(0, 0, cv.width, Math.max(1, Math.round(d * 0.5)));
    var pat = dom.fctx.createPattern(cv, 'repeat');
    if (pat && typeof pat.setTransform === 'function') {
      try {
        var k = 1 / d;
        pat.setTransform(new DOMMatrix([k, 0, 0, k, 0, 0]));
      } catch (e) { /* ignore */ }
    }
    patCache[key] = pat;
    return pat;
  }

  /* -------------------------------- 粒子 --------------------------------- */
  function particleCount() {
    return Math.min(cfg.particles, level === 'max' ? 64 : 28);
  }

  function resetParticles() {
    var n = particleCount();
    var out = [];
    for (var i = 0; i < n; i++) {
      out.push({
        x: Math.random() * view.w,
        y: Math.random() * view.h,
        a: Math.random() * TAU,
        s: 7 + Math.random() * 9,          // px/s，很慢
        r: 1.1 + Math.random() * 1.1,
        al: 0.55 + Math.random() * 0.45
      });
    }
    particles = out;
  }

  function fitParticles() {
    var n = particleCount();
    if (!particles.length) { resetParticles(); return; }
    if (particles.length > n) particles.length = n;
    while (particles.length < n) {
      particles.push({
        x: Math.random() * view.w, y: Math.random() * view.h,
        a: Math.random() * TAU, s: 7 + Math.random() * 9,
        r: 1.1 + Math.random() * 1.1, al: 0.55 + Math.random() * 0.45
      });
    }
    for (var i = 0; i < particles.length; i++) {
      var p = particles[i];
      if (p.x > view.w) p.x = Math.random() * view.w;
      if (p.y > view.h) p.y = Math.random() * view.h;
    }
  }

  /* -------------------------------- 尺寸 --------------------------------- */
  function resize() {
    if (!dom.fctx) return;
    var dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);
    var w = Math.max(1, window.innerWidth || document.documentElement.clientWidth || 1);
    var h = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
    var changed = dpr !== view.dpr || w !== view.w || h !== view.h;
    if (!changed) return;

    view.w = w; view.h = h; view.dpr = dpr;
    var bw = Math.round(w * dpr), bh = Math.round(h * dpr);
    dom.field.width = bw; dom.field.height = bh;
    dom.spark.width = bw; dom.spark.height = bh;
    patCache = {};
    fitParticles();

    if (reduced) paintStatic();
    else if (level !== 'off') syncLoop();
  }

  /* ------------------------------ 环境场绘制 ----------------------------- */
  /* 注：原本这里有一条全屏斜向扫描带（react-bits 的 Scanner 观感），用户看过之后要求去掉——
     整屏扫过去太抢眼。点阵、粒子连线、视差、底部透视网格保留。 */

  function drawFloor(ctx, w, h, px, py) {
    var alive = st.engineAlive !== false;
    var c = alive ? colors.a : colors.dim;
    var horizon = h * 0.58 + py * 0.5;
    var vx = w * 0.5 + px * 1.6;
    var depth = Math.max(1, h - horizon);
    var i, t;

    ctx.save();
    ctx.lineWidth = 1;
    ctx.strokeStyle = rgba(c, alive ? 0.05 : 0.04);
    ctx.beginPath();
    for (i = 1; i <= 7; i++) {
      t = i / 7;
      var y = horizon + depth * Math.pow(t, 2.2);
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
    }
    for (i = 0; i <= 12; i++) {
      var xb = -w * 0.4 + (w * 1.8) * (i / 12);
      ctx.moveTo(vx, horizon);
      ctx.lineTo(xb, h);
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawParticles(ctx, dt, px, py) {
    var alive = st.engineAlive !== false;
    var busy = alive && st.busy === true;
    var mul = busy ? 2 : (alive ? 1 : 0.35);
    var c = alive ? colors.a : colors.dim;
    var L = cfg.link;
    var i, j, p, q;

    ctx.save();
    ctx.translate(px, py);

    for (i = 0; i < particles.length; i++) {
      p = particles[i];
      p.x += Math.cos(p.a) * p.s * mul * dt;
      p.y += Math.sin(p.a) * p.s * mul * dt;
      if (p.x < -12) p.x = view.w + 12;
      else if (p.x > view.w + 12) p.x = -12;
      if (p.y < -12) p.y = view.h + 12;
      else if (p.y > view.h + 12) p.y = -12;
    }

    /* 连线：按 alpha 分 4 档批量 stroke（每帧最多 4 次 stroke 调用） */
    var b0 = [], b1 = [], b2 = [], b3 = [];
    var cap = level === 'max' ? 320 : 140;
    var used = 0;
    for (i = 0; i < particles.length && used < cap; i++) {
      p = particles[i];
      for (j = i + 1; j < particles.length; j++) {
        q = particles[j];
        var dx = p.x - q.x, dy = p.y - q.y;
        var d2 = dx * dx + dy * dy;
        if (d2 > L * L) continue;
        var k = 1 - Math.sqrt(d2) / L;
        var arr = k > 0.72 ? b3 : (k > 0.48 ? b2 : (k > 0.24 ? b1 : b0));
        arr.push(p.x, p.y, q.x, q.y);
        if (++used >= cap) break;
      }
    }
    var buckets = [b0, b1, b2, b3];
    ctx.lineWidth = 0.6;
    for (i = 0; i < buckets.length; i++) {
      var b = buckets[i];
      if (!b.length) continue;
      ctx.beginPath();
      for (j = 0; j < b.length; j += 4) {
        ctx.moveTo(b[j], b[j + 1]);
        ctx.lineTo(b[j + 2], b[j + 3]);
      }
      ctx.strokeStyle = rgba(c, MAX_LINK_ALPHA * (0.22 + i * 0.24));
      ctx.stroke();
    }

    /* 粒子本体：一次 path + 一次 fill */
    ctx.beginPath();
    for (i = 0; i < particles.length; i++) {
      p = particles[i];
      ctx.moveTo(p.x + p.r, p.y);
      ctx.arc(p.x, p.y, p.r, 0, TAU);
    }
    ctx.fillStyle = rgba(c, MAX_LINK_ALPHA);
    ctx.fill();

    ctx.restore();
  }

  function drawField(ts) {
    var ctx = dom.fctx;
    var w = view.w, h = view.h;
    if (!ctx || !w || !h) return;

    var alive = st.engineAlive !== false;
    var busy = alive && st.busy === true;
    var px = parallax.x, py = parallax.y;

    ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
    ctx.globalAlpha = 1;
    ctx.globalCompositeOperation = 'source-over';
    ctx.clearRect(0, 0, w, h);

    /* 1) 极淡 scanline（整页背景质感，只在面板缝隙里可见） */
    var lp = linePattern();
    if (lp) {
      ctx.fillStyle = lp;
      ctx.fillRect(0, 0, w, h);
    }

    /* 2) 点阵网格（1px 点；busy 时 alpha ×1.4；离线转冷灰） */
    var dp = dotPattern(cfg.gap, alive);
    if (dp) {
      ctx.save();
      ctx.translate(px, py);
      ctx.globalAlpha = alive ? (busy ? 0.12 : 0.09) : 0.055;
      ctx.fillStyle = dp;
      ctx.fillRect(-24, -24, w + 48, h + 48);
      ctx.restore();
      ctx.globalAlpha = 1;
    }

    /* 3) 斜向扫描带（std 12s / busy 4s 一轮；离线冻结成静态） */

    /* 4) max 档：底部透视网格 */
    if (cfg.perspective) drawFloor(ctx, w, h, px, py);

    /* 5) 稀疏粒子 + 近距连线 */
    drawParticles(ctx, loop.lastDt || 0, px, py);

    /* 6) busy：顶部一层品红辉光 */
    if (busy) {
      var g = ctx.createLinearGradient(0, 0, 0, 96);
      g.addColorStop(0, rgba(colors.b, 0.10));
      g.addColorStop(1, rgba(colors.b, 0));
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, 96);
    }
  }

  /* ------------------------------ 点击迸溅 ------------------------------- */
  function drawSparks(ts) {
    var ctx = dom.sctx;
    if (!ctx) return;
    var w = view.w, h = view.h;

    if (!sparks.length) {
      if (sparkDirty) {
        ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);
        sparkDirty = false;
      }
      return;
    }

    ctx.setTransform(view.dpr, 0, 0, view.dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    sparkDirty = true;

    var keep = [];
    for (var i = 0; i < sparks.length; i++) {
      var s = sparks[i];
      var p = (ts - s.t0) / SPARK_LIFE;
      if (p >= 1) continue;
      if (p < 0) p = 0;
      keep.push(s);
      var e = 1 - Math.pow(1 - p, 3);
      var dist = s.travel * e;
      var x = s.x + Math.cos(s.a) * dist;
      var y = s.y + Math.sin(s.a) * dist;
      var alpha = 1 - p;
      ctx.beginPath();
      ctx.arc(x, y, 1, 0, TAU);        /* 2px 直径 */
      ctx.fillStyle = rgba(s.c, alpha);
      ctx.fill();
    }
    sparks = keep;
  }

  function burst(x, y, count) {
    var n = clamp(Math.round(count || cfg.spark), 1, 64);
    var now = (window.performance && performance.now) ? performance.now() : Date.now();
    for (var i = 0; i < n; i++) {
      var t = n === 1 ? 0 : i / (n - 1);
      sparks.push({
        x: x, y: y,
        a: (i / n) * TAU + (Math.random() - 0.5) * 0.55,
        travel: 26 + Math.random() * 26,
        c: mixRgb(colors.a, colors.b, t),
        t0: now
      });
    }
    if (sparks.length > SPARK_CAP) sparks.splice(0, sparks.length - SPARK_CAP);
  }

  /* -------------------------------- 循环 --------------------------------- */
  function frame(ts) {
    loop.raf = requestAnimationFrame(frame);
    if (ts - loop.prev < FRAME_MS - 0.6) return;   /* 30fps 闸门 */
    var dt = loop.prev ? Math.min(0.12, (ts - loop.prev) / 1000) : 1 / 30;
    loop.prev = ts;
    loop.lastDt = dt;
    loop.frames++;
    loop.drawn++;
    step(dt, ts);
  }

  function step(dt, ts) {
    var k = Math.min(1, dt * 6);
    parallax.x += (parallax.tx - parallax.x) * k;
    parallax.y += (parallax.ty - parallax.y) * k;

    drawField(ts);
    drawSparks(ts);
  }

  function syncLoop() {
    var need = inited && level !== 'off' && !reduced && !document.hidden &&
               view.w > 0 && !!dom.fctx;
    if (need && !loop.on) {
      loop.on = true;
      loop.prev = 0;
      loop.lastDt = 1 / 30;
      loop.raf = requestAnimationFrame(frame);
    } else if (!need && loop.on) {
      loop.on = false;
      if (loop.raf) cancelAnimationFrame(loop.raf);
      loop.raf = 0;
    }
  }

  /* reduced-motion / off：画一帧静态画面（不跑 rAF） */
  function paintStatic() {
    if (!dom.fctx) return;
    parallax.x = 0; parallax.y = 0;
    drawField(0);
    drawSparks(0);
  }

  function clearCanvases() {
    if (dom.fctx) {
      dom.fctx.setTransform(1, 0, 0, 1, 0, 0);
      dom.fctx.clearRect(0, 0, dom.field.width, dom.field.height);
    }
    if (dom.sctx) {
      dom.sctx.setTransform(1, 0, 0, 1, 0, 0);
      dom.sctx.clearRect(0, 0, dom.spark.width, dom.spark.height);
    }
    sparks = [];
    sparkDirty = false;
  }

  function showCanvases(on) {
    if (dom.field) dom.field.style.display = on ? '' : 'none';
    if (dom.spark) dom.spark.style.display = on ? '' : 'none';
  }

  /* ------------------------------- 状态轮询 ------------------------------- */
  function applyState(s) {
    if (!s) return;
    var busy = !!s.busy;
    var alive = s.engineAlive !== false;
    if (busy === st.busy && alive === st.engineAlive) return;
    st.busy = busy;
    st.engineAlive = alive;
    if (reduced) paintStatic();
  }

  function poll() {
    if (!opts.getState) return;
    var s = null;
    try { s = opts.getState(); } catch (e) { return; }
    applyState(s);
  }

  function pollStart() {
    pollStop();
    if (!opts.getState) return;
    pollId = window.setInterval(poll, POLL_MS);
    poll();
  }

  function pollStop() {
    if (pollId) { window.clearInterval(pollId); pollId = 0; }
  }

  /* -------------------------------- 档位 --------------------------------- */
  function bodyClasses() {
    if (!document.body) return;
    var b = document.body.classList;
    b.toggle('fx-on', level !== 'off');
    b.toggle('fx-std', level === 'std');
    b.toggle('fx-max', level === 'max');
    b.toggle('fx-off', level === 'off');
  }

  function setLevelRaw(lv) {
    if (lv !== 'off' && lv !== 'std' && lv !== 'max') lv = 'std';
    level = lv;
    cfg = LEVELS[lv] || LEVELS.std;
    bodyClasses();
    if (!inited) return;

    if (level === 'off') {
      syncLoop();
      clearCanvases();
      showCanvases(false);
      return;
    }
    showCanvases(true);
    if (dom.fctx) { patCache = {}; }   /* gap 变了，图案重建 */
    fitParticles();
    if (reduced) { syncLoop(); paintStatic(); return; }
    syncLoop();
    if (!loop.on) paintStatic();
  }

  /* ------------------------------- 事件绑定 ------------------------------ */
  function bind(target, type, fn, opt) {
    if (!target || !target.addEventListener) return;
    target.addEventListener(type, fn, opt || false);
    listeners.push([target, type, fn, opt || false]);
  }

  function unbindAll() {
    for (var i = 0; i < listeners.length; i++) {
      var l = listeners[i];
      try { l[0].removeEventListener(l[1], l[2], l[3]); } catch (e) { /* ignore */ }
    }
    listeners = [];
  }

  function tapFx(el) {
    if (!el || !el.classList) return;
    el.classList.add('fx-tap');
    window.setTimeout(function () { el.classList.remove('fx-tap'); }, 140);
  }

  function onClick(e) {
    if (level === 'off' || !inited) return;
    var t = e.target;
    var hit = (t && t.closest) ? t.closest('.send,.btn-primary,.newchat,.danger') : null;
    if (!hit) return;
    var x = typeof e.clientX === 'number' ? e.clientX : 0;
    var y = typeof e.clientY === 'number' ? e.clientY : 0;
    if (reduced) { tapFx(hit); return; }     /* reduced：纯 opacity 反馈 */
    burst(x, y, cfg.spark);
    if (!loop.on) syncLoop();                /* 兜底：万一循环停着 */
  }

  function onMove(e) {
    var w = view.w || 1, h = view.h || 1;
    var nx = clamp((e.clientX / w) * 2 - 1, -1, 1);
    var ny = clamp((e.clientY / h) * 2 - 1, -1, 1);
    parallax.tx = -nx * 8;    /* 视差位移 ≤8px */
    parallax.ty = -ny * 8;
  }

  function onLeave() {
    parallax.tx = 0;
    parallax.ty = 0;
  }

  function onResize() {
    if (resizeId) window.clearTimeout(resizeId);
    resizeId = window.setTimeout(function () { resizeId = 0; resize(); }, 120);
  }

  function onVisible() {
    syncLoop();
    if (!loop.on) paintStatic();
  }

  var mq = null;
  function onMedia() {
    reduced = !!(mq && mq.matches);
    if (reduced) { syncLoop(); paintStatic(); }
    else { syncLoop(); }
  }

  /* ------------------------------- 文字解码 ------------------------------ */
  var CHARSET = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#$%&*+-/<>[]{}=_';

  function scramble(el, text) {
    if (!el) return function () {};
    var target = text == null ? (el.textContent || '') : String(text);
    var chars = Array.from ? Array.from(target) : target.split('');
    var n = chars.length;

    /* off 档 / reduced-motion：不跑解码，直接定格 */
    if (level === 'off' || reduced) {
      el.textContent = target;
      return function () {};
    }

    var plan = [];
    var i;
    for (i = 0; i < n; i++) {
      var base = n > 1 ? i / (n - 1) : 0;
      plan.push(0.06 + 0.86 * (0.72 * base + 0.28 * Math.random()));
    }

    var start = (window.performance && performance.now) ? performance.now() : Date.now();
    var dur = 450;
    var tid = 0;
    var done = false;

    function render(now) {
      var x = clamp((now - start) / dur, 0, 1);
      var p = easeScramble(x);
      var out = '';
      for (var k = 0; k < n; k++) {
        var ch = chars[k];
        if (ch === ' ' || ch === '\n' || p >= plan[k]) out += ch;
        else out += CHARSET[(Math.random() * CHARSET.length) | 0];
      }
      el.textContent = out;
      if (x >= 1) {
        done = true;
        el.textContent = target;
        if (tid) window.clearInterval(tid);
        tid = 0;
      }
    }

    tid = window.setInterval(function () {
      var now = (window.performance && performance.now) ? performance.now() : Date.now();
      render(now);
    }, 24);
    scrambleTimers.push(tid);
    render(start + 16);

    return function cancel() {
      if (tid) window.clearInterval(tid);
      tid = 0;
      if (!done) el.textContent = target;
    };
  }

  /* --------------------------------- API --------------------------------- */
  var FX = {
    __fx: true,
    version: '1.0.0',

    init: function (o) {
      o = o || {};
      if (typeof o.getState === 'function') opts.getState = o.getState;
      if (typeof o.zField === 'number') opts.zField = o.zField;
      if (typeof o.zSpark === 'number') opts.zSpark = o.zSpark;

      if (!document.body) {
        document.addEventListener('DOMContentLoaded', function () { FX.init(o); });
        return FX;
      }

      if (!ensureDom()) return FX;
      applyZ();
      readColors();
      inited = true;

      if (!mq && window.matchMedia) {
        try {
          mq = window.matchMedia('(prefers-reduced-motion: reduce)');
          reduced = mq.matches;
          if (mq.addEventListener) mq.addEventListener('change', onMedia);
          else if (mq.addListener) mq.addListener(onMedia);
        } catch (e) { reduced = false; }
      }

      /* 只绑一次 */
      if (!listeners.length) {
        bind(window, 'resize', onResize);
        bind(window, 'pointermove', onMove, { passive: true });
        bind(document, 'pointerleave', onLeave, { passive: true });
        bind(document, 'click', onClick, true);
        bind(document, 'visibilitychange', onVisible);
      }

      resize();
      setLevelRaw(o.level || (document.body.classList.contains('fx-max') ? 'max' : 'std'));
      pollStart();
      if (reduced) paintStatic();
      return FX;
    },

    setLevel: function (lv) {
      setLevelRaw(lv);
      return FX;
    },

    setState: function (s) {
      applyState(s);
      return FX;
    },

    scramble: scramble,

    spark: function (x, y, o) {
      if (level === 'off' || reduced) return FX;
      var n = (o && typeof o.count === 'number') ? o.count : cfg.spark;
      burst(x || 0, y || 0, n);
      if (!loop.on) syncLoop();
      return FX;
    },

    /* 只给自测用：同步画一帧，返回这帧的绘制耗时(ms)。
       正式接线不要调它（循环自己会画）。 */
    __paintOnce: function () {
      if (!dom.fctx) return -1;
      var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
      step(1 / 30, (window.performance && performance.now) ? performance.now() : Date.now());
      var t1 = (window.performance && performance.now) ? performance.now() : Date.now();
      return t1 - t0;
    },

    status: function () {
      return {
        level: level,
        running: loop.on,
        frames: loop.frames,
        drawn: loop.drawn,
        particles: particles.length,
        sparks: sparks.length,
        busy: st.busy,
        engineAlive: st.engineAlive,
        reduced: reduced,
        w: view.w,
        h: view.h,
        dpr: view.dpr,
      };
    },

    destroy: function () {
      syncLoop();
      pollStop();
      unbindAll();
      for (var i = 0; i < scrambleTimers.length; i++) window.clearInterval(scrambleTimers[i]);
      scrambleTimers = [];
      if (mq) {
        try {
          if (mq.removeEventListener) mq.removeEventListener('change', onMedia);
          else if (mq.removeListener) mq.removeListener(onMedia);
        } catch (e) { /* ignore */ }
        mq = null;
      }
      inited = false;
      if (dom.made) {
        if (dom.field && dom.field.parentNode) dom.field.parentNode.removeChild(dom.field);
        if (dom.spark && dom.spark.parentNode) dom.spark.parentNode.removeChild(dom.spark);
        dom.made = false;
      }
      dom.field = dom.spark = null;
      dom.fctx = dom.sctx = null;
      if (document.body) {
        document.body.classList.remove('fx-on', 'fx-std', 'fx-max');
      }
      return FX;
    }
  };

  window.FX = FX;
})();
// __FX_JS_END__
</script>

<script>
const AGENTS = __AGENTS__;
const STATUS_LABEL = __STATUS_LABEL__;
const ROLES = ['coordinator','planner','executor','tester','reviewer'];
const ALIAS = {deepseek:'coordinator', brain:'coordinator', codex:'executor', relay:'user'};
const CTRL_LABEL = {pause:'暂停', resume:'继续', stop:'停止', approve:'批准', rerun:'重新执行'};
const STAGE_LABEL = {plan:'规划', dispatch:'执行', execute:'执行', exec:'执行',
  verify:'验收', review:'复核', approve:'等你批准', report:'报告'};

let controlCooldown = 0;

const $ = id => document.getElementById(id);
const streamEl = $('stream'), sessionsEl = $('sessions'), inspEl = $('insp');
const welcomeHtml = ($('welcome') || {}).outerHTML || '';
const wsInput = $('ws-input'), wsBtn = $('ws-btn'), wsStatus = $('ws-status');
const input = $('input'), sendBtn = $('send');
const modal = $('modal'), modalPath = $('modal-path'), modalBody = $('modal-body');
const toastEl = $('toast');
const sessMenu = $('sess-menu');
const confirmEl = $('confirm');
const trashEl = $('trash'), trashListEl = $('trash-list');
const trashEntry = $('trash-entry'), trashCountEl = $('trash-count');
const trashCloseBtn = $('trash-close');
const trashEmptyBtn = $('trash-empty');
const unlockEl = $('unlock'), unlockInput = $('unlock-input');
const unlockBtn = $('unlock-btn'), unlockErr = $('unlock-err');

let st = {};
let activeId = '';
let localTyping = null;
let typingEl = null;
let firstPaint = true;
let echoSeq = 0;
const seen = new Map();

/* 消息分页：本地只留已加载的消息，轮询增量合并，翻页只在点击时发生。 */
const PAGE = 120;
let msgs = [];                 // 已加载的消息（升序）
let syncedSession = '';        // msgs 属于哪个会话
let hasMoreBefore = false;     // 上面还有更早的消息

const asInt = (v, d) => { const n = parseInt(v, 10); return isNaN(n) ? d : n; };

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => (
  {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const resolveAgent = a => ALIAS[a] || a || 'coordinator';
const agentOf = a => AGENTS[resolveAgent(a)] || {name: a || '助手', role: '', color: '#64748b', initial: '·'};
const baseName = p => String(p || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || String(p || '');
const p2 = n => String(n).padStart(2, '0');
function hhmmss(s){ s = String(s || ''); return s.length >= 19 ? s.slice(11, 19) : ''; }
function timeLabel(s){
  const t = hhmmss(s);
  if(!t) return '';
  const d = new Date(s);
  const now = new Date();
  if(!isNaN(d) && d.toDateString() === now.toDateString()) return t.slice(0, 5);
  return String(s).slice(5, 10);
}

/* ---------- 请求封装：凭证走 Cookie（浏览器自动带），URL 里不出现令牌 ---------- */
function showUnlock(message){
  document.body.classList.remove('ready');
  unlockEl.hidden = false;
  unlockErr.textContent = message || '';
  try { unlockInput.focus(); } catch(e){}
}

const API_HEADERS = {'Content-Type': 'application/json'};

async function apiGet(url){
  try{
    const res = await fetch(url, {cache: 'no-store', credentials: 'same-origin'});
    if(res.status === 401){ showUnlock('需要访问令牌：把 room/web-token 的内容粘贴进来'); return null; }
    if(!res.ok) return null;
    return await res.json();
  }catch(e){ return null; }
}

async function apiPost(url, body){
  try{
    const res = await fetch(url, {method: 'POST', credentials: 'same-origin',
      headers: API_HEADERS, body: JSON.stringify(body || {})});
    if(res.status === 401){ showUnlock('需要访问令牌：把 room/web-token 的内容粘贴进来'); return null; }
    if(res.status === 403) return {ok: false, error: '这个页面来源不被信任，刷新一下再试'};
    if(res.status === 415) return {ok: false, error: '请求格式不对（服务端只收 JSON）'};
    return await res.json().catch(() => ({ok: false, error: '服务端返回异常'}));
  }catch(e){ return {ok: false, error: '网络异常'}; }
}

function inlineCode(s){ return s.replace(/`([^`\n]+)`/g, '<code>$1</code>'); }
function fmtContent(text){
  const raw = String(text ?? '');
  const parts = raw.split('```');
  if(parts.length === 1) return inlineCode(esc(raw));
  return parts.map((seg, i) => {
    if(i % 2) return '<pre class="code">' + esc(seg.replace(/^\s*[A-Za-z0-9_+-]*\r?\n/, '')) + '</pre>';
    return inlineCode(esc(seg));
  }).join('');
}

function buildMsg(m){
  const role = m.role === 'user' ? 'user' : (m.role === 'system' ? 'system' : 'assistant');
  if(role === 'system'){
    const el = document.createElement('div');
    el.className = 'notice';
    el.dataset.id = String(m.id || '');
    const span = document.createElement('span');
    span.textContent = m.content || '';
    el.appendChild(span);
    return el;
  }
  const agentKey = resolveAgent(m.agent || (role === 'user' ? 'user' : 'coordinator'));
  const a = agentOf(agentKey);
  const el = document.createElement('div');
  const streaming = m.status === 'pending' || m.status === 'streaming';
  el.className = 'msg ' + role + (m.status === 'failed' ? ' failed' : '')
    + (streaming ? ' streaming' : '');
  el.dataset.id = String(m.id || '');
  el.style.setProperty('--who', a.color || '#58a6ff');

  const isUser = role === 'user';
  const who = isUser ? '你' : (a.name || agentKey);
  const badge = [];
  if(m.task_id) badge.push('<span class="role mono">' + esc(m.task_id) + '</span>');
  if(streaming) badge.push('<span class="role">处理中…</span>');

  el.innerHTML = ''
    + '<div class="av" style="background:' + esc(a.color) + '">' + esc(isUser ? '我' : a.initial) + '</div>'
    + '<div class="body">'
    +   '<div class="meta">'
    +     '<span class="who" style="color:' + esc(a.color) + '">' + esc(who) + '</span>'
    +     (isUser ? '' : '<span class="role">' + esc(a.role || '') + '</span>')
    +     '<span>' + esc(timeLabel(m.created_at)) + '</span>'
    +     badge.join('')
    +   '</div>'
    +   '<div class="bubble">' + fmtContent(m.content) + '</div>'
    + '</div>';

  const body = el.querySelector('.body');
  const arts = Array.isArray(m.artifacts) ? m.artifacts : [];
  if(arts.length){
    const host = document.createElement('div');
    host.className = 'artifacts';
    fillArtifacts(host, arts);
    body.appendChild(host);
  }
  if(m.status === 'failed'){
    const meta = m.meta || {};
    const reason = meta.error || meta.reason || '';
    const box = document.createElement('div');
    box.className = 'fail';
    box.textContent = reason ? ('失败：' + reason) : '这一步没有通过，请查看上面的内容与右侧证据';
    body.appendChild(box);
  }
  return el;
}

function resetStream(){
  streamEl.innerHTML = '';
  seen.clear();
  if(typingEl){ typingEl.remove(); typingEl = null; }
  ensureEarlier();
}
function nearBottom(){
  return streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 120;
}
function scrollDown(){ streamEl.scrollTop = streamEl.scrollHeight; }

/* ---------- 消息增量合并 / 翻页 / 断层自愈 ---------- */
const MAX_HEAL_PAGES = 5;      // 自动补断层最多补 5 页，补不齐就明说，不静默
const MAX_RETRY_PAGES = 20;    // 用户点「重试补齐」时给更多页

function msgSig(m){
  return JSON.stringify([m.role, m.agent, m.content, m.artifacts || [],
                         m.status, m.task_id, (m.meta && m.meta.error) || '']);
}
function orderKey(m){ return String(m.created_at || '') + '|' + String(m.id || ''); }
function byOrder(a, b){
  const ka = orderKey(a), kb = orderKey(b);
  return ka < kb ? -1 : ka > kb ? 1 : 0;
}
/* 翻页和断层自愈共用这一个：只把新的/变了的消息并进来，再按时间+id 排好。 */
function mergeMessages(list){
  if(!Array.isArray(list) || !list.length) return false;
  const index = new Map(msgs.map(m => [String(m.id), m]));
  let changed = false;
  list.forEach(m => {
    const id = String((m && m.id) || '');
    if(!id) return;
    const cur = index.get(id);
    if(!cur || msgSig(cur) !== msgSig(m)){ index.set(id, m); changed = true; }
  });
  if(changed) msgs = Array.from(index.values()).sort(byOrder);
  return changed;
}

/* id 是 msg-数字（也容旧账本的纯数字）：取不到数字就跳过自愈，不报错。 */
function idNum(m){
  const id = String((m && m.id) || '');
  const hit = /^msg-(\d+)$/.exec(id) || /^(\d+)$/.exec(id);
  return hit ? parseInt(hit[1], 10) : null;
}
function lastIdNum(list){
  for(let i = list.length - 1; i >= 0; i--){
    const n = idNum(list[i]);
    if(n !== null) return n;
  }
  return null;
}
function firstIdNum(list){
  for(let i = 0; i < list.length; i++){
    const n = idNum(list[i]);
    if(n !== null) return n;
  }
  return null;
}
/* 服务端这页的第一条比本地最后一条大 1 以上 → 中间断了一截。 */
function findGap(incoming){
  if(!incoming || !incoming.length) return null;
  const maxId = lastIdNum(msgs);
  const firstId = firstIdNum(incoming);
  if(maxId === null || firstId === null) return null;
  if(firstId <= maxId + 1) return null;
  return {from: maxId + 1, to: firstId - 1, boundary: String(incoming[0].id)};
}

let gapState = null;           // 还没补上的缺口 {from, to}
let healing = false;           // 正在补，别再触发一轮

function earlierEl(){
  let btn = document.getElementById('earlier');
  if(!btn){
    btn = document.createElement('button');
    btn.id = 'earlier';
    btn.className = 'earlier';
    btn.type = 'button';
    btn.textContent = '加载更早的 ' + PAGE + ' 条';
    btn.hidden = true;
    btn.onclick = loadEarlier;
  }
  return btn;
}
/* 流顶部固定顺序：断层提示 → 「加载更早」→ 消息 */
function ensureEarlier(){
  const btn = earlierEl();
  btn.hidden = !hasMoreBefore;
  const note = renderGapNote();
  if(btn.parentNode) btn.remove();
  streamEl.insertBefore(btn, note && note.parentNode === streamEl ? note.nextSibling : streamEl.firstChild);
  return btn;
}
/* 整条流按 msgs 的顺序重建：补断层的消息要插回中间，append 是插不到中间的。 */
function rebuildStream(){
  const wasNear = nearBottom();
  resetStream();
  renderMessages(msgs);
  ensureEarlier();
  if(wasNear) scrollDown();
}
function renderGapNote(){
  let el = document.getElementById('gap');
  if(!el){
    el = document.createElement('div');
    el.id = 'gap';
    el.className = 'gap-note';
    el.hidden = true;
  }
  if(!gapState){
    if(el.parentNode) el.remove();
    return null;
  }
  const missing = gapState.to - gapState.from + 1;
  el.innerHTML = '<span>历史消息有断层（缺 ' + missing + ' 条）</span>'
    + '<button type="button" id="gap-retry">重试补齐</button>'
    + '<span class="muted">也可以点上面的「加载更早」往前翻</span>';
  el.hidden = false;
  streamEl.insertBefore(el, streamEl.firstChild);
  const retry = document.getElementById('gap-retry');
  if(retry) retry.onclick = retryGap;
  return el;
}

/* 循环 /api/messages?before=<缺口上界+1> 把缺口补回来；boundary 是「第一条新消息」的原始 id。 */
async function healGap(gap, boundary, maxPages){
  if(healing) return;
  healing = true;
  let from = gap.from, to = gap.to, bound = boundary, pages = 0;
  try{
    while(pages < (maxPages || MAX_HEAL_PAGES) && from <= to){
      pages += 1;
      const q = new URLSearchParams();
      q.set('before', bound);                     // before 是不含的
      q.set('limit', String(PAGE));
      if(activeId) q.set('session', activeId);
      const data = await apiGet('/api/messages?' + q.toString());
      if(!data || data.ok === false || !Array.isArray(data.messages) || !data.messages.length) break;
      const chunk = data.messages.filter(m => {
        const n = idNum(m);
        return n !== null && n >= from && n <= to;
      });
      if(!chunk.length) break;
      mergeMessages(chunk);                       // 和「加载更早」共用合并逻辑
      const lowest = Math.min.apply(null, chunk.map(idNum));
      to = lowest - 1;
      if(to < from) break;
      const oldest = data.messages.reduce((a, b) => (idNum(a) <= idNum(b) ? a : b));
      const oldestId = String(oldest.id);
      if(oldestId === bound) break;               // 没往前走，防死循环
      bound = oldestId;
      if(!data.has_more) break;
    }
  }finally{
    healing = false;
  }
  gapState = from <= to ? {from: from, to: to} : null;
  rebuildStream();                     // 补回来的消息要插回中间，整条流按顺序重排一次
  if(gapState){
    toast('断层没补全，还缺 ' + (to - from + 1) + ' 条，可以点「重试补齐」');
  }else{
    toast('已补齐中间断掉的 ' + (gap.to - gap.from + 1) + ' 条消息');
  }
}

async function retryGap(){
  if(!gapState || healing) return;
  const btn = document.getElementById('gap-retry');
  if(btn) btn.disabled = true;
  const above = msgs.find(m => idNum(m) === gapState.to + 1);
  const boundary = above ? String(above.id) : String(gapState.to + 1);
  await healGap({from: gapState.from, to: gapState.to}, boundary, MAX_RETRY_PAGES);
}

async function loadEarlier(){
  const btn = earlierEl();
  if(btn.disabled) return;
  btn.disabled = true;
  btn.textContent = '正在加载…';
  const before = msgs.length ? String(msgs[0].id) : '';
  const anchorEl = before ? streamEl.querySelector('[data-id="' + before + '"]') : null;
  const anchorTop = anchorEl ? anchorEl.getBoundingClientRect().top : 0;
  const q = new URLSearchParams();
  q.set('before', before);
  q.set('limit', String(PAGE));
  if(activeId) q.set('session', activeId);
  const data = await apiGet('/api/messages?' + q.toString());
  btn.disabled = false;
  btn.textContent = '加载更早的 ' + PAGE + ' 条';
  if(!data || data.ok === false || !Array.isArray(data.messages) || !data.messages.length){
    hasMoreBefore = false;
    ensureEarlier();
    if(!data) toast('翻页失败：需要访问令牌或服务不在');
    return;
  }
  mergeMessages(data.messages);      // 和自愈同一套合并
  hasMoreBefore = !!data.has_more;
  resetStream();
  renderMessages(msgs);
  ensureEarlier();
  // 翻完别让视线跳走：把原来那条重新对回屏幕同一位置
  if(before){
    const again = streamEl.querySelector('[data-id="' + before + '"]');
    if(again) streamEl.scrollTop += (again.getBoundingClientRect().top - anchorTop);
  }
}

function renderMessages(list){
  const ids = new Set();
  list.forEach((m, i) => {
    const id = m.id ? String(m.id) : ('idx-' + i);
    ids.add(id);
    const sig = JSON.stringify([m.role, m.agent, m.content, m.artifacts || [],
                                m.status, m.task_id, (m.meta && m.meta.error) || '']);
    const cur = seen.get(id);
    if(!cur){
      const el = buildMsg(m);
      seen.set(id, {sig, el});
      // 交接时刻：新来的 Agent 消息扫一道它自己的颜色。首屏把历史一次画出来时不演
      if(!firstPaint && m.role !== 'user') el.classList.add('arrive');
      streamEl.appendChild(el);
    } else if(cur.sig !== sig){
      const el = buildMsg(m);
      cur.el.replaceWith(el);
      cur.sig = sig;
      cur.el = el;
    }
  });
  for(const [id, cur] of Array.from(seen)){
    if(!ids.has(id)){ cur.el.remove(); seen.delete(id); }
  }
  renderTyping();
}

function renderTyping(){
  const who = (st.typing && st.typing.who) || (localTyping && localTyping.who) || '';
  if(!who){
    if(typingEl) typingEl.remove();
    return;
  }
  if(!typingEl){
    typingEl = document.createElement('div');
    typingEl.className = 'typing';
    typingEl.innerHTML = '<span class="dots"><i></i><i></i><i></i></span><span class="tw"></span>';
  }
  typingEl.querySelector('.tw').textContent = (agentOf(who).name || who) + ' 正在思考…';
  streamEl.appendChild(typingEl);
}

/* ---------- 会话列表：主列表 + 已归档分组 + 行尾菜单 ---------- */
let archOpen = false;          // 已归档分组是否展开（归档后自动展开，让用户看到它去哪了）
let menuSessId = '';           // 菜单当前对应的会话
let menuAnchor = null;         // 打开菜单的那一行/按钮（Esc 后焦点还回去）
let menuIndex = 0;
let confirmRun = null;         // 「确定」要跑的动作（删会话 / 清空回收站…），空则不动作
let confirmBack = null;        // 关掉确认框后焦点还回去
let lastSessHtml = '';         // 上一次渲染出来的列表 HTML（没变就不重建 DOM）
let renamingId = '';           // 正在行内改名的会话（非空时暂停列表重绘，别把输入框吞掉）
let lastTrashSig = '';         // 回收站面板上一次渲染的内容签名
let trashBack = null;          // 关掉回收站后焦点还回去

function sessRow(s, archived){
  const ws = baseName(s.workspace || '');
  const id = esc(s.id);
  const tag = archived ? '<span class="arch-tag">已归档</span>' : '';
  const tail = archived
    ? '<button class="sess-restore" data-restore="' + id + '" title="恢复到列表">恢复</button>'
    : '<button class="sess-more" data-more="' + id + '" aria-haspopup="menu"'
      + ' aria-expanded="false" aria-label="更多操作" title="更多操作（Enter 打开）">⋯</button>';
  const sub = '<span class="sess-meta">' + esc(ws || '未绑定工作区') + ' · '
    + esc(String(s.message_count || 0)) + ' 条消息</span>';
  return '<div class="sess' + (archived ? ' archived' : (s.id === activeId ? ' sel' : '')) + '"'
    + ' data-id="' + id + '" role="button" tabindex="0"'
    + ' aria-current="' + (s.id === activeId && !archived ? 'true' : 'false') + '">'
    + '<div class="sess-top"><span class="sess-title">' + esc(s.title || '新会话') + '</span>'
    + tag + '<span class="sess-time">' + esc(timeLabel(s.updated_at)) + '</span></div>'
    + '<div class="sess-sub">' + sub + (archived ? tail : '') + '</div>'
    + (archived ? '' : tail) + '</div>';
}

function renderSessions(){
  const list = st.sessions || [];
  const live = list.filter(s => !s.archived_at);
  const arch = list.filter(s => s.archived_at);
  $('session-count').textContent = live.length;
  const trashed = st.trash || [];
  if(trashCountEl) trashCountEl.textContent = trashed.length;
  if(trashEntry) trashEntry.classList.toggle('has', trashed.length > 0);
  if(trashEl && !trashEl.hidden) renderTrash();
  let html = '';
  if(!live.length){
    html += '<div class="empty sess-empty">还没有会话 · 在下面说一句话就会开一个新的</div>';
  } else {
    html += live.map(s => sessRow(s, false)).join('');
  }
  if(arch.length){
    html += '<div class="arch-group' + (archOpen ? ' open' : '') + '">'
      + '<button class="arch-head" id="arch-toggle" aria-expanded="' + (archOpen ? 'true' : 'false') + '">'
      + '<span class="chev">›</span><span>已归档 · ' + arch.length + '</span></button>'
      + '<div class="arch-list" id="arch-list">' + arch.map(s => sessRow(s, true)).join('')
      + '</div></div>';
  }
  // 每秒轮询都会走到这里：内容没变就别重建 DOM，否则键盘焦点每秒被清一次
  if(renamingId) return;        // 正在改名：先把列表冻住，等改名结束再重绘
  if(html === lastSessHtml) return;
  lastSessHtml = html;
  sessionsEl.innerHTML = html;
  wireSessions();
}

function wireSessions(){
  sessionsEl.querySelectorAll('.sess').forEach(el => {
    const id = el.dataset.id;
    const archived = el.classList.contains('archived');
    el.onclick = ev => {
      if(ev.target.closest('.sess-more') || ev.target.closest('.sess-restore')) return;
      if(archived){ toast('已归档，先恢复再打开'); return; }
      selectSession(id);
    };
    // 双击标题行 = 行内改名（已归档的不给改，先恢复）
    el.ondblclick = ev => {
      if(archived) return;
      if(ev.target.closest('.sess-more') || ev.target.closest('.sess-restore')) return;
      ev.preventDefault();
      startRename(id);
    };
    el.onkeydown = ev => {
      if(ev.key === 'Enter'){ ev.preventDefault(); openSessMenu(id, el); }
      else if(ev.key === ' '){ ev.preventDefault();
        if(archived){ toast('已归档，先恢复再打开'); } else { selectSession(id); } }
    };
  });
  sessionsEl.querySelectorAll('.sess-more').forEach(btn => {
    btn.onclick = ev => { ev.stopPropagation(); openSessMenu(btn.dataset.more, btn); };
  });
  sessionsEl.querySelectorAll('.sess-restore').forEach(btn => {
    btn.onclick = ev => { ev.stopPropagation(); sessArchive(btn.dataset.restore, false); };
  });
  const toggle = $('arch-toggle');
  if(toggle) toggle.onclick = () => {
    archOpen = !archOpen;
    toggle.parentElement.classList.toggle('open', archOpen);
    toggle.setAttribute('aria-expanded', archOpen ? 'true' : 'false');
  };
}

function sessById(id){
  return (st.sessions || []).find(s => s.id === id) || null;
}

/* ---------- 行尾菜单：Tab 到行 → Enter 打开 → ↑/↓ 选 → Esc 关 ---------- */
function menuItems(){ return Array.from(sessMenu.querySelectorAll('.mi')); }

function paintMenu(){
  menuItems().forEach((btn, i) => btn.classList.toggle('cur', i === menuIndex));
}

function openSessMenu(id, anchor){
  const s = sessById(id);
  if(!s) return;
  if(!sessMenu.hidden && menuSessId === id){ closeSessMenu(true); return; }   // 再点一次 = 收起
  menuSessId = id;
  menuAnchor = anchor || null;
  sessMenu.hidden = false;
  const r = (anchor && anchor.getBoundingClientRect)
    ? anchor.getBoundingClientRect() : {left: 0, top: 0, right: 0, bottom: 0};
  const w = sessMenu.offsetWidth || 140;
  const h = sessMenu.offsetHeight || 80;
  let left = r.right - w;
  let top = r.bottom + 4;
  if(top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 4);
  sessMenu.style.left = Math.max(8, Math.min(left, window.innerWidth - w - 8)) + 'px';
  sessMenu.style.top = Math.max(8, top) + 'px';
  document.querySelectorAll('.sess-more.on').forEach(b => b.classList.remove('on'));
  if(anchor && anchor.classList && anchor.classList.contains('sess-more')){
    anchor.classList.add('on');
    anchor.setAttribute('aria-expanded', 'true');
  }
  menuIndex = 0;
  paintMenu();
  const first = menuItems()[0];
  if(first) first.focus();
}

function closeSessMenu(focusBack){
  if(sessMenu.hidden) return;
  sessMenu.hidden = true;
  menuSessId = '';
  document.querySelectorAll('.sess-more.on').forEach(b => {
    b.classList.remove('on');
    b.setAttribute('aria-expanded', 'false');
  });
  if(focusBack && menuAnchor && menuAnchor.focus){
    try { menuAnchor.focus(); } catch(e){}
  }
  menuAnchor = null;
}

function sessKeys(ev){
  if(confirmEl && !confirmEl.hidden){                 // 确认框开着：Esc 取消、Tab 只在两个按钮间转
    if(ev.key === 'Escape'){
      ev.preventDefault();
      // 模态优先：别让同一个 Esc 继续传到下面那层（回收站会把面板也关掉）
      ev.stopImmediatePropagation();
      closeConfirm(true);
      return;
    }
    if(ev.key === 'Tab'){
      const ring = [$('confirm-cancel'), $('confirm-ok')];
      const at = ring.indexOf(document.activeElement);
      ev.preventDefault();
      const step = ev.shiftKey ? -1 : 1;
      const pos = (at + step + ring.length) % ring.length;
      if(ring[pos]) ring[pos].focus();
    }
    return;
  }
  if(sessMenu.hidden) return;
  const items = menuItems();
  if(!items.length) return;
  if(ev.key === 'Escape'){ ev.preventDefault(); closeSessMenu(true); return; }
  if(ev.key === 'Tab'){ closeSessMenu(false); return; }
  if(ev.key === 'ArrowDown' || ev.key === 'ArrowUp'){
    ev.preventDefault();
    menuIndex = (menuIndex + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    paintMenu();
    items[menuIndex].focus();
    return;
  }
  if(ev.key === 'Home' || ev.key === 'End'){
    ev.preventDefault();
    menuIndex = ev.key === 'Home' ? 0 : items.length - 1;
    paintMenu();
    items[menuIndex].focus();
  }
}

/* ---------- 确认框（通用）：默认「取消」有焦点，回车不会误删 ---------- */
function askConfirm(opts){
  const o = opts || {};
  confirmBack = document.activeElement;
  confirmRun = typeof o.run === 'function' ? o.run : null;
  $('confirm-title').textContent = o.title || '确认';
  $('confirm-target').innerHTML = o.target || '';
  $('confirm-warn').innerHTML = o.warn || '';
  $('confirm-ok').textContent = o.ok || '确定';
  confirmEl.hidden = false;
  const cancel = $('confirm-cancel');
  if(cancel){ try { cancel.focus(); } catch(e){} }      // 焦点默认在「取消」
}

function openConfirm(id){
  const s = sessById(id);
  if(!s) return;
  closeSessMenu(false);
  askConfirm({
    title: '删除会话',
    target: '<b>' + esc(s.title || '新会话') + '</b> · '
      + esc(String(s.message_count || 0)) + ' 条消息',
    warn: '删除后这个会话不再出现在列表里，但会留在左栏底部的「回收站」里，随时能还原',
    ok: '删除',
    run: () => doDelete(id)
  });
}

function closeConfirm(focusBack){
  if(confirmEl.hidden) return;
  confirmEl.hidden = true;
  confirmRun = null;
  if(focusBack && confirmBack && confirmBack.focus){
    try { confirmBack.focus(); } catch(e){}
  }
  confirmBack = null;
}

/* 「确定」按钮统一走这里：先收起框，再跑这一轮登记的动作 */
function runConfirm(){
  const run = confirmRun;
  closeConfirm(false);
  if(run){ try { run(); } catch(e){} }
}

async function doDelete(id){
  if(!id) return;
  const data = await apiPost('/api/session/delete', {id: id, confirm: true});
  if(!data || !data.ok){ toast((data && data.error) || '删除失败'); return; }
  toast('已删除');
  const next = (data && data.active) || '';
  if(next !== activeId){ activeId = next; localTyping = null; }
  await load({fresh: true});
}

async function sessArchive(id, archived){
  closeSessMenu(false);
  const data = await apiPost('/api/session/archive', {id: id, archived: archived});
  if(!data || !data.ok){ toast((data && data.error) || (archived ? '归档失败' : '恢复失败')); return; }
  toast(archived ? '已归档' : '已恢复');
  if(archived) archOpen = true;
  const next = (data && data.active) || '';
  if(next !== activeId){ activeId = next; localTyping = null; }
  await load({fresh: true});
}

/* ---------- 行内改名：双击标题（或菜单里的「重命名」） ---------- */
function sessRowEl(id){
  return Array.from(sessionsEl.querySelectorAll('.sess'))
    .find(el => el.dataset.id === id) || null;
}

function endRename(){
  renamingId = '';
  lastSessHtml = '';            // DOM 已经落后了，逼下一次重绘把标题换回来
  renderSessions();
}

async function commitRename(inputEl, id, before){
  if(!renamingId) return;       // Enter 之后 blur 还会再来一次，别提交两遍
  const title = String(inputEl.value || '').trim();
  renamingId = '';
  if(!title || title === before){ endRename(); return; }
  const data = await apiPost('/api/session/rename', {id: id, title: title});
  if(!data || !data.ok){
    toast((data && data.error) || '改名失败');
    endRename();
    return;
  }
  toast('已改名');
  await load({fresh: true});
}

function startRename(id){
  const s = sessById(id);
  const row = sessRowEl(id);
  closeSessMenu(false);
  if(!s || !row || renamingId) return;
  const title = row.querySelector('.sess-title');
  if(!title) return;
  renamingId = id;
  const box = document.createElement('input');
  box.type = 'text';
  box.className = 'sess-rename';
  box.maxLength = 60;
  box.value = s.title || '';
  box.setAttribute('aria-label', '会话标题，回车保存，Esc 取消');
  title.replaceWith(box);
  box.onkeydown = ev => {
    if(ev.key === 'Enter'){ ev.preventDefault(); commitRename(box, id, s.title || ''); }
    else if(ev.key === 'Escape'){ ev.preventDefault(); renamingId = ''; endRename(); }
  };
  box.onblur = () => { commitRename(box, id, s.title || ''); };
  box.focus();
  try { box.select(); } catch(e){}
}

/* ---------- 回收站：列出去掉的会话，一键还原 ---------- */
function renderTrash(){
  if(!trashListEl) return;
  const rows = st.trash || [];
  if(trashEmptyBtn){                                    // 空的就别让人点
    trashEmptyBtn.disabled = !rows.length;
    trashEmptyBtn.textContent = rows.length ? ('清空回收站（' + rows.length + '）') : '清空回收站';
  }
  const sig = rows.map(t => t.trash_name).join('|');
  if(sig === lastTrashSig) return;
  lastTrashSig = sig;
  if(!rows.length){
    trashListEl.innerHTML = '<div class="trash-empty">回收站是空的<br>删掉的会话会出现在这里，随时能还原</div>';
    return;
  }
  trashListEl.innerHTML = rows.map(t => {
    const sub = [];
    if(t.session_id) sub.push(esc(t.session_id));
    sub.push(esc(String(t.message_count || 0)) + ' 条消息');
    const when = timeLabel(t.deleted_at);
    if(when) sub.push('删于 ' + esc(when));
    return '<div class="trash-row">'
      + '<div class="info"><div class="ttl" title="' + esc(t.title) + '">' + esc(t.title) + '</div>'
      + '<div class="sub">' + sub.join(' · ') + '</div></div>'
      + '<button class="back" data-back="' + esc(t.trash_name) + '">还原</button></div>';
  }).join('');
  trashListEl.querySelectorAll('button[data-back]').forEach(btn => {
    btn.onclick = () => restoreSession(btn.dataset.back, btn);
  });
}

function openTrash(){
  if(!trashEl || !trashEl.hidden) return;
  trashBack = document.activeElement;
  lastTrashSig = '';
  trashEl.hidden = false;
  renderTrash();
  const first = trashListEl.querySelector('button[data-back]') || trashCloseBtn;
  if(first){ try { first.focus(); } catch(e){} }
}

function closeTrash(focusBack){
  if(!trashEl || trashEl.hidden) return;
  trashEl.hidden = true;
  lastTrashSig = '';
  if(focusBack && trashBack && trashBack.focus){ try { trashBack.focus(); } catch(e){} }
  trashBack = null;
}

/* 清空回收站：先问一句（默认落在「取消」上），确认才真删 */
function askEmptyTrash(){
  const n = (st.trash || []).length;
  if(!n){ toast('回收站本来就是空的'); return; }
  askConfirm({
    title: '清空回收站',
    target: '这里的 <b>' + n + '</b> 个会话会被彻底删除',
    warn: '「还原」是捡回来，「清空」是删掉文件，删完就找不回来了',
    ok: '彻底删除',
    run: doEmptyTrash
  });
}

async function doEmptyTrash(){
  const data = await apiPost('/api/session/empty-trash', {});
  if(!data || !data.ok){ toast((data && data.error) || '清空失败'); return; }
  const gone = Number(data.removed || 0);
  toast(gone ? ('已清空 ' + gone + ' 个会话') : '回收站本来就是空的');
  if(data.error) toast(data.error);                     // 个别文件删不掉时单独说一句
  lastTrashSig = '';
  await load({fresh: true});
  renderTrash();
  if(!(st.trash || []).length) closeTrash(true);
}

async function restoreSession(name, btn){
  if(!name) return;
  if(btn){ btn.disabled = true; btn.textContent = '还原中…'; }
  const data = await apiPost('/api/session/restore', {trash_name: name});
  if(!data || !data.ok){
    if(btn){ btn.disabled = false; btn.textContent = '还原'; }
    toast((data && data.error) || '还原失败');
    return;
  }
  const got = (data && data.session) || {};
  toast('已还原：' + (got.title || '会话'));
  closeTrash(false);
  const next = (data && data.active) || '';
  if(next && next !== activeId){ activeId = next; localTyping = null; }
  await load({fresh: true});
}

function renderWorkspace(){
  const ws = st.workspace || (st.session && st.session.workspace) || '';
  const card = $('ws-card');
  const hint = $('composer-ws');
  if(!ws){
    card.innerHTML = '<div class="wname"><span class="dot"></span>还没有工作区</div>'
      + '<div class="wpath">先选一个仓库目录，协作台才知道在哪儿改代码</div>'
      + '<button class="ws-pick" id="ws-pick">选择工作区</button>';
    const pickBtn = $('ws-pick');
    if(pickBtn) pickBtn.onclick = () => openPicker();
    if(hint) hint.textContent = '未绑定工作区 · 点上面「选择工作区」浏览目录';
  } else {
    const known = (st.workspaces || []).find(w => w.path === ws);
    const exists = known ? known.exists : true;
    const count = (st.sessions || []).filter(s => s.workspace === ws).length;
    card.innerHTML = '<div class="wname"><span class="dot ' + (exists ? 'on' : '') + '"></span>'
      + esc(baseName(ws) || ws) + '</div>'
      + '<div class="wpath">' + esc(ws) + '</div>'
      + '<div class="wmeta"><span>' + (exists ? '可用' : '路径不可用') + '</span>'
      + '<span>' + count + ' 个会话</span></div>'
      + '<button class="ws-pick" id="ws-pick">换一个工作区</button>';
    const pickBtn = $('ws-pick');
    if(pickBtn) pickBtn.onclick = () => openPicker();
    if(hint) hint.textContent = '工作区：' + ws;
  }
  const cta = $('welcome-cta');
  if(cta){
    cta.innerHTML = ws ? '' :
      '<button class="cta-btn" id="welcome-pick">选择工作区</button>'
      + '<span class="cta-hint">可以浏览目录、先看「Git 状态 · 多少文件 · 能不能写」再确认</span>';
    const wb = $('welcome-pick');
    if(wb) wb.onclick = () => openPicker();
  }
  if(document.activeElement !== wsInput) wsInput.value = ws;
}

function renderInspector(){
  const agents = st.agents || {};
  const task = st.task || null;
  const changes = st.changes || {};
  const busyCount = ROLES.filter(r => agents[r] === 'working').length;

  const models = st.models || {};
  const modelRows = {};
  (models.roles || []).forEach(row => { modelRows[row.role] = row; });

  const agentRows = ROLES.map(r => {
    const a = agentOf(r);
    const busy = agents[r] === 'working';
    const m = modelRows[r] || {};
    // 模型挂在角色名下面：一眼看到"这个角色现在是谁在干"，点一下能换
    const modelLabel = m.model || '默认';
    const badge = m.switchable
      ? '<button class="am" data-role="' + esc(r) + '" title="点击换模型（'
        + esc(m.source || '配置') + '）">' + esc(modelLabel) + '</button>'
      : '<span class="am fixed" title="' + esc(m.reason || '') + '">' + esc(modelLabel) + '</span>';
    return '<div class="agent-row ' + (busy ? 'busy' : '') + '">'
      + '<span class="dot"></span>'
      + '<div><div class="an" style="color:' + esc(a.color) + '">' + esc(a.name) + '</div>'
      + '<div class="ar">' + esc(a.role) + '</div>'
      + '<div class="amrow">' + badge
      + '<span class="ams">' + esc(m.source || '') + '</span></div></div>'
      + '<span class="as">' + (busy ? '工作中' : '空闲') + '</span></div>';
  }).join('');

  let taskBlock = '<div class="empty">还没有任务卡<br>发一条需求就会自动建卡</div>';
  if(task){
    // 产物排在最前（workspace: 引用），证据文件跟在后面
    const products = Array.isArray(task.products) ? task.products : [];
    const arts = (Array.isArray(task.artifacts) ? task.artifacts : []).map(a => a.path || a);
    const refs = [];
    products.forEach(p => { if(p && p.ref && refs.indexOf(p.ref) < 0) refs.push(p.ref); });
    arts.forEach(a => { if(a && refs.indexOf(a) < 0) refs.push(a); });
    const artHtml = refs.length
      ? '<div class="artifacts">' + refs.map(ref => {
          const hit = products.find(p => p && p.ref === ref);
          return chipHtml(ref, hit ? hit.in_scope !== false : true);
        }).join('') + '</div>'
      : '';
    taskBlock = '<div class="task-card">'
      + '<div class="tid">' + esc(task.id || '') + '</div>'
      + '<div class="ttitle">' + esc(task.title || '（无标题）') + '</div>'
      + '<div class="chips">'
      +   '<span class="chip ' + esc(task.status || '') + '">'
      +     esc(STATUS_LABEL[task.status] || task.status || '') + '</span>'
      +   '<span class="chip">' + ((task.rounds || []).length) + ' 轮</span>'
      +   '<span class="chip">验收 ' + ((task.acceptance || []).length) + '</span>'
      + '</div>'
      + '<div class="kv"><div class="k">工作区</div><div class="v mono">'
      +   esc(task.workspace || '未设置') + '</div>'
      +   '<div class="k">账本</div><div class="v mono">tasks/' + esc(task.id || '') + '/</div></div>'
      + artHtml + '</div>';
  }

  const engine = st.engine || {};
  const busyInfo = st.busy || {};
  const alive = !!engine.alive;
  const stalled = String(engine.status || '') === 'stalled' || (!alive ? false : !!engine.stale);
  const pausedInfo = st.paused || null;
  const pausedHere = !!pausedInfo && (!pausedInfo.task || !task || pausedInfo.task === task.id);
  // 规划中/预审中还没有任务卡：给一张占位卡，别让右栏看起来是空的
  const planning = !!busyInfo.busy && (!task || !!busyInfo.task_stale);
  if(planning){
    const req = String(busyInfo.request || '').trim();
    const short = req.length > 40 ? req.slice(0, 40) + '…' : req;
    const phaseLabel = STAGE_LABEL[busyInfo.phase] || busyInfo.phase || '规划';
    taskBlock = '<div class="task-card pending">'
      + '<div class="tid">正在规划</div>'
      + '<div class="ttitle">' + esc(short || '（刚收到需求，正在理解）') + '</div>'
      + '<div class="chips"><span class="chip executing">' + esc(phaseLabel) + '</span>'
      +   '<span class="chip">引擎在跑 · 还没有任务卡</span></div>'
      + '<div class="kv"><div class="k">开始</div><div class="v mono">'
      +   esc(timeLabel(busyInfo.since) || '刚刚') + '</div>'
      +   '<div class="k">下一步</div><div class="v">拆完步骤就出任务卡</div></div>'
      + '</div>';
  }

  const cooling = Date.now() < controlCooldown;
  // 只要进程活着（alive）按钮就能点：心跳旧只是"在跑长任务"，不是死机
  const canPause = alive && !pausedInfo;
  const canResume = !!pausedInfo || (alive && !planning);
  const canStop = alive;
  const canRerun = alive && !!task && !planning;
  const ctrlBtn = (act, label, on, cls) => '<button'
    + (cls ? ' class="' + cls + '"' : '') + ' data-act="' + act + '"'
    + ((on && !cooling) ? '' : ' disabled') + '>' + label + '</button>';
  const ctrlHtml = '<div class="ctrl">'
    + ctrlBtn('pause', '暂停', canPause, '')
    + ctrlBtn('resume', '继续', canResume, pausedInfo ? 'hl' : '')
    + ctrlBtn('stop', '停止', canStop, 'danger')
    + ctrlBtn('rerun', '重新执行', canRerun, '')
    + '</div>';
  let ctrlHint;
  if(pausedHere){
    ctrlHint = '已暂停：点「继续」接着干，点「停止」就收工。';
  } else if(!alive){
    ctrlHint = '引擎没在跑（进程都不在了），按钮点不动；先起 worker 再发需求。';
  } else if(planning){
    ctrlHint = '正在规划：暂停/停止已经可用，等任务卡出来再点「继续/重新执行」。';
  } else if(stalled){
    ctrlHint = '心跳没刷新，但进程还在；点停止仍然能停掉。';
  } else if(task){
    ctrlHint = '引擎在跑，指令会在下一个步骤边界生效。';
  } else {
    ctrlHint = '引擎在跑，还没有任务卡：暂停/停止对将要派发的任务也有效。';
  }
  const pausedBanner = pausedHere
    ? '<div class="paused-banner"><b>已暂停</b>' + esc(pausedInfo.reason || '等你决定')
      + (pausedInfo.stage ? '（' + esc(STAGE_LABEL[pausedInfo.stage] || pausedInfo.stage) + '）' : '')
      + (pausedInfo.since ? ' · ' + esc(timeLabel(pausedInfo.since)) + ' 起' : '')
      + '</div>'
    : '';

  const verify = (task && task.verify_tail)
    ? '<pre class="block">' + esc(task.verify_tail) + '</pre>'
    : '<div class="empty">' + esc(changes.last_verify || '还没有验收记录') + '</div>';

  const files = Array.isArray(changes.files) ? changes.files : [];
  const prods = Array.isArray(task && task.products) ? task.products : [];
  /* 改动列表也做成蓝色文件名：点开就能看，右击能定位到文件夹——不然只能看到一串路径发呆 */
  const filesHtml = files.length
    ? '<div class="artifacts">' + files.map(name => {
        const hit = prods.find(p => p && (p.path === name || p.ref === 'workspace:' + name));
        return chipHtml('workspace:' + name, hit ? hit.in_scope !== false : true);
      }).join('') + '</div>'
    : '<div class="empty" style="padding:4px 0 8px">没有未提交改动</div>';
  const outside = prods.filter(p => p && p.in_scope === false);
  const outsideHtml = outside.length
    ? '<div class="insp-warn">任务卡没声明要改这几个文件，但它们确实被改了：'
      + outside.map(p => esc(p.path)).join('、') + '</div>'
    : '';
  const diffHtml = changes.diff_stat
    ? '<pre class="block">' + esc(changes.diff_stat) + '</pre>' : '';

  const wsNow = st.workspace || (st.session && st.session.workspace) || '';
  const wsKnown = (st.workspaces || []).find(w => w.path === wsNow);
  const wsPanel = '<section class="panel"><h3>工作区<span class="n">'
    + (wsNow ? (wsKnown && wsKnown.exists === false ? '路径不可用' : '已绑定') : '未绑定')
    + '</span></h3><div class="insp-ws">'
    + (wsNow
        ? '<div class="p">' + esc(wsNow) + '</div>'
          + '<button class="pick-btn" data-ws="1">更换工作区</button>'
        : '<div class="p">还没绑定工作区，先选一个仓库目录</div>'
          + '<button class="pick-btn" data-ws="1">选择工作区</button>')
    + '</div></section>';

  inspEl.innerHTML = ''
    + wsPanel
    + '<section class="panel"><h3>Agent 状态<span class="n">'
    +   (busyCount ? busyCount + ' 个在忙' : '全部空闲') + '</span></h3>'
    +   '<div class="agents">' + agentRows + '</div></section>'
    + '<section class="panel"><h3>当前任务<span class="n">'
    +   (!alive ? '引擎没在跑'
        : stalled ? '引擎在跑 · 心跳旧'
        : planning ? '引擎在跑 · 规划中'
        : '引擎在跑') + '</span></h3>'
    +   taskBlock + pausedBanner + ctrlHtml
    +   '<div class="ctrl-hint">' + esc(ctrlHint) + '</div></section>'
    + '<section class="panel"><h3>测试结果</h3>' + verify + '</section>'
    + '<section class="panel"><h3>代码改动<span class="n">' + files.length
    +   ' 个文件</span></h3>' + outsideHtml + filesHtml + diffHtml + '</section>';

  wireArtChips(inspEl);
  inspEl.querySelectorAll('.am[data-role]').forEach(btn => {
    btn.onclick = (ev) => { ev.stopPropagation(); openModelMenu(btn.dataset.role, btn); };
  });
  inspEl.querySelectorAll('.ctrl button[data-act]').forEach(btn => {
    btn.onclick = () => sendControl(btn.dataset.act);
  });
  inspEl.querySelectorAll('[data-ws]').forEach(btn => {
    btn.onclick = () => openPicker();
  });
}

function renderEngine(){
  const pill = $('engine'), txt = $('engine-txt');
  if(!pill || !txt) return;
  const engine = st.engine || {};
  const status = String(engine.status || (engine.alive ? 'running' : 'none'));
  const age = Math.max(0, Math.round(Number(engine.age) || 0));
  pill.classList.remove('engine-running', 'engine-stalled', 'engine-dead');
  let cls = 'engine-dead', label = '引擎没在跑';
  if(engine.alive){
    if(status === 'stalled'){
      cls = 'engine-stalled';
      label = '引擎在跑，心跳 ' + age + 's 没刷新（可能在跑长任务）';
    } else {
      cls = 'engine-running';
      label = status === 'busy' ? '引擎在跑（忙）' : '引擎在跑';
    }
  }
  pill.classList.add(cls);
  pill.classList.toggle('is-hot', !!engine.alive && status !== 'stalled');
  txt.textContent = label;
  pill.title = 'pid ' + (engine.pid || '—')
    + ' · 心跳 ' + (Number(engine.age) >= 0 ? age + 's 前' : '未知')
    + ' · 状态 ' + status;
}

function renderChrome(){
  const session = st.session || {};
  $('crumb').textContent = session.title || '新会话';
  const agents = st.agents || {};
  const busy = ROLES.filter(r => agents[r] === 'working');
  const live = $('live');
  const txt = $('live-txt');
  const paused = st.paused || null;
  live.classList.remove('busy', 'paused');
  if(paused){
    live.classList.add('paused');
    const wait = String(paused.stage || '') === 'approve' || /批准/.test(String(paused.reason || ''));
    txt.textContent = wait ? '已暂停：等你批准' : '已暂停';
  } else if(busy.length){
    live.classList.add('busy');
    txt.textContent = busy.map(r => agentOf(r).name).join(' / ') + ' 在干活';
  } else {
    txt.textContent = '空闲';
  }
  live.classList.toggle('is-hot', !paused && busy.length > 0);
  renderEngine();
  const note = $('store-note');
  const store = st.store || {};
  if(store.ready === false){
    note.textContent = '旧账本兼容模式：' + (store.note || 'session.py 未就绪');
    note.style.color = 'var(--warn)';
  } else {
    note.textContent = '会话存储：session.py 已就绪';
    note.style.color = 'var(--dim2)';
  }
}

function applyState(next, fresh){
  const wasNear = nearBottom();
  const sid = String((next.session && next.session.id) || '');
  if(fresh || sid !== syncedSession){
    msgs = [];
    syncedSession = sid;
    hasMoreBefore = false;
    gapState = null;
    resetStream();
    firstPaint = true;
  }
  st = Object.assign({}, st, next);
  activeId = sid;
  const incoming = next.messages || [];
  // 先按「合并前」的本地尾部判断有没有断层，再合并
  const gap = (!fresh && !healing) ? findGap(incoming) : null;
  mergeMessages(incoming);
  if(gap) healGap(gap, gap.boundary, MAX_HEAL_PAGES);
  const total = asInt(next.total_messages, msgs.length);
  hasMoreBefore = msgs.length > 0 && msgs.length < total;
  if(msgs.length){
    if($('welcome')) $('welcome').remove();
  } else if(!$('welcome') && welcomeHtml){
    streamEl.insertAdjacentHTML('beforeend', welcomeHtml);
  }
  renderMessages(msgs);
  ensureEarlier();
  renderSessions();
  renderWorkspace();
  renderInspector();
  renderChrome();
  if(wasNear || firstPaint){ scrollDown(); firstPaint = false; }
}

async function load(opts){
  const fresh = !!(opts && opts.fresh);
  const q = new URLSearchParams();
  if(activeId) q.set('session', activeId);
  if(!fresh && msgs.length > 20){
    q.set('after', String(msgs[msgs.length - 20].id));   // 只取新消息，别再整表拉一遍
  }
  q.set('limit', String(PAGE));
  const data = await apiGet('/api/state?' + q.toString());
  if(!data) return null;      // 没令牌/服务重启中：下一轮再试
  applyState(data, fresh);
  return data;
}

async function selectSession(id){
  if(!id || id === activeId) return;
  await apiPost('/api/session/select', {session_id: id});
  localTyping = null;
  activeId = id;
  await load({fresh: true});
}

async function newSession(){
  const btn = $('new-chat');
  btn.disabled = true;
  const workspace = st.workspace || (st.session && st.session.workspace) || '';
  const data = await apiPost('/api/session', {workspace: workspace}) || {};
  if(data.ok && data.session && data.session.id){
    localTyping = null;
    activeId = data.session.id;
    await load({fresh: true});
    input.focus();
  } else if(data && data.error){
    toast(data.error);
  }
  btn.disabled = false;
}

function showWsStatus(kind, text){
  wsStatus.className = 'ws-status' + (kind === true ? ' ok' : kind === false ? ' bad' : '');
  wsStatus.textContent = text || '';
}

async function bindWorkspace(){
  const raw = wsInput.value.trim();
  if(!raw){ showWsStatus(false, '请先填写工作区绝对路径'); return; }
  wsBtn.disabled = true;
  showWsStatus(null, '正在验证…');
  const data = await apiPost('/api/workspace', {path: raw});
  if(!data){
    showWsStatus(false, '需要访问令牌，先在解锁卡片里粘贴令牌');
  } else if(data.ok){
    showWsStatus(true, '已绑定：' + (data.workspace || raw)
      + (data.name && data.name !== data.workspace ? '（' + data.name + '）' : ''));
    rememberWorkspace(data.workspace || raw);
    await load();
  } else {
    showWsStatus(false, data.error || '这个路径不可用');
  }
  wsBtn.disabled = false;
}

/* ---------- 工作区选择：目录索引 + 体检 + 确认 ---------- */
const pickerEl = $('picker'), pickList = $('pick-list'), pickCrumbs = $('pick-crumbs');
const pickDrivesEl = $('pick-drives'), pickPathEl = $('pick-path');
const pickHealthEl = $('pick-health'), pickOk = $('pick-ok'), pickUp = $('picker-up');
const pickCloseBtn = $('picker-close');
const LAST_WS_KEY = 'team-last-workspace';

const pick = {open: false, view: '', parent: '', selPath: '', sel: null, entries: [],
              index: -1, drives: [], home: '', seq: 0};

function lastWorkspacePath(){
  try{ return String(localStorage.getItem(LAST_WS_KEY) || ''); }catch(e){ return ''; }
}
function rememberWorkspace(path){
  try{ if(path) localStorage.setItem(LAST_WS_KEY, path); }catch(e){}
}
const samePath = (a, b) => String(a || '').toLowerCase() === String(b || '').toLowerCase();

async function apiBrowse(path){
  const q = new URLSearchParams();
  if(path) q.set('path', path);
  try{
    const res = await fetch('/api/browse?' + q.toString(),
      {cache: 'no-store', credentials: 'same-origin'});
    if(res.status === 401){ showUnlock('需要访问令牌：把 room/web-token 的内容粘贴进来'); return null; }
    const data = await res.json().catch(() => null);
    if(!data){ return {ok: false, error: '服务端返回异常'}; }
    if(!data.ok && !data.error) data.error = '读不了这个目录';
    return data;
  }catch(e){ return {ok: false, error: '网络异常，请重试'}; }
}

/* 默认停在「上次用过的路径」或主目录，不从盘符根开始让人自己找 */
function pickerStartPath(){
  return st.workspace || lastWorkspacePath() || pick.home || '';
}

async function openPicker(start){
  if(!pickerEl) return;
  pickerEl.hidden = false;
  pick.open = true;
  document.body.classList.add('picking');
  pick.index = -1;
  pick.selPath = '';
  pick.sel = null;
  const from = start || pickerStartPath();
  if(from){
    await browseTo(from);
  } else {
    const root = await apiBrowse('');       // 不知道停哪儿时先问主目录，别从盘符根开始
    if(root && root.ok && root.home){ pick.home = root.home; await browseTo(root.home); }
    else await browseTo('');
  }
  if(pick.open && pickList) pickList.focus();
}

function closePicker(){
  if(!pick.open) return;
  pickerEl.hidden = true;
  pick.open = false;
  document.body.classList.remove('picking');
  pick.seq += 1;                       // 关了就丢掉还在路上的响应
}

async function browseTo(path){
  const seq = ++pick.seq;
  const data = await apiBrowse(path);
  if(seq !== pick.seq || !pick.open) return;
  if(!data) return;                    // 401 已经弹了解锁卡
  if(!data.ok){
    pickHealthEl.className = 'pick-health bad';
    pickHealthEl.textContent = (data.path ? data.path + '：' : '') + (data.error || '读不了这个目录');
    return;
  }
  pick.view = data.path || '';
  pick.parent = data.parent || '';
  pick.home = data.home || pick.home;
  pick.drives = data.drives || [];
  pick.entries = data.entries || [];
  pick.index = -1;
  if(pick.view){ pick.selPath = pick.view; pick.sel = data.selected || null; }
  else { pick.selPath = ''; pick.sel = null; }
  renderPicker(data);
}

function renderPicker(data){
  renderCrumbs(pick.view);
  renderDrives(pick.view);
  if(pickUp) pickUp.disabled = !pick.parent;

  if(!pick.entries.length){
    const tip = pick.view
      ? '这个目录下面没有子目录<br>要选它就直接点右下角「确认使用这个工作区」'
      : '没有可用的盘符';
    pickList.innerHTML = '<div class="pick-empty">' + tip + '</div>';
  } else {
    pickList.innerHTML = pick.entries.map((e, i) => {
      const noisy = e.noise ? ' noise' : '';
      const blocked = e.blocked ? ' blocked' : '';
      const icon = e.is_git
      ? '<svg class="ico" aria-hidden="true"><use href="#i-pkg"/></svg>'
      : '<svg class="ico" aria-hidden="true"><use href="#i-folder"/></svg>';
      // 协作台自己的目录：能进去看，但「选它」是灰的（后端也会拒，这里提前挡住）
      const hint = e.blocked ? (e.block_reason || '不能当工作区') : (e.hint || '');
      const use = e.blocked
        ? '<button class="use" disabled title="' + esc(hint) + '">不能选</button>'
        : '<button class="use" data-use="' + i + '" title="只选中，不进入">选它</button>';
      return '<div class="pick-row' + noisy + blocked + '" data-i="' + i + '" role="option"'
        + ' tabindex="-1" aria-selected="false" title="' + esc(e.path) + '">'
        + '<span class="ic">' + icon + '</span>'
        + '<span class="nm">' + esc(e.name) + '</span>'
        + '<span class="hh">' + esc(hint) + '</span>'
        + use + '</div>';
    }).join('');
    if(data.truncated) pickList.insertAdjacentHTML('beforeend',
      '<div class="pick-more">目录太多，只显示前 ' + pick.entries.length
      + ' 条（共 ' + data.dir_count + ' 个）</div>');
    pickList.querySelectorAll('.pick-row').forEach(row => {
      const i = Number(row.dataset.i);
      row.onclick = () => pickEnter(i);
      row.ondblclick = ev => { ev.preventDefault(); pickSelect(i); };
    });
    pickList.querySelectorAll('button[data-use]').forEach(btn => {
      btn.onclick = ev => { ev.stopPropagation(); pickSelect(Number(btn.dataset.use)); };
    });
  }
  renderPickFoot();
}

function renderCrumbs(path){
  if(!pickCrumbs) return;
  if(!path){ pickCrumbs.innerHTML = '<span class="sep">先选一个盘符或目录</span>'; return; }
  const parts = String(path).split(/[\\/]+/).filter(Boolean);
  let html = '', acc = '';
  if(/^[A-Za-z]:/.test(path)){
    const drive = parts[0] + '\\';
    acc = drive;
    html = '<button data-p="' + esc(drive) + '">' + esc(drive) + '</button>';
    parts.slice(1).forEach(seg => {
      acc = acc.replace(/[\\/]+$/, '') + '\\' + seg;
      html += '<span class="sep">›</span><button data-p="' + esc(acc) + '">' + esc(seg) + '</button>';
    });
  } else {
    html = '<button data-p="/">/</button>';
    acc = '/';
    parts.forEach(seg => {
      acc = acc.replace(/\/+$/, '') + '/' + seg;
      html += '<span class="sep">›</span><button data-p="' + esc(acc) + '">' + esc(seg) + '</button>';
    });
  }
  pickCrumbs.innerHTML = html;
  pickCrumbs.querySelectorAll('button[data-p]').forEach(btn => {
    btn.onclick = () => browseTo(btn.dataset.p);
  });
}

function renderDrives(path){
  if(!pickDrivesEl) return;
  const cur = String(path || '').slice(0, 2).toUpperCase();
  pickDrivesEl.innerHTML = (pick.drives || []).map(d => {
    const on = String(d).slice(0, 2).toUpperCase() === cur ? ' on' : '';
    return '<button class="pick-btn' + on + '" data-d="' + esc(d) + '">' + esc(d) + '</button>';
  }).join('');
  pickDrivesEl.querySelectorAll('button[data-d]').forEach(btn => {
    btn.onclick = () => browseTo(btn.dataset.d);
  });
}

/* 体检行：✔ git 仓库 main · 8 个文件 · 可写 / 普通文件夹 · 目录不存在 */
function renderPickFoot(message){
  if(!pickHealthEl) return;
  pickPathEl.textContent = pick.selPath || '还没选目录';
  const sel = pick.sel;
  const reasons = [];
  let text = '', cls = 'pick-health';
  if(message){
    text = message; cls += ' bad';
  } else if(!sel || !sel.exists){
    text = '✘ 目录不存在，换一个位置'; cls += ' bad';
    reasons.push('目录不存在');
  } else {
    const bits = [];
    if(sel.is_git){
      bits.push('✔ git 仓库' + (sel.git_branch ? ' ' + sel.git_branch : ''));
    } else {
      bits.push('普通文件夹（无 git）');
    }
    bits.push((sel.file_count || 0) + ' 个文件');
    bits.push(sel.writable ? '可写' : '不可写');
    if(!sel.writable) reasons.push('目录不可写，换一个位置');
    if(sel.has_tasks) bits.push('已有任务账本');
    if(sel.blocked){                       // 协作台自己的目录：后端也会拒，这里直接说清为什么
      const why = sel.block_reason || '这个目录不能当工作区';
      reasons.push(why);
      bits.push('✘ ' + why);
    }
    text = bits.join(' · ');
    cls += reasons.length ? ' bad' : ' ok';
  }
  pickHealthEl.className = cls;
  pickHealthEl.textContent = text;
  const can = !!(sel && sel.exists && sel.writable && !sel.blocked) && !message;
  pickOk.disabled = !can;
  pickOk.title = can ? '绑定这个目录作为工作区' : (reasons.join('；') || '先选一个可用的目录');
}

function paintRowPick(){
  pickList.querySelectorAll('.pick-row').forEach(el => {
    const on = Number(el.dataset.i) === pick.index;
    el.classList.toggle('cur', on);
    el.setAttribute('aria-selected', on ? 'true' : 'false');
    if(on) el.focus({preventScroll: true});
  });
}

async function pickEnter(i){
  const entry = pick.entries[i];
  if(!entry) return;
  pick.index = i;
  paintRowPick();
  await browseTo(entry.path);
  pickList.focus();
}

async function pickSelect(i){
  const entry = pick.entries[i];
  if(!entry) return;
  if(entry.blocked){                                 // 协作台自己的目录：只提示，不选中
    renderPickFoot(entry.block_reason || '这个目录不能当工作区，换一个仓库');
    return;
  }
  pick.index = i;
  paintRowPick();
  pick.selPath = entry.path;
  pick.sel = {exists: true, is_git: !!entry.is_git, git_branch: '',
    file_count: entry.file_count || 0, entry_count: entry.entry_count || 0,
    writable: entry.writable !== false, has_tasks: false,
    blocked: false, block_reason: ''};
  renderPickFoot();
  const data = await apiBrowse(entry.path);        // 补分支/账本这些只有体检才给的
  if(!pick.open || !samePath(pick.selPath, entry.path)) return;
  if(data && data.ok && data.selected){ pick.sel = data.selected; renderPickFoot(); }
}

async function pickConfirm(){
  if(pickOk.disabled || !pick.selPath) return;
  const path = pick.selPath;
  pickOk.disabled = true;
  pickOk.textContent = '绑定中…';
  const data = await apiPost('/api/workspace', {path: path});
  pickOk.textContent = '确认使用这个工作区';
  if(data && data.ok){
    rememberWorkspace(data.workspace || path);
    closePicker();
    showWsStatus(true, '已绑定：' + (data.workspace || path));
    toast('工作区已绑定：' + (data.workspace || path));
    await load();
    return;
  }
  renderPickFoot((data && data.error) || '绑定失败，换一个仓库试试');
}

function pickerKeys(ev){
  if(!pick.open) return;
  if(ev.key === 'Escape'){ ev.preventDefault(); closePicker(); return; }
  if(ev.key === 'ArrowDown' || ev.key === 'ArrowUp'){
    if(!pick.entries.length) return;
    ev.preventDefault();
    pick.index = ev.key === 'ArrowDown'
      ? Math.min(pick.entries.length - 1, pick.index + 1)
      : Math.max(0, pick.index - 1);
    paintRowPick();
    const row = pickList.querySelector('.pick-row[data-i="' + pick.index + '"]');
    if(row && row.scrollIntoView) row.scrollIntoView({block: 'nearest'});
    return;
  }
  if(ev.key === 'Enter'){
    if(ev.target === pickOk) return;                 // 按钮自己会响应
    ev.preventDefault();
    if(pick.index >= 0) pickEnter(pick.index);
    return;
  }
  if(ev.key === 'Backspace'){
    ev.preventDefault();
    if(pick.parent) browseTo(pick.parent);
  }
}

function toast(text){
  toastEl.textContent = text || '';
  toastEl.classList.add('show');
  setTimeout(() => toastEl.classList.remove('show'), 3200);
}

function autosize(){
  input.style.height = 'auto';
  input.style.height = Math.min(150, input.scrollHeight) + 'px';
  sendBtn.disabled = !input.value.trim();
}

async function sendControl(action){
  if(Date.now() < controlCooldown) return;          // 1 秒内不重复发
  const label = CTRL_LABEL[action] || action;
  const taskId = (st.task && st.task.id) || '';
  controlCooldown = Date.now() + 1000;
  renderInspector();                                // 立刻禁掉按钮，别让用户连点
  const data = await apiPost('/api/control', {action: action, task_id: taskId});
  if(data && data.ok){
    toast('已' + label + (taskId ? '（' + taskId + '）' : '') + '，引擎会在下一步生效');
    // 本地先回显，别等引擎写 pause.json 才变
    if(action === 'pause' && !st.paused){
      st.paused = {task: taskId, reason: '你点了暂停', stage: '', since: new Date().toISOString()};
    } else if((action === 'resume' || action === 'approve' || action === 'stop') && st.paused){
      st.paused = null;
    }
    renderChrome();
    renderInspector();
  } else {
    toast((data && data.error) || (label + '没发出去'));
  }
  await load();
}

function showEcho(text){
  echoSeq += 1;
  const el = buildMsg({
    id: 'echo-' + echoSeq, role: 'user', agent: 'user', content: text,
    status: 'pending', artifacts: [], created_at: new Date().toISOString()
  });
  el.classList.add('echo');
  streamEl.appendChild(el);
  scrollDown();
  return el;
}

async function send(){
  const text = input.value.trim();
  if(!text) return;
  input.value = '';
  autosize();
  sendBtn.disabled = true;
  const echo = showEcho(text);
  localTyping = {who: 'coordinator'};
  renderTyping();
  scrollDown();
  const data = await apiPost('/api/post', {session_id: activeId || '', content: text});
  if(!data || data.ok === false) toast((data && data.error) || '发送失败');
  const next = await load();
  if(msgs.some(m => m.role === 'user' && String(m.content || '').trim() === text)) echo.remove();
  else setTimeout(() => echo.remove(), 8000);
  localTyping = null;
  renderTyping();
  input.focus();
}

async function openArtifact(path){
  modal.hidden = false;
  modalPath.textContent = path;
  modalBody.textContent = '正在加载…';
  const data = await apiGet('/api/artifact?path=' + encodeURIComponent(path));
  if(!data){
    modalBody.textContent = '读取失败：需要访问令牌或服务不在';
    return;
  }
  if(data.ok){
    modalBody.textContent = data.content || '（空文件）';
    if(data.truncated) modalPath.textContent = path + '（内容过长，已截断）';
  } else {
    modalBody.textContent = data.error || '读取失败';
  }
}

function closeModal(){ modal.hidden = true; modalBody.textContent = ''; }

/* ---------- 产物：蓝色文件名（单击打开 / 右击出菜单） ---------- */
/* 引用格式两种：`workspace:<工作区相对路径>`（真正产出的文件）、`tasks/<id>/xxx.md`（证据）。
   显示一律只给文件名，完整路径放 title 和右键菜单里。 */
function refLabel(ref){
  const raw = String(ref || '');
  const rel = raw.startsWith('workspace:') ? raw.slice('workspace:'.length) : raw;
  return baseName(rel);
}
function refIsProduct(ref){ return String(ref || '').startsWith('workspace:'); }
function productScope(ref){
  const products = (st.task && Array.isArray(st.task.products)) ? st.task.products : [];
  const hit = products.find(p => p && p.ref === ref);
  return hit ? hit.in_scope !== false : true;      // 找不到就当清单内，别乱标
}
/* 造一个蓝色文件名。opts: {inScope} 不传就按当前任务卡查 */
function artifactChip(ref, opts){
  const o = opts || {};
  const inScope = (o.inScope === undefined) ? productScope(ref) : o.inScope;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'art';
  btn.dataset.art = ref;
  btn.title = (refIsProduct(ref) ? '产物：' : '证据：') + String(ref).replace(/^workspace:/, '')
    + '\n单击打开，右击更多';
  if(!inScope) btn.dataset.scope = 'out';
  btn.innerHTML = '<svg class="ico" aria-hidden="true"><use href="#i-file"/></svg>'
    + '<span class="nm">' + esc(refLabel(ref)) + '</span>'
    + (inScope ? '' : '<span class="tag">清单外</span>');
  btn.onclick = ev => { ev.preventDefault(); openArtifact(ref); };
  btn.oncontextmenu = ev => { ev.preventDefault(); openArtMenu(ref, ev); };
  return btn;
}
function fillArtifacts(host, refs, opts){
  if(!host) return;
  host.innerHTML = '';
  (refs || []).forEach(ref => host.appendChild(artifactChip(ref, opts)));
}
/* 同一套外观的 HTML 版本（右栏用 innerHTML 拼，图省事） */
function chipHtml(ref, inScope){
  const okScope = inScope !== false;
  return '<button type="button" class="art" data-art="' + esc(ref) + '"'
    + (okScope ? '' : ' data-scope="out"')
    + ' title="' + esc((refIsProduct(ref) ? '产物：' : '证据：') + String(ref).replace(/^workspace:/, ''))
    + '">'
    + '<svg class="ico" aria-hidden="true"><use href="#i-file"/></svg>'
    + '<span class="nm">' + esc(refLabel(ref)) + '</span>'
    + (okScope ? '' : '<span class="tag">清单外</span>')
    + '</button>';
}
/* 给容器里所有蓝色文件名接上单击/右击（innerHTML 拼出来的那些） */
function wireArtChips(root){
  if(!root) return;
  root.querySelectorAll('.art[data-art]').forEach(btn => {
    if(btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.onclick = ev => { ev.preventDefault(); openArtifact(btn.dataset.art); };
    btn.oncontextmenu = ev => { ev.preventDefault(); openArtMenu(btn.dataset.art, ev); };
  });
}

/* 产物右键菜单：位置跟鼠标，贴边时翻到左边/上边 */
let artMenuRef = '';
function artMenuEl(){ return $('art-menu'); }
function openArtMenu(ref, ev){
  const menu = artMenuEl();
  if(!menu) return;
  artMenuRef = ref;
  menu.hidden = false;
  const pad = 8;
  const w = menu.offsetWidth, h = menu.offsetHeight;
  let x = ev.clientX, y = ev.clientY;
  if(x + w + pad > window.innerWidth) x = Math.max(pad, window.innerWidth - w - pad);
  if(y + h + pad > window.innerHeight) y = Math.max(pad, window.innerHeight - h - pad);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
  const first = menu.querySelector('.mi');
  if(first) first.focus({preventScroll: true});
}
function closeArtMenu(){
  const menu = artMenuEl();
  if(menu) menu.hidden = true;
  artMenuRef = '';
}

/* 换模型菜单：点 Agent 行里的模型徽章弹出来。先选 Agent（Codex / Claude Code / …），再选它的模型。
   Agent 本机找不到（或想补模型）就进设置页：给一条终端命令 + 一段可以直接问那个 Agent 的话，
   用户把回答贴回来，服务端只做文本识别；点「保存并验证」才会真的跑一次 --version。
   没装 / 跑不起来 / 协作台没有执行适配的，一律置灰并写明原因，不让人选完才发现跑不起来。 */
let modelMenuRole = '';
let modelMenuAgent = '';
let modelMenuProvider = '';
let modelMenuRect = null;
let modelMenuParsed = null;
function modelMenuEl(){ return $('model-menu'); }
function modelAgents(){ return ((st.models || {}).agents) || []; }
function modelAgent(id){ return modelAgents().find(a => a.id === id) || {}; }
function modelRole(role){ return (((st.models || {}).roles) || []).find(r => r.role === role) || {}; }
function placeModelMenu(){
  const menu = modelMenuEl();
  if(!menu || menu.hidden) return;
  const pad = 8, rect = modelMenuRect || {left: 0, bottom: 0, top: 0};
  const w = menu.offsetWidth, h = menu.offsetHeight;
  let x = rect.left, y = rect.bottom + 4;
  if(x + w + pad > window.innerWidth) x = Math.max(pad, window.innerWidth - w - pad);
  if(y + h + pad > window.innerHeight) y = Math.max(pad, rect.top - h - 4);
  menu.style.left = x + 'px';
  menu.style.top = y + 'px';
}
function showModelMenu(html, wide){
  const menu = modelMenuEl();
  if(!menu) return;
  menu.innerHTML = html;
  menu.classList.toggle('wide', !!wide);
  menu.hidden = false;
  placeModelMenu();
}
function agentTag(a){
  if(a.id === 'api'){
    if(!((a.providers || []).length)) return '点这里添加';
    if(!a.works) return (a.version || '') + ' · 没读到 key';
    return a.version || '已配置';
  }
  if(!a.found) return '本机没装 · 点这里找';
  if(!a.runnable) return '已找到 · 协作台还不能用它执行';
  if(a.checked && !a.works) return '跑不起来';
  return a.version ? ('可用 · ' + a.version) : '已找到';
}
function openModelMenu(role, anchor){
  modelMenuRole = role;
  modelMenuAgent = '';
  modelMenuRect = anchor ? anchor.getBoundingClientRect() : null;
  renderModelAgents();
}
function renderModelAgents(){
  const me = modelRole(modelMenuRole);
  const reg = ((st.models || {}).registry) || [];
  const cur = reg.find(m => m.id === me.model) || {};
  const curCli = cur.cli || 'codex';
  let html = '<div class="mh">' + esc(me.name || modelMenuRole) + ' · 现在 ' + esc(me.model || '默认')
    + ' · 先选 Agent</div>';
  modelAgents().forEach(a => {
    const on = a.id === curCli && !!me.model;
    const bad = !a.found || !a.runnable || (a.checked && !a.works);
    const blocked = modelMenuRole === 'executor' && a.id === 'api';
    const why = blocked ? 'API 模型不能改文件，只能做协调/规划/复核' : (a.path || a.reason || '');
    html += '<button class="mi ' + (on ? 'on' : '') + '" data-agent="' + esc(a.id) + '"'
      + (blocked ? ' disabled' : '')
      + ' title="' + esc(why) + '">'
      + '<span>' + esc(a.label) + (on ? ' ✓' : '') + ' ›</span>'
      + '<span class="tag' + (bad || blocked ? ' warn' : '') + '">'
      + esc(blocked ? '不能当执行者' : agentTag(a)) + '</span></button>';
  });
  html += '<div class="menu-sep"></div>'
    + '<button class="mi" data-model=""><span>恢复配置默认</span>'
    + '<span class="tag">' + esc(me.source === '配置' ? '已是默认' : '') + '</span></button>';
  showModelMenu(html, false);
}
async function pickModelAgent(agent){
  modelMenuAgent = agent;
  if(agent === 'api'){ renderApiProviders(); return; }
  const a = modelAgent(agent);
  showModelMenu('<div class="mh">正在本机查找 ' + esc(a.label || agent) + '…</div>', false);
  const data = await apiPost('/api/agent-cli/detect', {agent: agent});
  if(data && data.models) st.models = data.models;
  if(modelMenuEl().hidden || modelMenuAgent !== agent) return;   // 等的时候用户已经关了/换了
  const row = (data && data.agent) || modelAgent(agent);
  if(row.found && row.runnable && row.works) renderModelList(agent);
  else renderAgentSetup(agent, row);
}
function renderModelList(agent, pid){
  const a = modelAgent(agent);
  const me = modelRole(modelMenuRole);
  const prov = pid ? apiProvider(pid) : {};
  const isApi = agent === 'api';
  const reg = (((st.models || {}).registry) || []).filter(m =>
    (m.cli || 'codex') === agent && (!pid || (m.provider || '') === pid));
  const apiBlocked = isApi && modelMenuRole === 'executor';
  let html = '<button class="mi back" data-back="' + (isApi ? 'providers' : 'agents') + '">'
    + '<span>‹ ' + (isApi ? 'API 提供方' : '选 Agent') + '</span></button>'
    + '<div class="mh">' + esc(isApi ? (prov.label || pid || 'API') : (a.label || agent)) + '</div>'
    + '<div class="mh mono">' + esc(isApi ? (prov.base_url || '') : (a.path || a.version || '')) + '</div>';
  if(apiBlocked){
    html += '<div class="p warn">API 模型只收发文字、不能改文件，不能当执行者；协调 / 规划 / 复核可以用。</div>';
  }
  if(modelMenuRole === 'coordinator'){
    html += '<div class="p dim">切协调者（大脑）会顺手重启引擎，约 3 秒；'
      + '有任务或正在回复时只切换、不重启，新模型从下一条消息生效。</div>';
  }
  if(!reg.length){
    html += '<div class="mh">' + esc(isApi
      ? '这个提供方还没有勾选模型。点下面「拉取模型」，勾选后保存。'
      : '还没有登记它的模型。点下面「补充模型」，按提示问它要模型 id。') + '</div>';
  }
  reg.forEach(m => {
    const on = m.id === me.model;
    const why = apiBlocked ? '不能当执行者' : (m.ready ? '' : (m.why || '本机没装 ' + (m.cli || '对应 CLI')));
    const blocked = apiBlocked || !m.ready;
    html += '<button class="mi ' + (on ? 'on' : '') + '" data-model="' + esc(m.id) + '"'
      + (blocked ? ' disabled' : '')
      + ' title="' + esc(why || ((m.aliases || []).join(' / ') + (m.note ? ' · ' + m.note : ''))) + '">'
      + '<span>' + esc(isApi ? (m.id.includes(':') ? m.id.slice(m.id.indexOf(':') + 1) : m.id) : m.id)
      + (on ? ' ✓' : '') + '</span>'
      + '<span class="tag">' + esc(why ? (apiBlocked ? '不能当执行者' : '不可用') : (m.note || '')) + '</span></button>';
  });
  html += '<div class="menu-sep"></div>';
  if(isApi){
    html += '<button class="mi" data-provider-setup="' + esc(pid || '') + '"><span>'
      + (pid ? '编辑地址 / 拉取模型…' : '设置提供方…') + '</span></button>';
  } else {
    html += '<button class="mi" data-setup="1"><span>补充模型 / 换路径…</span></button>';
  }
  showModelMenu(html, true);
}

/* ---------- API 通道（OpenAI 兼容）：选项组、提供方、拉取模型 ----------
   key 不经过协作台：界面上没有填 key 的输入框，只填"key 在哪个环境变量里"。
   文案里那句"设置后要重启协作台"必须一直在——`setx` 之后正在跑的进程读不到新变量。 */
function apiProviders(){ const a = modelAgent('api'); return (a && a.providers) || []; }
function apiProvider(pid){ return apiProviders().find(p => p.id === pid) || {}; }
function renderApiProviders(){
  modelMenuProvider = '';
  const provs = apiProviders();
  const roleName = (modelRole(modelMenuRole) || {}).name || modelMenuRole;
  let html = '<button class="mi back" data-back="agents"><span>‹ 选 Agent</span></button>'
    + '<div class="mh">API（OpenAI 兼容）· ' + esc(String(provs.length)) + ' 个提供方 · 给 ' + esc(roleName) + '</div>';
  if(!provs.length){
    html += '<div class="p">还没有提供方。加一个 OpenAI 兼容地址（DeepSeek / Ollama / 中转站），'
      + 'key 放进环境变量——协作台不保存 key。</div>';
  }
  provs.forEach(p => {
    const n = (p.models || []).length;
    const keyTxt = p.key_source === 'env' ? ('已读到（环境变量 ' + p.api_key_env + '）')
      : (p.key_source === 'file' ? 'key 已保存在本机' : ('没读到 ' + (p.api_key_env || 'key')));
    html += '<button class="mi" data-provider="' + esc(p.id) + '" title="' + esc(p.base_url || '') + '">'
      + '<span>' + esc(p.label || p.id) + (n ? '' : ' · 还没勾模型') + ' ›</span>'
      + '<span class="tag' + (p.has_key ? '' : ' warn') + '">' + esc(n + ' 个模型 · ' + keyTxt) + '</span></button>';
  });
  if(provs.some(p => !p.has_key)){
    html += '<div class="p warn">还没有 key：在提供方里直接贴（立即生效、不用重启），'
      + '或者设好环境变量再重启协作台。</div>';
  }
  html += '<div class="menu-sep"></div>'
    + '<button class="mi" data-provider-add="1"><span>＋ 添加提供方</span></button>';
  showModelMenu(html, true);
}
function renderProviderModels(pid){
  modelMenuProvider = pid;
  renderModelList('api', pid);
}
function applyApiPreset(key){
  const table = {
    deepseek: {label: 'DeepSeek', id: 'deepseek', url: 'https://api.deepseek.com', env: 'DEEPSEEK_API_KEY'},
    ollama: {label: 'Ollama', id: 'ollama', url: 'http://127.0.0.1:11434/v1', env: 'OLLAMA_API_KEY'},
  };
  const p = table[key];
  if(!p) return;
  const setId = $('api-id');
  if(setId && !setId.disabled) setId.value = p.id;
  if($('api-label')) $('api-label').value = p.label;
  if($('api-url')) $('api-url').value = p.url;
  if($('api-env')) $('api-env').value = p.env;
}
function renderApiSetup(pid){
  modelMenuProvider = pid || '';
  const p = pid ? apiProvider(pid) : {};
  const presets = [['', '自定义 / 手动填'], ['deepseek', 'DeepSeek（api.deepseek.com）'], ['ollama', 'Ollama（本机 11434）']];
  let html = '<button class="mi back" data-provider-back="1"><span>‹ API 提供方</span></button>'
    + '<div class="mh">' + (pid ? '编辑 API 提供方 · ' + esc(pid) : '添加 API 提供方') + '</div>'
    + '<div class="setup">'
    + '<label class="step">预设<select id="api-preset">'
    + presets.map(x => '<option value="' + esc(x[0]) + '">' + esc(x[1]) + '</option>').join('')
    + '</select></label>'
    + '<label class="step">显示名<input id="api-label" value="' + esc(p.label || '')
    + '" placeholder="DeepSeek" spellcheck="false"></label>'
    + '<label class="step">提供方 id（模型 id 的前缀）<input id="api-id" value="' + esc(pid || '')
    + '" placeholder="deepseek" spellcheck="false"' + (pid ? ' disabled' : '') + '></label>'
    + '<label class="step">地址<input id="api-url" value="' + esc(p.base_url || '')
    + '" placeholder="https://api.deepseek.com" spellcheck="false"></label>'
    + '<label class="step">key 所在的环境变量（可留空，自动用 ' + esc((pid || 'deepseek').toUpperCase() + '_API_KEY')
    + '）<input id="api-env" value="' + esc(p.api_key_env || '')
    + '" placeholder="留空自动填，如 DEEPSEEK_API_KEY" spellcheck="false"></label>'
    + '<div class="p dim">两种给 key 的方式，选一种就行：① 设环境变量（<code>setx</code> 之后要重启协作台）；'
    + '② 在下面直接贴——只存本机 <code>room/api_keys.json</code>（不进 git、不回显），保存后<strong>立即生效、不用重启</strong>。</div>'
    + '<label class="step">直接贴 key（存本机；和提供方一起保存）<input id="api-key" type="password"'
    + ' autocomplete="off" spellcheck="false" placeholder="sk-…（贴在这里，不要贴到上面变量名栏）"></label>'
    + '<div class="row"><button class="pick-btn" data-provider-save="1">保存</button>'
    + '<button class="pick-btn" data-provider-fetch="1"' + (pid ? '' : ' disabled') + '>拉取模型</button></div>';
  if(pid){
    const src = p.key_source === 'env' ? ('✓ 已读到（环境变量 ' + p.api_key_env + '）')
      : (p.key_source === 'file' ? '✓ 已保存在本机（立即生效）'
        : ('✗ 还没有 key：环境变量 ' + (p.api_key_env || '（未填写）') + ' 为空，本机也没保存过'));
    html += '<div class="p' + (p.has_key ? ' dim' : ' warn') + '">' + esc(src) + '</div>'
      + '<div class="row"><button class="pick-btn" data-provider-key="1">单独保存 key</button>'
      + (p.key_source === 'file' ? '<button class="pick-btn" data-provider-clear-key="1">清除本机 key</button>' : '')
      + '</div>';
  }
  html += '<div id="api-models"></div>';
  if(pid){
    html += '<div class="row"><button class="pick-btn" data-provider-delete="1">删除提供方</button></div>';
  }
  html += '</div>';
  showModelMenu(html, true);
}
async function saveApiProvider(){
  const val = id => { const el = $(id); return el ? String(el.value || '').trim() : ''; };
  const pid = val('api-id');
  const data = await apiPost('/api/api-provider/save', {id: pid, label: val('api-label'),
    base_url: val('api-url'), api_key_env: val('api-env'), key: val('api-key')});
  if(data && data.models) st.models = data.models;
  if(!data || !data.ok){ toast((data && data.error) || '保存失败'); return; }
  toast(data.message || '已保存');
  renderApiSetup(pid.toLowerCase());
}
async function fetchApiModels(){
  const pid = modelMenuProvider;
  const box = $('api-models');
  if(box) box.innerHTML = '<div class="mh">正在问它有哪些模型…</div>';
  const data = await apiPost('/api/api-provider/fetch', {id: pid});
  if(!box || modelMenuProvider !== pid) return;      // 等的时候用户已经换了页
  if(!data || !data.ok){
    box.innerHTML = '<div class="p warn">' + esc((data && data.error) || '拉取失败') + '</div>';
    placeModelMenu();
    return;
  }
  const saved = new Set((pid ? apiProvider(pid) : {}).models || []);
  const ids = data.ids || [];
  let html = '<div class="mh">勾选要用的模型（' + esc(String(ids.length)) + ' 个）</div>';
  if(!ids.length) html += '<div class="p dim">它没返回可用的模型 id。</div>';
  ids.forEach(m => {
    html += '<label class="pick"><input type="checkbox" name="api-model" value="' + esc(m) + '"'
      + (saved.has(m) ? ' checked' : '') + '> <span class="mono">' + esc(m) + '</span></label>';
  });
  html += '<div class="row"><button class="pick-btn" data-provider-models="1"'
    + (ids.length ? '' : ' disabled') + '>保存勾选</button></div>';
  box.innerHTML = html;
  placeModelMenu();
}
async function saveApiModels(){
  const menu = modelMenuEl();
  if(!menu) return;
  const ids = Array.from(menu.querySelectorAll('input[name="api-model"]:checked')).map(x => x.value);
  const pid = modelMenuProvider;
  const data = await apiPost('/api/api-provider/models', {id: pid, models: ids});
  if(data && data.models) st.models = data.models;
  if(!data || !data.ok){ toast((data && data.error) || '保存失败'); return; }
  toast(data.message || '已保存');
  if(modelMenuProvider === pid) renderProviderModels(pid);
}
async function deleteApiProvider(){
  const pid = modelMenuProvider;
  if(!pid) return;
  if(!window.confirm('删除 API 提供方「' + (apiProvider(pid).label || pid) + '」？已勾选的模型会一起消失。')) return;
  const data = await apiPost('/api/api-provider/delete', {id: pid});
  if(data && data.models) st.models = data.models;
  if(!data || !data.ok){ toast((data && data.error) || '删除失败'); return; }
  toast(data.message || '已删除');
  renderApiProviders();
}
async function saveApiKey(){
  const pid = modelMenuProvider;
  const el = $('api-key');
  const key = el ? String(el.value || '').trim() : '';
  if(!pid) return;
  if(!key){ toast('先把 key 粘进来'); return; }
  const data = await apiPost('/api/api-provider/key', {id: pid, key: key});
  if(data && data.models) st.models = data.models;
  if(!data || !data.ok){ toast((data && data.error) || '保存 key 失败'); return; }
  toast(data.message || 'key 已保存，立即生效');
  renderApiSetup(pid);       // 重画会把输入框整个换掉，key 不在页面上停留
}
async function clearApiKey(){
  const pid = modelMenuProvider;
  if(!pid) return;
  const data = await apiPost('/api/api-provider/clear-key', {id: pid});
  if(data && data.models) st.models = data.models;
  if(!data || !data.ok){ toast((data && data.error) || '清除失败'); return; }
  toast(data.message || '已清除');
  renderApiSetup(pid);
}
function renderAgentSetup(agent, row){
  const a = row || modelAgent(agent);
  const g = a.guide || {};
  modelMenuAgent = agent;
  modelMenuParsed = null;
  const state = !a.found ? '本机没找到 ' + a.label + '（PATH 和常见安装目录都看过了）。'
    : (a.reason ? a.reason : '找到了：' + a.path);
  const html = '<button class="mi back" data-back="agents"><span>‹ 选 Agent</span></button>'
    + '<div class="mh">' + esc(a.label || agent) + ' · 设置</div>'
    + '<div class="setup">'
    + '<div class="p' + (a.found && !a.reason ? '' : ' warn') + '">' + esc(state) + '</div>'
    + '<div class="step">① 在终端里跑：<code>' + esc(g.command || '') + '</code></div>'
    + '<div class="step">② 或者把下面这段话发给 ' + esc(a.label) + '，让它自己告诉你：</div>'
    + '<pre class="ask" id="agent-ask">' + esc(g.ask || '') + '</pre>'
    + '<button class="pick-btn" data-copy="1">复制这段话</button>'
    + '<div class="step">③ 把终端输出或它的回答整段贴进来：</div>'
    + '<textarea id="agent-paste" rows="5" spellcheck="false"'
    + ' placeholder="例如：C:\\Users\\你\\AppData\\Roaming\\npm\\claude.cmd&#10;claude-sonnet-4-5"></textarea>'
    + '<div class="row"><button class="pick-btn" data-parse="1">识别</button>'
    + '<button class="pick-btn" data-redetect="1">重新检测</button></div>'
    + '<div id="agent-parsed"></div>'
    + '</div>';
  showModelMenu(html, true);
}
async function copyAgentAsk(){
  const text = ($('agent-ask') || {}).textContent || '';
  try { await navigator.clipboard.writeText(text); toast('已复制，去问它吧'); }
  catch(e) { window.prompt('复制这段话：', text); }
}
async function parseAgentPaste(){
  const box = $('agent-parsed');
  const text = ($('agent-paste') || {}).value || '';
  if(!text.trim()){ toast('先把回答贴进来'); return; }
  const data = await apiPost('/api/agent-cli/parse', {agent: modelMenuAgent, text: text});
  if(!box) return;
  if(!data || data.ok === false){ box.innerHTML = '<div class="p warn">' + esc((data && data.error) || '识别失败') + '</div>'; return; }
  modelMenuParsed = data;
  const a = modelAgent(modelMenuAgent);
  const first = Math.max(0, (data.paths || []).findIndex(p => p.usable && p.exists));
  let html = '<div class="mh">识别结果（勾选后保存）</div>';
  if(a.found){
    html += '<label class="pick"><input type="radio" name="agent-path" value="" '
      + ((data.paths || []).length ? '' : 'checked') + '> 路径不变（' + esc(a.path) + '）</label>';
  }
  (data.paths || []).forEach((p, i) => {
    const note = !p.usable ? '不能直接执行' : (!p.exists ? '本机没有这个文件' : '');
    html += '<label class="pick' + (note ? ' dim' : '') + '"><input type="radio" name="agent-path" value="'
      + esc(p.path) + '"' + (i === first && p.usable ? ' checked' : '') + (p.usable ? '' : ' disabled') + '> '
      + '<span class="mono">' + esc(p.path) + '</span>' + (note ? ' · ' + esc(note) : '') + '</label>';
  });
  (data.models || []).forEach(m => {
    html += '<label class="pick"><input type="checkbox" name="agent-model" value="' + esc(m) + '" checked> '
      + '<span class="mono">' + esc(m) + '</span></label>';
  });
  if((data.other_models || []).length){
    html += '<div class="p dim">这些看着是别家的模型，没收：' + esc(data.other_models.join('、')) + '</div>';
  }
  if((data.ignored_paths || []).length){
    html += '<div class="p dim">这些是别的 Agent 的路径，没收：' + esc(data.ignored_paths.join('、')) + '</div>';
  }
  if(data.hint) html += '<div class="p warn">' + esc(data.hint) + '</div>';
  const can = (data.paths || []).some(p => p.usable) || ((data.models || []).length && a.found);
  html += '<div class="row"><button class="pick-btn" data-save="1"' + (can ? '' : ' disabled') + '>保存并验证</button></div>';
  box.innerHTML = html;
  placeModelMenu();
}
async function saveAgentCli(){
  const menu = modelMenuEl();
  const picked = menu.querySelector('input[name="agent-path"]:checked');
  const models = Array.from(menu.querySelectorAll('input[name="agent-model"]:checked')).map(x => x.value);
  const agent = modelMenuAgent;
  const data = await apiPost('/api/agent-cli/save', {agent: agent, path: picked ? picked.value : '', models: models});
  if(data && data.models) st.models = data.models;
  if(!data || !data.ok){ toast((data && data.error) || '保存失败'); return; }
  toast(data.message || '已保存');
  if(!menu.hidden && modelMenuAgent === agent){
    const row = data.agent || modelAgent(agent);
    if(row.runnable && row.works) renderModelList(agent);
    else renderAgentSetup(agent, row);
  }
}
function closeModelMenu(){
  const menu = modelMenuEl();
  if(menu) menu.hidden = true;
  modelMenuRole = '';
  modelMenuAgent = '';
  modelMenuProvider = '';
  modelMenuParsed = null;
}
if(modelMenuEl()){
  // 一个委托处理全部按钮：菜单内容每次都整块重画，逐个绑 onclick 容易漏
  modelMenuEl().addEventListener('click', ev => {
    const btn = ev.target.closest && ev.target.closest('button');
    if(!btn || btn.disabled) return;
    const role = modelMenuRole;
    if(btn.dataset.agent) pickModelAgent(btn.dataset.agent);
    else if(btn.dataset.back === 'providers') renderApiProviders();
    else if(btn.dataset.back) renderModelAgents();
    else if(btn.dataset.providerAdd) renderApiSetup('');
    else if(btn.dataset.providerSetup !== undefined) renderApiSetup(btn.dataset.providerSetup);
    else if(btn.dataset.provider) renderProviderModels(btn.dataset.provider);
    else if(btn.dataset.providerBack !== undefined) renderApiProviders();
    else if(btn.dataset.providerSave) saveApiProvider();
    else if(btn.dataset.providerKey) saveApiKey();
    else if(btn.dataset.providerClearKey !== undefined) clearApiKey();
    else if(btn.dataset.providerFetch) fetchApiModels();
    else if(btn.dataset.providerModels) saveApiModels();
    else if(btn.dataset.providerDelete !== undefined) deleteApiProvider();
    else if(btn.dataset.setup) renderAgentSetup(modelMenuAgent);
    else if(btn.dataset.copy) copyAgentAsk();
    else if(btn.dataset.parse) parseAgentPaste();
    else if(btn.dataset.redetect) pickModelAgent(modelMenuAgent);
    else if(btn.dataset.save) saveAgentCli();
    else if(btn.dataset.model !== undefined) switchModel(role, btn.dataset.model);
  });
  // 预设下拉只填表，不发请求
  modelMenuEl().addEventListener('change', ev => {
    if(ev.target && ev.target.id === 'api-preset') applyApiPreset(ev.target.value);
  });
}
async function switchModel(role, model){
  const isBrain = role === 'coordinator';
  if(isBrain){
    const ok = window.confirm('切换协调者（大脑）会顺手重启引擎，约 3 秒。'
      + '有任务或回复在跑时只切换、不重启，新模型从下一条消息生效。继续？');
    if(!ok) return;
    toast('正在切换并重启引擎，约 3 秒…');
  }
  closeModelMenu();
  const data = await apiPost('/api/set-model', {role: role, model: model});
  if(data && data.ok){
    toast(data.message || '模型已切换');
    if(data.models) st.models = data.models;
    renderInspector();
  } else {
    toast((data && data.error) || '切换失败');
  }
}
async function artAction(act){
  const ref = artMenuRef;
  closeArtMenu();
  if(!ref) return;
  if(act === 'preview'){ openArtifact(ref); return; }
  if(act === 'copy'){
    const data = await apiGet('/api/artifact?path=' + encodeURIComponent(ref));
    const full = (data && data.path) || String(ref).replace(/^workspace:/, '');
    try {
      await navigator.clipboard.writeText(full);
      toast('已复制路径：' + full);
    } catch(e) {
      window.prompt('复制这个路径：', full);
    }
    return;
  }
  const data = await apiPost('/api/artifact/' + act, {path: ref});
  if(!data || data.ok === false) toast((data && data.error) || '操作失败');
  else toast(act === 'reveal' ? '已在资源管理器中定位' : '已用默认程序打开');
}
if(artMenuEl()){
  artMenuEl().addEventListener('click', ev => {
    const btn = ev.target.closest && ev.target.closest('.mi');
    if(btn) artAction(btn.dataset.act);
  });
}
document.addEventListener('click', ev => {
  const menu = artMenuEl();
  if(menu && !menu.hidden && !(ev.target.closest && ev.target.closest('#art-menu'))) closeArtMenu();
  const mm = modelMenuEl();
  // 菜单内容点完会整块重画，被点的按钮这时已经脱离文档，closest 找不到 #model-menu；
  // composedPath 记的是派发那一刻的路径，所以用它判断"点在菜单里面"
  const path = ev.composedPath ? ev.composedPath() : [];
  const inMenu = mm && (path.includes(mm) || (ev.target.closest && ev.target.closest('.am[data-role]')));
  if(mm && !mm.hidden && !inMenu) closeModelMenu();
});
document.addEventListener('keydown', ev => {
  if(ev.key !== 'Escape') return;
  const mm = modelMenuEl();
  if(mm && !mm.hidden){ ev.preventDefault(); ev.stopImmediatePropagation(); closeModelMenu(); return; }
  const menu = artMenuEl();
  if(!menu || menu.hidden) return;
  ev.preventDefault(); ev.stopImmediatePropagation(); closeArtMenu();
});
window.addEventListener('resize', () => {
  if(artMenuEl() && !artMenuEl().hidden) closeArtMenu();
  if(modelMenuEl() && !modelMenuEl().hidden) closeModelMenu();
});

$('new-chat').onclick = newSession;
wsBtn.onclick = bindWorkspace;
wsInput.addEventListener('keydown', e => {
  if(pick.open) return;                      // 对话框开着时回车归对话框
  if(e.key === 'Enter'){ e.preventDefault(); bindWorkspace(); }
});
const wsBrowseBtn = $('ws-browse');
if(wsBrowseBtn) wsBrowseBtn.onclick = () => openPicker();
if(pickUp) pickUp.onclick = () => { if(pick.parent) browseTo(pick.parent); };
if(pickCloseBtn) pickCloseBtn.onclick = closePicker;
if(pickOk) pickOk.onclick = pickConfirm;
if(pickerEl) pickerEl.addEventListener('click', ev => { if(ev.target === pickerEl) closePicker(); });
sendBtn.onclick = send;
input.addEventListener('input', autosize);
input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
});
$('modal-close').onclick = closeModal;
modal.addEventListener('click', e => { if(e.target === modal) closeModal(); });
document.addEventListener('keydown', e => { if(e.key === 'Escape' && !modal.hidden) closeModal(); });
document.addEventListener('keydown', pickerKeys);

/* ---------- 会话归档 / 删除的接线 ---------- */
$('menu-rename').onclick = () => { const id = menuSessId; if(id) startRename(id); };
$('menu-archive').onclick = () => { const id = menuSessId; if(id) sessArchive(id, true); };
$('menu-delete').onclick = () => { const id = menuSessId; if(id) openConfirm(id); };
$('confirm-cancel').onclick = () => closeConfirm(true);
$('confirm-ok').onclick = runConfirm;
if(confirmEl) confirmEl.addEventListener('click', ev => {
  if(ev.target === confirmEl) closeConfirm(true);      // 点遮罩 = 取消
});
document.addEventListener('click', ev => {
  if(sessMenu.hidden) return;
  const t = ev.target;
  if(t.closest && (t.closest('#sess-menu') || t.closest('.sess-more'))) return;
  closeSessMenu(false);                                // 点别处收起菜单
}, true);
window.addEventListener('resize', () => closeSessMenu(false));
sessionsEl.addEventListener('scroll', () => closeSessMenu(false), {passive: true});
document.addEventListener('keydown', sessKeys);

/* ---------- 回收站的接线 ---------- */
if(trashEntry) trashEntry.onclick = openTrash;
if(trashCloseBtn) trashCloseBtn.onclick = () => closeTrash(true);
if(trashEmptyBtn) trashEmptyBtn.onclick = askEmptyTrash;
if(trashEl) trashEl.addEventListener('click', ev => {
  if(ev.target === trashEl) closeTrash(true);          // 点遮罩 = 关闭
});
document.addEventListener('keydown', ev => {
  // 两层叠着时 Esc 只收最上面那层：确认框已经 preventDefault 处理过的，这里不再管
  if(ev.defaultPrevented || (confirmEl && !confirmEl.hidden)) return;
  if(ev.key === 'Escape' && trashEl && !trashEl.hidden) closeTrash(true);
});

/* ---------- 高科技 HUD：档位（关 / 标准 / 加强）+ 状态接线 ---------- */
const FX_KEY = 'team-fx-level';
const FX_LEVELS = [{id:'off', label:'特效·关'}, {id:'std', label:'特效·标准'}, {id:'max', label:'特效·加强'}];
function fxStored(){
  // URL 上带了 #fx=max / #fx=off 就以它为准（截图和排障时不用手点）
  const forced = /(?:^|[#&])fx=(off|std|max)\b/.exec(String(location.hash || ''));
  if(forced) return forced[1];
  try{ const v = localStorage.getItem(FX_KEY); if(FX_LEVELS.some(l => l.id === v)) return v; }catch(e){}
  return 'std';
}
/* 环境场要读的「仪器状态」：引擎死没死、有没有人在干活 */
function fxState(){
  const engine = st.engine || {}, agents = st.agents || {};
  const busy = !!st.paused || ROLES.some(r => agents[r] === 'working');
  return {busy, engineAlive: !!engine.alive};
}
function fxApply(level, announce){
  const hit = FX_LEVELS.find(l => l.id === level) || FX_LEVELS[1];
  const root = document.body;
  root.classList.toggle('fx-on', hit.id !== 'off');
  root.classList.toggle('fx-std', hit.id === 'std');
  root.classList.toggle('fx-max', hit.id === 'max');
  const btn = $('fx-toggle');
  if(btn){
    const lbl = btn.querySelector('.fx-lbl');
    if(lbl) lbl.textContent = hit.label;
    btn.setAttribute('aria-pressed', String(hit.id !== 'off'));
  }
  try{ localStorage.setItem(FX_KEY, hit.id); }catch(e){}
  // fx.js 万一没加载/报错，整页照常能用，只是没有特效
  if(window.FX && typeof FX.setLevel === 'function'){
    try{ FX.setLevel(hit.id); }catch(e){}
  }
  if(announce) toast(hit.id === 'off' ? '特效已经关掉了' : '特效档位：' + hit.label.slice(3));
}
function fxBoot(){
  fxApply(fxStored(), false);
  const btn = $('fx-toggle');
  if(btn){
    btn.onclick = () => {
      const i = FX_LEVELS.findIndex(l => l.id === fxStored());
      fxApply(FX_LEVELS[(i + 1) % FX_LEVELS.length].id, true);
    };
  }
  if(window.FX && typeof FX.init === 'function'){
    try{ FX.init({level: fxStored(), zField: 60, zSpark: 62, getState: fxState}); }catch(e){}
  }
}

let lastClock = 0;
function tick(){
  load();
  lastClock += 1;
}

/* ---------- 启动：有 Cookie 就进工作台，没有就先让用户粘贴（POST /api/unlock 换 Cookie） ---------- */
async function submitUnlock(){
  const value = String(unlockInput.value || '').trim();
  if(!value){ unlockErr.textContent = '先粘贴访问令牌'; return; }
  unlockBtn.disabled = true;
  unlockErr.textContent = '';
  const data = await apiPost('/api/unlock', {token: value});
  unlockBtn.disabled = false;
  if(!data){ unlockErr.textContent = '服务没响应，稍后再试'; return; }
  if(!data.ok){ unlockErr.textContent = data.error || '令牌不对'; return; }
  unlockInput.value = '';
  location.reload();          // Cookie 已经种下，刷新一次直接进工作台
}

async function startWorkbench(){
  const data = await load();
  if(!data){ showUnlock('需要访问令牌：把 room/web-token 的内容粘贴进来'); return; }
  unlockEl.hidden = true;
  document.body.classList.add('ready');
  // 首屏那次 applyState 时 .app 还没显示（display:none），滚动是空操作；
  // 显示出来补一次，长会话才不会停在最上面、把最新几条裁在下面。
  scrollDown();
  fxBoot();
  // 空会话才看得到欢迎语：解码入场只在这时候演一次（有历史消息时 #welcome 已经被换掉了）
  const h1 = document.querySelector('#welcome h1');
  if(h1 && !h1.dataset.fxDone && window.FX && typeof FX.scramble === 'function'){
    h1.dataset.fxDone = '1';
    try{ FX.scramble(h1); }catch(e){}
  }
  autosize();
  if(!startWorkbench.timer){
    startWorkbench.timer = setInterval(tick, 1000);
  }
  input.focus();
}

unlockBtn.onclick = submitUnlock;
unlockInput.addEventListener('keydown', e => {
  if(e.key === 'Enter'){ e.preventDefault(); submitUnlock(); }
});

unlockErr.textContent = __UNLOCK_MSG__;
startWorkbench();

/* 页面开着就一直在（每秒打一次 /api/state，服务端据此判断在线）。
   关页面/关标签页时尽力发一次告别；凭证在 Cookie 里，beacon 同源会自动带上。 */
function sayBye(){
  try{
    const body = JSON.stringify({reason: 'page-unloaded'});
    if(navigator.sendBeacon){
      navigator.sendBeacon('/api/bye', new Blob([body], {type: 'application/json'}));
    } else {
      fetch('/api/bye', {method:'POST', body, headers: API_HEADERS, credentials:'same-origin', keepalive: true});
    }
  }catch(e){}
}
window.addEventListener('pagehide', sayBye);
window.addEventListener('beforeunload', sayBye);
</script>
</body></html>"""


def _write_session_state(collab: str, reason: str = "") -> None:
    payload = {
        "collab": collab,
        "reason": reason,
        "at": _now(),
        "root": str(ROOT),
    }
    try:
        ROOM.mkdir(parents=True, exist_ok=True)
        SESSION_STATE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _write_project(path: Path) -> dict:
    payload = {"workspace": str(path), "name": path.name or str(path), "updated_at": _now()}
    try:
        ROOM.mkdir(parents=True, exist_ok=True)
        PROJECT_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return payload


def _nudge() -> None:
    try:
        ROOM.mkdir(parents=True, exist_ok=True)
        NUDGE_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _stop_worker() -> int:
    """按锁里的 pid 停掉工作流引擎（跨平台：Windows 用 taskkill，其余用 SIGTERM）。"""
    try:
        info = json.loads((ROOM / "worker.lock").read_text(encoding="utf-8"))
        pid = int(info.get("pid") or 0)
    except (OSError, ValueError, json.JSONDecodeError):
        return 0
    if pid <= 0:
        return 0
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, errors="replace",
                           creationflags=quiet_flags())
        else:
            os.kill(pid, 15)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0
    try:
        (ROOM / "worker.lock").unlink(missing_ok=True)
    except OSError:
        pass
    return pid


def _active_tasks() -> list[str]:
    """还没到终态的任务。有活要干的时候不能收工，否则会打断执行者。"""
    active: list[str] = []
    if not TASKS.is_dir():
        return active
    for directory in TASKS.iterdir():
        card = directory / "task.json"
        if not card.is_file():
            continue
        try:
            task = json.loads(card.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(task, dict) and (task.get("status") or "") not in TERMINAL_STATUS:
            active.append(directory.name)
    return active


def _shutdown(reason: str) -> None:
    pid = _stop_worker()
    _write_session_state("ended", reason)
    try:
        detail = f"，引擎已停（pid {pid}）" if pid else "，引擎本来没在跑"
        if room is not None:
            room.post("system", f"协作已结束：{reason}{detail}", "event",
                      {"phase": "collab", "state": "ok", "actor": "deepseek"})
    except Exception:  # noqa: BLE001
        pass
    time.sleep(0.5)
    os._exit(0)


def _guardian_trigger(idle_limit: float, grace: float,
                      now: float | None = None) -> str:
    """该不该收工？返回原因文本，空串表示继续跑。

    - 页面发了 bye（关标签/关窗口）且过了 grace：立刻走，给刷新留缓冲；
    - 没有 bye 但心跳断太久（浏览器崩溃、断电、网络断）：靠 idle_limit 兜底；
    - 从没人打开过页面：不擅自关（命令行起的服务不该自己死）。
    """
    if not PRESENCE["ever"]:
        return ""
    now = time.time() if now is None else now
    idle = now - PRESENCE["last_seen"]
    bye = PRESENCE["bye_at"]
    if bye and (now - bye) >= grace and idle >= grace:
        return "网页已关闭"
    if idle >= idle_limit:
        return "网页已关闭（长时间没有心跳）"
    return ""


def _guardian_tick(idle_limit: float, grace: float) -> str:
    """跑一次判定：有任务在跑就再等等（只提示一次），返回要收工的原因。"""
    trigger = _guardian_trigger(idle_limit, grace)
    if not trigger:
        return ""
    pending = _active_tasks()
    if pending:
        if not PRESENCE.get("deferred"):
            PRESENCE["deferred"] = True
            try:
                if room is not None:
                    room.post("system",
                              f"还有 {len(pending)} 个任务在跑，干完再收工（网页已经关了也不打断）",
                              "event", {"phase": "collab", "state": "ok", "actor": "deepseek"})
            except Exception:  # noqa: BLE001
                pass
        return ""              # 任务没结束，先不收工
    return trigger


def _guardian(idle_limit: float, grace: float) -> None:
    """没人看着就收工：关页面立刻走（有 grace 缓冲），崩溃/强杀靠超时兜底。"""
    while True:
        time.sleep(1.5)
        trigger = _guardian_tick(idle_limit, grace)
        if trigger:
            _shutdown(trigger)


class Handler(BaseHTTPRequestHandler):
    server_version = "RoomWorkbench/3.0-chat"

    def log_message(self, fmt, *args):
        pass

    # ---------- 基础 ----------
    def _send(self, code: int, body: bytes,
              ctype: str = "application/json; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, code: int = 200) -> None:
        self._drain_body()
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _drain_body(self) -> None:
        """拒绝写请求前先把请求体读掉。

        Windows 上带着没读完的 body 关连接会 RST，客户端连 401/403 都收不到。
        """
        if getattr(self, "_body_done", False):
            return
        self._body_done = True
        if self.command not in WRITE_METHODS:
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 2_000_000:
            return
        try:
            self.rfile.read(length)
        except OSError:
            pass

    def _body(self) -> dict | None:
        self._body_done = True
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > 2_000_000:
            self._json({"ok": False, "error": "请求体太大"}, 413)
            return None
        try:
            raw = self.rfile.read(length).decode("utf-8", errors="replace")
            data = json.loads(raw or "{}")
        except (OSError, ValueError):
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return None
        return data if isinstance(data, dict) else {}

    def _touch(self) -> None:
        PRESENCE["last_seen"] = time.time()
        PRESENCE["ever"] = True
        PRESENCE["bye_at"] = 0.0        # 刷新页面会重新发请求，撤销关闭标记
        PRESENCE["deferred"] = False

    # ---------- 凭证：Cookie 优先，其次请求头；URL 上的 token 在 /api/* 一律不认 ----------
    def _cookies(self) -> dict[str, str]:
        raw = self.headers.get("Cookie") or ""
        jar: dict[str, str] = {}
        for part in raw.split(";"):
            name, _, value = part.partition("=")
            name = name.strip()
            if name:
                jar[name] = value.strip()
        return jar

    def _cookie_token(self) -> str:
        return self._cookies().get(COOKIE_NAME, "").strip()

    def _header_token(self) -> str:
        return (self.headers.get("X-Team-Token") or "").strip()

    @staticmethod
    def _token_matches(supplied: str) -> bool:
        if not TOKEN:
            return True                     # 没启用令牌（令牌文件写不进去）就别拦人
        if not supplied:
            return False
        try:
            return secrets.compare_digest(supplied, TOKEN)
        except TypeError:                   # 极端情况下类型不一致
            return supplied == TOKEN

    def _authorized(self) -> bool:
        """Cookie 或请求头任一对就算过；URL 上的 ?token= 不参与。"""
        if not TOKEN:
            return True
        return any(self._token_matches(t) for t in (self._cookie_token(), self._header_token()) if t)

    def _cookie_header(self) -> str:
        """Cookie 属性只在这里拼一次：HttpOnly + SameSite=Strict + Path=/。"""
        return (f"{COOKIE_NAME}={TOKEN}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={COOKIE_MAX_AGE}")

    def _set_cookie(self) -> None:
        """下发凭证的唯一出口：302 引导和 /api/unlock 都走它，别处不许再 send_header。"""
        if TOKEN:
            self.send_header("Set-Cookie", self._cookie_header())

    def _guard(self, parsed, qs: dict) -> bool:
        """除 /api/alive 外所有 /api/* 都要凭证；URL 里带 token 的直接拒（挡历史书签/Referer）。"""
        if not parsed.path.startswith("/api/"):
            return True
        if "token" in qs:
            self._json({"ok": False,
                        "error": "令牌不能放在 URL 里：只在首次打开时用一次性的 ?k= 引导，之后靠 Cookie"}, 401)
            return False
        if parsed.path == "/api/alive":
            return True
        if self._authorized():
            return True
        self._json({"ok": False, "error": "需要访问令牌"}, 401)
        return False

    # ---------- 写操作：CSRF/Origin + 只收 JSON ----------
    def _csrf_problem(self) -> str:
        """Sec-Fetch-Site → Origin → 都没有就放行（非浏览器客户端）。"""
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site:
            return "" if site in SAFE_FETCH_SITES else "跨站请求被拒绝"
        origin = (self.headers.get("Origin") or "").strip()
        if not origin:
            return ""
        try:
            bits = urlparse(origin)
        except ValueError:
            return "跨站请求被拒绝"
        if bits.scheme not in ("http", "https"):
            return "跨站请求被拒绝"
        if (bits.hostname or "").lower() not in LOOPBACK_HOSTS:
            return "跨站请求被拒绝"
        host = (self.headers.get("Host") or "").strip().lower()
        if not host or bits.netloc.lower() != host:
            return "跨站请求被拒绝"          # Origin 的端口必须和这次请求的 Host 一致
        return ""

    def _write_guard(self, path: str) -> bool:
        if not path.startswith("/api/"):
            return True
        problem = self._csrf_problem()
        if problem:
            self._json({"ok": False, "error": problem}, 403)
            return False
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"ok": False, "error": "只接受 JSON"}, 415)
            return False
        return True

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self._set_cookie()
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _page_html(self, unlock_msg: str = "") -> bytes:
        page = (PAGE
                .replace("__UNLOCK_MSG__", json.dumps(unlock_msg, ensure_ascii=False))
                .replace("__AGENTS__", json.dumps(AGENTS, ensure_ascii=False))
                .replace("__STATUS_LABEL__", json.dumps(STATUS_LABEL, ensure_ascii=False)))
        return page.encode("utf-8")

    # ---------- GET ----------
    def do_GET(self) -> None:           # noqa: N802
        try:
            self._route_get()
        except Exception as exc:        # noqa: BLE001 - 任何异常都要回 JSON，网页才不会卡死
            self._json({"ok": False, "error": f"服务端错误：{type(exc).__name__}: {exc}"}, 500)

    def _route_get(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            # 引导只认一次性码 ?k=：对了就 302 到干净地址 + 种 Cookie，并且立刻换码作废
            if "k" in qs:
                supplied = str((qs.get("k") or [""])[0]).strip()
                if _code_matches(supplied):
                    _rotate_code()               # 用掉即作废：旧链接再点一次就没用了
                    rest = {k: v for k, v in qs.items() if k != "k"}
                    clean = urlencode(rest, doseq=True)
                    self._redirect(parsed.path + (("?" + clean) if clean else ""))
                    return
                self._send(401, self._page_html("这个打开链接已经用过了（一次性），重新拿一个"),
                           "text/html; charset=utf-8")
                return
            if "token" in qs:
                # 长期令牌不再接受走 URL：历史/Referer 里留一份就等于长期泄露
                self._send(401, self._page_html(
                    "链接里带的是长期令牌，已经不再支持了：用「一次性打开链接」，"
                    "或者把 room/web-token 的内容粘到下面的输入框里"), "text/html; charset=utf-8")
                return
            self._send(200, self._page_html(), "text/html; charset=utf-8")
            return

        if not self._guard(parsed, qs):
            return

        def one(name: str) -> str:
            return (qs.get(name) or [""])[0]

        if parsed.path == "/api/browse":
            payload, code = _browse_payload(one("path"))
            self._json(payload, code)
            return
        if parsed.path == "/api/state":
            self._touch()
            self._json(_state_payload(one("session"), one("after"), one("task"), one("limit")))
            return
        if parsed.path == "/api/models":
            self._touch()
            self._json({"ok": True, "models": _models_snapshot()})
            return
        if parsed.path == "/api/alive":
            self._touch()
            # 带上版本：外部工具（MCP / 脚本）能一眼看出对面跑的是哪一版
            self._json({"ok": True, "version": __version__})
            return
        if parsed.path == "/api/control":
            paused, pending = _control_state()
            self._json({"ok": True, "paused": paused, "control": pending,
                        "engine": _engine_info(), "actions": list(CONTROL_ACTIONS),
                        "available": control is not None})
            return
        if parsed.path == "/api/session":
            # 读接口不建会话：一条都没有就空着（界面靠这个显示空状态）
            sessions = STORE.sessions()
            active = STORE.active(one("session")) if sessions else {}
            self._json({"ok": True, "session": active, "sessions": sessions,
                        "store": {"ready": STORE.ready, "note": STORE.broken}})
            return
        if parsed.path == "/api/workspace":
            project = _project_info()
            self._json({"ok": True, "workspace": _norm_path(project.get("workspace") or ""),
                        "name": str(project.get("name") or "")})
            return
        if parsed.path == "/api/task":
            tid = one("id")
            if not tid:
                self._json({"ok": False, "error": "缺少 id 参数"}, 400)
                return
            detail = _task_detail(tid)
            if not detail:
                self._json({"ok": False, "error": f"没有这个任务：{tid}"}, 404)
                return
            self._json({"ok": True, **detail})
            return
        if parsed.path == "/api/artifact":
            ref = one("path") or one("artifact")
            payload = _artifact_payload(ref)
            self._json(payload, 200 if payload.get("ok") else 404)
            return
        if parsed.path == "/api/messages":
            before = one("before")
            limit_raw = one("limit")
            if before or limit_raw:
                # 新式分页：before=<最老那条 id>&limit=120 → 更早的一页（结构同 state.messages）
                payload = _messages_page(one("session"), before, _page_limit(limit_raw))
                self._json(payload)
                return
            after = _as_int(one("after"))
            if room is not None:
                try:
                    self._json(room.after(after))
                    return
                except Exception:  # noqa: BLE001
                    pass
            self._json([_legacy_item(r) for r in _ledger_rows() if _as_int(r.get("id")) > after])
            return
        if parsed.path == "/api/tasks":
            self._json(_tasks_summary())
            return
        self._json({"ok": False, "error": "接口不存在"}, 404)

    # ---------- POST ----------
    def do_POST(self) -> None:          # noqa: N802
        try:
            self._route_post()
        except Exception as exc:        # noqa: BLE001
            self._json({"ok": False, "error": f"服务端错误：{type(exc).__name__}: {exc}"}, 500)

    def _route_post(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        if parsed.path == "/api/unlock":
            self._post_unlock()
            return
        if not self._guard(parsed, qs):
            return
        if not self._write_guard(parsed.path):
            return
        if parsed.path == "/api/bye":
            PRESENCE["bye_at"] = time.time()
            self._json({"ok": True, "bye_at": PRESENCE["bye_at"]})
            return

        body = self._body()
        if body is None:
            return

        if parsed.path == "/api/post":
            self._post_message(body)
            return
        if parsed.path == "/api/control":
            self._post_control(body)
            return
        if parsed.path == "/api/workspace":
            self._post_workspace(body)
            return
        if parsed.path == "/api/session":
            self._post_session(body)
            return
        if parsed.path == "/api/session/select":
            self._post_select(body)
            return
        if parsed.path == "/api/session/archive":
            self._post_session_archive(body)
            return
        if parsed.path == "/api/session/delete":
            self._post_session_delete(body)
            return
        if parsed.path == "/api/session/restore":
            self._post_session_restore(body)
            return
        if parsed.path == "/api/session/empty-trash":
            self._post_session_empty_trash(body)
            return
        if parsed.path == "/api/session/rename":
            self._post_session_rename(body)
            return
        if parsed.path == "/api/artifact/open":
            self._post_artifact_action(body, "open")
            return
        if parsed.path == "/api/artifact/reveal":
            self._post_artifact_action(body, "reveal")
            return
        if parsed.path == "/api/set-model":
            self._post_set_model(body)
            return
        if parsed.path in ("/api/agent-cli/detect", "/api/agent-cli/parse", "/api/agent-cli/save"):
            self._post_agent_cli(parsed.path.rsplit("/", 1)[-1], body)
            return
        if parsed.path.startswith("/api/api-provider/"):
            self._post_api_provider(parsed.path.rsplit("/", 1)[-1], body)
            return
        self._json({"ok": False, "error": "接口不存在"}, 404)

    # ---------- 产物：用默认程序打开 / 在资源管理器里定位 ----------
    def _post_artifact_action(self, body: dict, action: str) -> None:
        ref = str((body or {}).get("path") or "").strip()
        target, error = _resolve_artifact(ref)
        if target is None:
            self._json({"ok": False, "error": error or "找不到这个文件"}, 404)
            return
        ok, message = _open_in_system(str(target), action)
        self._json({"ok": ok, "error": "" if ok else message, "path": str(target)},
                   200 if ok else 500)

    # ---------- /api/set-model：右栏点一下就换模型 ----------
    def _post_set_model(self, body: dict) -> None:
        role = str((body or {}).get("role") or "").strip()
        model = str((body or {}).get("model") or "").strip()
        ok, message = MODELS.set_model(role, model)
        if not ok:
            self._json({"ok": False, "error": message, "models": _models_snapshot()}, 400)
            return
        _room_note(message)                 # 群里留一行，事后能看出是谁什么时候换的
        restart, extra = "", ""
        if MODELS.resolve_role(role) == "coordinator":
            # 大脑换了就让引擎自己重启：用户点一下就行，不用再教他敲命令行。
            # 有任务/回复在跑时不动它——切换本来就下一条消息生效，重启只是顺手让状态干净。
            busy, why = _engine_busy()
            if busy:
                extra = f"；{why}，这次没有重启引擎（换模型下一条消息就生效）"
            else:
                try:
                    from . import start_all
                    ok_restart, note = start_all.restart_engine()
                except OSError as exc:
                    ok_restart, note = False, f"引擎重启失败：{exc}"
                restart = "ok" if ok_restart else "failed"
                extra = "；" + note
                _room_note(note if ok_restart else f"引擎重启失败：{note}")
        self._json({"ok": True, "message": message + extra, "restart": restart,
                    "models": _models_snapshot()})

    # ---------- /api/agent-cli/*：换模型菜单里的「找 CLI / 识别粘贴 / 保存」 ----------
    def _post_agent_cli(self, action: str, body: dict) -> None:
        """detect 会真的跑一次 `--version`，save 会写 room/agent_clis.json，所以都走 POST + 写守卫。
        parse 只做文本提取，不执行任何东西。"""
        agent = str((body or {}).get("agent") or "").strip()
        if agent not in AGENT_CLIS.AGENTS:
            self._json({"ok": False, "error": f"不认识的 Agent：{agent or '（空）'}"}, 400)
            return
        if action == "parse":
            self._json({"ok": True, **AGENT_CLIS.parse_paste(agent, str(body.get("text") or ""))})
            return
        if action == "detect":
            row = AGENT_CLIS.detect(agent)
            self._json({"ok": True, "agent": row, "models": _models_snapshot()})
            return
        raw_models = body.get("models")
        if raw_models is not None and not isinstance(raw_models, list):
            self._json({"ok": False, "error": "models 要是列表"}, 400)
            return
        ok, message, row = AGENT_CLIS.save(agent, str(body.get("path") or ""), raw_models or [])
        if ok:
            _room_note(message)
        self._json({"ok": ok, "message": message if ok else "", "error": "" if ok else message,
                    "agent": row, "models": _models_snapshot()}, 200 if ok else 400)

    # ---------- /api/api-provider/*：API 通道（OpenAI 兼容） ----------
    def _post_api_provider(self, action: str, body: dict) -> None:
        """save / models / delete 写 room/api_providers.json，fetch 还会对外发请求，
        所以全部走 POST + 写守卫。key 只在 fetch 那一刻从环境变量读，接口返回值里没有它。"""
        body = body if isinstance(body, dict) else {}
        pid = str(body.get("id") or "").strip()
        if action == "fetch":
            ok, message, ids = API_PROVIDERS.fetch_models(pid)
            self._json({"ok": ok, "ids": ids, "error": "" if ok else message,
                        "message": message if ok else ""}, 200 if ok else 400)
            return
        if action == "save":
            ok, message = API_PROVIDERS.save_provider(
                pid, str(body.get("label") or ""), str(body.get("base_url") or ""),
                str(body.get("api_key_env") or ""), str(body.get("key") or ""))
        elif action == "models":
            raw = body.get("models")
            if not isinstance(raw, list):
                self._json({"ok": False, "error": "models 要是列表"}, 400)
                return
            ok, message = API_PROVIDERS.save_models(pid, raw)
        elif action == "key":
            # 只在服务端收下并落盘，任何响应里都不带 key 本身
            ok, message = API_PROVIDERS.save_key(pid, str(body.get("key") or ""))
        elif action == "clear-key":
            ok, message = API_PROVIDERS.clear_key(pid)
        elif action == "delete":
            ok, message = API_PROVIDERS.delete(pid)
        else:
            self._json({"ok": False, "error": "接口不存在"}, 404)
            return
        if ok:
            _room_note(message)             # 登记/删提供方这类动作要留痕
        self._json({"ok": ok, "message": message if ok else "", "error": "" if ok else message,
                    "models": _models_snapshot()}, 200 if ok else 400)

    # ---------- /api/unlock：解锁卡片用的换 Cookie 接口 ----------
    def _client_ip(self) -> str:
        try:
            return str(self.client_address[0])
        except (IndexError, TypeError):
            return "-"

    def _unlock_limited(self, ip: str) -> bool:
        now = time.time()
        with UNLOCK_LOCK:
            recent = [t for t in UNLOCK_FAILS.get(ip, []) if now - t < UNLOCK_WINDOW]
            if recent:
                UNLOCK_FAILS[ip] = recent
            else:
                UNLOCK_FAILS.pop(ip, None)
        return len(recent) >= UNLOCK_MAX_FAILS

    def _unlock_failed(self, ip: str) -> None:
        now = time.time()
        with UNLOCK_LOCK:
            recent = [t for t in UNLOCK_FAILS.get(ip, []) if now - t < UNLOCK_WINDOW]
            recent.append(now)
            UNLOCK_FAILS[ip] = recent

    def _unlock_ok(self, ip: str) -> None:
        with UNLOCK_LOCK:
            UNLOCK_FAILS.pop(ip, None)

    def _post_unlock(self) -> None:
        """换取 Cookie：不需要旧凭证，但同样过 CSRF + JSON 校验，并按来源 IP 限流。"""
        qs = parse_qs(urlparse(self.path).query)
        if "token" in qs:                   # 令牌只能放 body 里，URL 一律不认
            self._json({"ok": False,
                        "error": "令牌不要放在 URL 里，请粘贴到解锁框（走请求体）"}, 401)
            return
        ip = self._client_ip()
        if self._unlock_limited(ip):
            self._json({"ok": False, "error": "试太多次了，等一分钟再试"}, 429)
            return
        if not self._write_guard("/api/unlock"):
            return
        body = self._body()
        if body is None:
            return
        supplied = str(body.get("token") or "").strip()
        if TOKEN and not self._token_matches(supplied):
            self._unlock_failed(ip)
            self._json({"ok": False, "error": "令牌不对"}, 401)
            return
        self._unlock_ok(ip)
        payload = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self._set_cookie()
        self.end_headers()
        self.wfile.write(payload)

    # ---------- 其他写方法：没有对应接口，但照样先过校验再拒 ----------
    def _other_write(self) -> None:
        parsed = urlparse(self.path)
        if not self._guard(parsed, parse_qs(parsed.query)):
            return
        if not self._write_guard(parsed.path):
            return
        self._json({"ok": False, "error": "接口不存在"}, 404)

    def do_PUT(self) -> None:            # noqa: N802
        self._other_write()

    do_PATCH = do_PUT
    do_DELETE = do_PUT

    def _post_control(self, body: dict) -> None:
        """网页上的 暂停/继续/停止/批准/重新执行：写一条指令，引擎在步骤边界消费。"""
        action = str(body.get("action") or "").strip().lower()
        task_id = str(body.get("task_id") or body.get("task") or "").strip() or None
        if control is None:
            self._json({"ok": False, "error": "控制模块（control.py）不可用，点了也不会生效"}, 503)
            return
        if not action:
            self._json({"ok": False, "error": "缺少 action（只认 "
                                             + "/".join(CONTROL_ACTIONS) + "）"}, 400)
            return
        try:
            payload = control.request(action, task_id)
        except ValueError as exc:       # 不认识的指令
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        except Exception as exc:        # noqa: BLE001 - 盘写不了也要说人话
            self._json({"ok": False, "error": f"控制指令没写进去：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "action": payload.get("action"),
                    "task": payload.get("task") or "", "at": payload.get("at")})

    def _post_message(self, body: dict) -> None:
        content = str(body.get("content") or body.get("text") or "").strip()
        if not content:
            self._json({"ok": False, "error": "消息内容不能为空"}, 400)
            return
        session_id = str(body.get("session_id") or "").strip() or str(STORE.active().get("id") or "")
        author = str(body.get("as") or "")
        role = str(body.get("role") or "")
        agent = _agent_from(str(body.get("agent") or ""), author, role)
        resolved_role = "user" if agent == "user" else ("system" if agent == "system" else "assistant")
        status = str(body.get("status") or "complete")
        if status not in ("pending", "streaming", "complete", "failed"):
            status = "complete"
        artifacts = body.get("artifacts")
        if isinstance(artifacts, str):
            artifacts = [artifacts]
        elif not isinstance(artifacts, list):
            artifacts = []
        meta = body.get("meta") if isinstance(body.get("meta"), dict) else None
        kind = str(body.get("kind") or "") or ("task" if agent == "user" else "msg")
        try:
            msg = STORE.append(
                session_id, role=resolved_role, agent=agent, content=content,
                task_id=(str(body.get("task_id")) if body.get("task_id") else None),
                artifacts=[str(a) for a in artifacts if str(a).strip()],
                status=status, meta=meta, kind=kind)
        except Exception as exc:    # noqa: BLE001 - 会话不存在/盘写不了都要给出人话
            self._json({"ok": False, "error": f"消息没写进去：{type(exc).__name__}: {exc}"}, 500)
            return
        _nudge()
        self._json({**msg, "ok": True})

    def _post_workspace(self, body: dict) -> None:
        raw = str(body.get("path") or body.get("workspace") or "").strip()
        if not raw:
            self._json({"ok": False, "error": "请填写工作区绝对路径"})
            return
        path, error = _validate_workspace(raw)
        if path is None:
            self._json({"ok": False, "error": error, "path": raw})
            return
        project = _write_project(path)
        _nudge()
        self._json({"ok": True, "workspace": project["workspace"], "name": project["name"]})

    def _post_session(self, body: dict) -> None:
        workspace = _norm_path(body.get("workspace") or _project_workspace())
        title = str(body.get("title") or "").strip()
        if not STORE.ready:
            self._json({"ok": False, "session": STORE.active(),
                        "error": "会话模块（session.py）还没就绪，当前是旧账本兼容模式，"
                                 "新建会话暂不可用；已把这条需求写进旧账本，引擎照常会跑"})
            return
        if workspace:
            resolved, error = _validate_workspace(workspace)
            if resolved is None:
                self._json({"ok": False, "error": f"工作区不可用：{error}"})
                return
            workspace = str(resolved)
        try:
            created = STORE.create(workspace, title)
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"新建会话失败：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "session": created})

    def _post_select(self, body: dict) -> None:
        session_id = str(body.get("session_id") or "").strip()
        if not session_id:
            self._json({"ok": False, "error": "缺少 session_id"}, 400)
            return
        if not STORE.ready:
            if session_id == LEGACY_ID:
                self._json({"ok": True, "session": _legacy_session()})
            else:
                self._json({"ok": False, "error": "会话模块（session.py）还没就绪，只能看旧账本"})
            return
        ids = {s.get("id") for s in STORE.sessions()}
        if session_id not in ids:
            self._json({"ok": False, "error": f"没有这个会话：{session_id}"}, 404)
            return
        try:
            current = STORE.select(session_id)
        except ValueError as exc:              # 已归档 / 找不到：原因原样给前端
            self._json(*self._session_op_error(exc))
            return
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"切换会话失败：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "session": current})

    # ---------- 归档 / 删除 ----------
    @staticmethod
    def _session_op_error(exc: Exception) -> tuple[dict, int]:
        """session.py 的 ValueError 里带的是人话：不存在给 404，其余（已归档/旧会话）给 400。"""
        reason = str(exc)
        code = 404 if "不存在" in reason else 400
        return {"ok": False, "error": reason}, code

    def _post_session_archive(self, body: dict) -> None:
        session_id = str(body.get("id") or body.get("session_id") or "").strip()
        if not session_id:
            self._json({"ok": False, "error": "缺少 id"}, 400)
            return
        want = body.get("archived", True)
        if isinstance(want, str):
            archived = want.strip().lower() not in ("", "0", "false", "no", "off")
        else:
            archived = bool(want)
        try:
            entry = STORE.archive(session_id, archived=archived)
        except ValueError as exc:
            self._json(*self._session_op_error(exc))
            return
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"归档失败：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "session": entry, "archived": archived,
                    "active": STORE.active_id()})

    def _post_session_delete(self, body: dict) -> None:
        session_id = str(body.get("id") or body.get("session_id") or "").strip()
        if not session_id:
            self._json({"ok": False, "error": "缺少 id"}, 400)
            return
        confirm = body.get("confirm")
        if confirm is not True and str(confirm).strip().lower() not in ("true", "1", "yes"):
            self._json({"ok": False, "error": "删除会话需要 confirm: true"}, 400)
            return
        try:
            entry = STORE.delete(session_id)
        except ValueError as exc:
            self._json(*self._session_op_error(exc))
            return
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"删除失败：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "deleted": entry, "active": STORE.active_id()})

    # ---------- 回收站还原 / 改名 ----------
    def _post_session_restore(self, body: dict) -> None:
        name = str(body.get("trash_name") or body.get("name") or "").strip()
        if not name:
            self._json({"ok": False, "error": "缺少 trash_name"}, 400)
            return
        try:
            entry = STORE.restore(name)
        except ValueError as exc:               # 文件名非法 / 文件已经不在回收站
            self._json(*self._session_op_error(exc))
            return
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"还原失败：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "session": entry, "active": STORE.active_id(),
                    "trash": STORE.trash()})

    def _post_session_empty_trash(self, body: dict) -> None:
        try:
            result = STORE.empty_trash()
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"清空回收站失败：{type(exc).__name__}: {exc}"}, 500)
            return
        failed = list(result.get("failed") or [])
        self._json({"ok": True, "removed": int(result.get("removed") or 0),
                    "failed": failed, "trash": STORE.trash(),
                    "error": ("有 %d 个文件删不掉（可能被占用）：%s"
                              % (len(failed), "、".join(failed[:3]))) if failed else None})

    def _post_session_rename(self, body: dict) -> None:
        session_id = str(body.get("id") or body.get("session_id") or "").strip()
        if not session_id:
            self._json({"ok": False, "error": "缺少 id"}, 400)
            return
        try:
            entry = STORE.rename(session_id, str(body.get("title") or ""))
        except ValueError as exc:               # 标题为空 / 会话不存在
            self._json(*self._session_op_error(exc))
            return
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"改名失败：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "session": entry})


def main() -> int:
    ap = argparse.ArgumentParser(description="多 Agent 协作台（聊天式）")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-autostop", action="store_true",
                    help="关网页也不收工（常驻后台用）")
    ap.add_argument("--idle-seconds", type=float, default=90.0,
                    help="多久没收到页面心跳就收工（默认 90 秒）")
    ap.add_argument("--grace-seconds", type=float, default=5.0,
                    help="收到关闭通知后等待几秒，留时间给刷新页面")
    args = ap.parse_args()

    global TOKEN
    TOKEN = _ensure_token()
    token_note = ("链接只能用一次，打开后浏览器会记住；别外传"
                  if TOKEN else "令牌文件写不进去，本次不启用令牌")

    autostop = not args.no_autostop and os.environ.get("AGENT_TEAM_AUTOSTOP", "1") != "0"
    _write_session_state("running")
    if autostop:
        threading.Thread(target=_guardian, args=(args.idle_seconds, args.grace_seconds),
                         daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"协作台: http://{args.host}:{args.port}/?k={_ensure_code()}", flush=True)
    print(f"（{token_note}；令牌也可以从 room/web-token 粘贴到页面里）", flush=True)
    if STORE.ready:
        print("会话存储：session.py 就绪", flush=True)
    else:
        print(f"会话存储：降级到旧账本（{STORE.broken or 'session.py 不可用'}）", flush=True)
    print("关掉网页就自动收工" if autostop else "自动收工已关闭：关网页后仍在后台跑", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
