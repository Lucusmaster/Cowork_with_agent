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
from urllib.parse import parse_qs, urlparse

from .winproc import quiet_flags
from .paths import workbench_root  # noqa: E402

try:
    from . import room
except Exception:  # noqa: BLE001 - room.py 可能在 session.py 未就绪时导入失败，网页也得能起
    room = None

try:
    from . import control
except Exception:  # noqa: BLE001 - 控制模块缺失时网页照常能看，只是按钮点了没反应
    control = None

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
MAX_ARTIFACT = 200 * 1024
CONTROL_ACTIONS = ("pause", "resume", "stop", "approve", "rerun")

# 访问令牌：启动时由 _ensure_token() 填。为空表示"没启用令牌"（安全兜底，不把人锁在外面）。
TOKEN = ""

# 网页在线状态：页面每次轮询都会刷新 last_seen，关页面时尽力发一次 bye。
PRESENCE = {"last_seen": 0.0, "bye_at": 0.0, "ever": False, "deferred": False}

AGENTS = {
    "user": {"name": "你", "role": "提需求 · 定边界", "color": "#58a6ff", "initial": "我"},
    "coordinator": {"name": "协调者", "role": "理解 · 计划 · 汇总", "color": "#a371f7", "initial": "协"},
    "planner": {"name": "规划者", "role": "拆步骤 · 写验收", "color": "#58a6ff", "initial": "规"},
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
    if not top:
        return None, "不是 git 仓库（目录里找不到 .git），请先 git init 或换一个仓库路径"
    return Path(top), ""


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


def _typing_info() -> dict:
    try:
        data = json.loads(TYPING_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    try:
        if time.time() - float(data.get("at") or 0) > 180:   # 超过 3 分钟别一直转
            return {}
    except (TypeError, ValueError):
        return {}
    return data


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


def _engine_info() -> dict:
    """工作流引擎在不在跑：只看 worker.lock 的 pid + 心跳，不额外起进程。"""
    try:
        info = json.loads((ROOM / "worker.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {"alive": False, "pid": 0, "beat": 0.0}
    if not isinstance(info, dict):
        return {"alive": False, "pid": 0, "beat": 0.0}
    pid = _as_int(info.get("pid"))
    try:
        beat = float(info.get("beat") or 0)
    except (TypeError, ValueError):
        beat = 0.0
    # 和 worker.take_lock 一个口径：90 秒内有心跳才算活着
    return {"alive": bool(pid) and (time.time() - beat) < 90, "pid": pid, "beat": beat}


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
    artifacts = []
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
        "acceptance": task.get("acceptance") or [],
        "rounds": task.get("rounds") or [],
        "reviews": task.get("reviews") or [],
        "verify_tail": _tail(verify_file) if verify_file else "",
        "review_text": _tail(review_file, 26) if review_file else "",
        "artifacts": artifacts,
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


def _state_payload(session_id: str = "", after_id: str = "", task_id: str = "") -> dict:
    active = STORE.active(session_id)
    sid = str(active.get("id") or "")
    sessions = STORE.sessions()
    if not any(s.get("id") == sid for s in sessions):
        sessions = [active, *sessions]
    sessions.sort(key=lambda s: str(s.get("updated_at") or ""), reverse=True)
    messages = STORE.messages(sid, after_id)
    typing = _typing_info()
    task = _task_detail(task_id) if task_id else _current_task(active, messages)
    project = _project_info()
    changes = _changes(task)
    paused, pending = _control_state()
    workspaces = _workspaces(sessions, str(project.get("workspace") or ""))
    current_ws = _project_workspace() or str(active.get("workspace") or "")
    for item in workspaces:
        item["active"] = bool(current_ws) and _norm_path(item["path"]) == _norm_path(current_ws)
    return {
        "ok": True,
        "session": active,
        "sessions": sessions,
        "workspaces": workspaces,
        "messages": messages,
        "agents": _agents_state(task, typing, messages),
        "typing": typing,
        "task": task,
        "changes": changes,
        "paused": paused,                        # 暂停中：{task, stage, reason, since}
        "control": pending,                      # 还没被引擎消费的控制指令
        "engine": _engine_info(),                # 引擎在不在跑，控制按钮据此启停
        "workspace": current_ws,
        "project": project,                       # 兼容旧前端
        "total_messages": _as_int(active.get("message_count"), len(messages)),
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
<style>
:root{
  --bg:#0a0d12; --panel:#0f131a; --panel2:#141922; --panel3:#1a212b;
  --line:#1f2630; --line2:#2a3340;
  --fg:#dbe2ea; --dim:#7d8899; --dim2:#55606e;
  --brain:#a371f7; --exec:#3fb950; --review:#d29922; --user:#58a6ff; --sys:#64748b;
  --ok:#3fb950; --bad:#f85149; --warn:#d29922;
}
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%}
body{background:var(--bg);color:var(--fg);overflow:hidden;
  font:13.5px/1.62 "Segoe UI","Microsoft YaHei",-apple-system,sans-serif;
  -webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;cursor:pointer}
.mono{font-family:"Cascadia Mono",Consolas,"SF Mono",monospace}
.app{display:none;grid-template-columns:264px minmax(0,1fr) 344px;height:100vh}
body.ready .app{display:grid}

/* ---------- 左栏 ---------- */
.side{background:var(--panel);border-right:1px solid var(--line);display:flex;
  flex-direction:column;min-height:0}
.brand{display:flex;align-items:center;gap:10px;padding:16px 16px 10px}
.brand .mark{width:30px;height:30px;border-radius:10px;display:grid;place-items:center;
  background:linear-gradient(135deg,#a371f7,#58a6ff);color:#0a0d12;font-weight:800;font-size:13px}
.brand .t{font-size:13.5px;font-weight:650;letter-spacing:.2px}
.brand .s{font-size:10.5px;color:var(--dim2);line-height:1.4}
.newchat{margin:8px 12px 14px;height:34px;border-radius:9px;border:1px solid var(--line2);
  background:var(--panel2);display:flex;align-items:center;gap:7px;padding:0 11px;font-size:12.5px}
.newchat:hover{background:var(--panel3);border-color:#3d4a5c}
.side-label{padding:0 16px;font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim2);margin:2px 0 7px;display:flex;align-items:center}
.side-label .count{margin-left:auto;letter-spacing:0;color:var(--dim2)}
.ws-card{margin:0 12px 16px;background:var(--panel2);border:1px solid var(--line);
  border-radius:10px;padding:10px 11px;min-width:0}
.ws-card .wname{font-size:12.5px;font-weight:600;display:flex;align-items:center;gap:7px;
  min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ws-card .wpath{font-size:10.5px;color:var(--dim2);word-break:break-all;margin-top:4px;
  font-family:"Cascadia Mono",Consolas,monospace;line-height:1.5}
.ws-card .wmeta{font-size:10.5px;color:var(--dim);margin-top:7px;display:flex;gap:10px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--dim2);display:inline-block;flex:0 0 auto}
.dot.on{background:var(--ok);box-shadow:0 0 0 3px #3fb95022}
.sessions{flex:1;min-height:0;overflow-y:auto;padding:0 8px 12px}
.sess{width:100%;text-align:left;background:transparent;border:1px solid transparent;
  border-radius:9px;padding:8px 9px;display:block;color:inherit}
.sess:hover{background:var(--panel2)}
.sess.sel{background:var(--panel3);border-color:var(--line2)}
.sess-top{display:flex;gap:8px;align-items:baseline}
.sess-title{font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;flex:1;min-width:0}
.sess-time{font-size:10px;color:var(--dim2);flex:0 0 auto}
.sess-sub{font-size:10.5px;color:var(--dim2);margin-top:2px;white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis}
.side-foot{margin-top:auto;padding:9px 14px;border-top:1px solid var(--line);
  font-size:10.5px;color:var(--dim2);line-height:1.6}

/* ---------- 中栏 ---------- */
.main{display:flex;flex-direction:column;min-width:0;min-height:0;background:var(--bg)}
.topbar{flex:0 0 auto;border-bottom:1px solid var(--line);padding:11px 22px 9px;
  background:linear-gradient(180deg,#0e131b,#0b0f15)}
.topbar-row{display:flex;gap:8px;align-items:center;max-width:860px;margin:0 auto}
.topbar-title{margin-bottom:9px;gap:10px}
.sess-name{font-size:13px;font-weight:650;white-space:nowrap;overflow:hidden;
  text-overflow:ellipsis;min-width:0;flex:1}
.live{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px;
  border-radius:999px;border:1px solid var(--line2);background:var(--panel2);
  font-size:11px;color:var(--dim);flex:0 0 auto}
.live .dot{width:6px;height:6px}
.live.busy{color:#9be3b1;border-color:#3fb95055;background:#3fb95014}
.live.busy .dot{background:var(--ok);animation:pulse 1.5s infinite;box-shadow:0 0 0 3px #3fb95022}
@keyframes pulse{50%{opacity:.4}}
.ws-input{flex:1;height:32px;min-width:0;border-radius:9px;border:1px solid var(--line2);
  background:var(--panel);color:var(--fg);padding:0 11px;font:inherit;font-size:12.5px;outline:none}
.ws-input:focus{border-color:#3d5a7d}
.btn-primary{height:32px;border-radius:9px;border:1px solid #2e5a86;background:#16283c;
  color:#cfe4ff;padding:0 12px;font-size:12.5px;flex:0 0 auto}
.btn-primary:hover{background:#1b3550}
.btn-primary:disabled{opacity:.5;cursor:default}
.ws-status{max-width:860px;margin:5px auto 0;font-size:11.5px;color:var(--dim2);
  min-height:17px;word-break:break-all}
.ws-status.ok{color:var(--ok)}
.ws-status.bad{color:var(--bad)}
.stream{flex:1;min-height:0;overflow-y:auto;padding:22px 22px 4px}
.msg{display:flex;gap:10px;max-width:860px;margin:0 auto 17px;width:100%}
.msg .av{width:28px;height:28px;border-radius:9px;display:grid;place-items:center;
  font-weight:700;font-size:12px;color:#06090f;flex:0 0 auto;margin-top:2px}
.msg .body{min-width:0;max-width:min(680px,84%)}
.msg .meta{display:flex;align-items:center;gap:7px;font-size:11px;color:var(--dim2);
  margin-bottom:4px;flex-wrap:wrap}
.msg .meta .who{font-weight:650}
.msg .meta .role{font-size:10px;padding:0 5px;border-radius:5px;
  border:1px solid var(--line2);color:var(--dim2);white-space:nowrap}
.msg .bubble{background:var(--panel2);border:1px solid var(--line);
  border-radius:4px 13px 13px 13px;padding:10px 14px;font-size:13.5px;line-height:1.7;
  white-space:pre-wrap;word-break:break-word;overflow-wrap:anywhere}
.msg.user{flex-direction:row-reverse}
.msg.user .body{max-width:min(640px,82%)}
.msg.user .meta{justify-content:flex-end}
.msg.user .bubble{background:#14263c;border-color:#26456b;
  border-radius:13px 4px 13px 13px;color:#e6f0fb}
.msg.failed .bubble{border-color:#f8514966;background:#1e1214}
.msg.streaming .bubble{opacity:.78}
.msg .bubble pre.code{background:#0a0f16;border:1px solid var(--line);border-radius:8px;
  padding:8px 10px;margin:6px 0;font-family:"Cascadia Mono",Consolas,monospace;
  font-size:11.5px;line-height:1.55;overflow-x:auto;white-space:pre;color:#c2d1e0}
.msg .bubble code{font-family:"Cascadia Mono",Consolas,monospace;font-size:12px;
  background:#0a0f16;border:1px solid var(--line);border-radius:4px;padding:0 3px}
.fail{display:flex;gap:6px;align-items:flex-start;margin-top:6px;font-size:11.5px;
  color:#ff9d95;background:#2a1214;border:1px solid #f8514944;border-radius:8px;
  padding:6px 9px;word-break:break-word}
.notice{max-width:860px;margin:0 auto 15px;display:flex;align-items:center;gap:10px;
  color:var(--dim2);font-size:11.5px}
.notice:before,.notice:after{content:"";height:1px;background:var(--line);flex:1}
.notice span{max-width:72%;text-align:center;white-space:pre-wrap;word-break:break-word}
.artifacts{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
.art{border:1px solid var(--line2);background:var(--panel);border-radius:7px;
  padding:3px 8px;font-size:11px;color:#9fb6cf;display:inline-flex;gap:5px;
  align-items:center;max-width:100%}
.art:hover{border-color:#3d5a7d;color:#cfe4ff}
.art .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:230px}
.typing{max-width:860px;margin:0 auto 17px;display:flex;align-items:center;gap:9px;
  color:#8aa0b8;font-size:12px}
.typing .dots{display:inline-flex;gap:3px}
.typing .dots i{width:5px;height:5px;border-radius:50%;background:var(--brain);
  animation:blink 1.1s infinite ease-in-out}
.typing .dots i:nth-child(2){animation-delay:.18s}
.typing .dots i:nth-child(3){animation-delay:.36s}
@keyframes blink{0%,80%,100%{opacity:.25;transform:translateY(0)}
  40%{opacity:1;transform:translateY(-2px)}}
.welcome{max-width:620px;margin:56px auto 0;text-align:center;color:var(--dim)}
.welcome h1{font-size:19px;font-weight:650;color:var(--fg);margin-bottom:10px}
.welcome p{font-size:12.5px;line-height:1.9;color:var(--dim2)}
.composer{flex:0 0 auto;padding:10px 22px 14px;border-top:1px solid var(--line);
  background:var(--panel)}
.composer-box{max-width:860px;margin:0 auto;display:flex;gap:8px;align-items:flex-end;
  background:var(--bg);border:1px solid var(--line2);border-radius:12px;
  padding:8px 8px 8px 14px;transition:border-color .15s}
.composer-box:focus-within{border-color:#3d5a7d}
#input{flex:1;resize:none;border:0;outline:none;background:transparent;color:var(--fg);
  font:inherit;max-height:150px;min-height:24px;padding:2px 0}
.send{width:32px;height:32px;border-radius:9px;border:0;background:var(--user);
  color:#06111f;display:grid;place-items:center;flex:0 0 auto}
.send:disabled{opacity:.35;cursor:default}
.composer-hint{max-width:860px;margin:7px auto 0;display:flex;gap:14px;align-items:center;
  font-size:11px;color:var(--dim2);min-height:16px}
.composer-hint kbd{border:1px solid var(--line2);border-radius:4px;padding:0 4px;
  font-size:10px;color:var(--dim)}
.composer-hint .ws{margin-left:auto;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;max-width:48%}

/* ---------- 右栏 ---------- */
.inspector{background:var(--panel);border-left:1px solid var(--line);
  min-height:0;display:flex;flex-direction:column}
.insp{flex:1;overflow-y:auto;padding:14px 14px 28px}
.panel{margin-bottom:17px}
.panel h3{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--dim2);display:flex;align-items:center;gap:6px;margin-bottom:8px}
.panel h3 .n{margin-left:auto;letter-spacing:0;text-transform:none;color:var(--dim2);
  font-size:10px;font-weight:400}
.agent-row{display:flex;align-items:center;gap:9px;padding:6px 7px;border-radius:8px}
.agent-row:hover{background:var(--panel2)}
.agent-row .dot{width:7px;height:7px}
.agent-row.busy .dot{background:var(--ok);box-shadow:0 0 0 3px #3fb9501f;animation:pulse 1.5s infinite}
.agent-row .an{font-size:12.5px;font-weight:600}
.agent-row .ar{font-size:10.5px;color:var(--dim2)}
.agent-row .as{margin-left:auto;font-size:10.5px;color:var(--dim)}
.agent-row.busy .as{color:var(--ok)}
.task-card{border:1px solid var(--line);background:var(--panel2);border-radius:10px;
  padding:10px 11px}
.task-card .tid{font-size:10.5px;color:var(--dim2);
  font-family:"Cascadia Mono",Consolas,monospace}
.task-card .ttitle{font-size:12.5px;font-weight:600;margin:3px 0 7px;line-height:1.5;
  word-break:break-word}
.chips{display:flex;flex-wrap:wrap;gap:5px;align-items:center}
.chip{font-size:10px;padding:1px 6px;border-radius:999px;border:1px solid var(--line2);
  color:var(--dim);line-height:1.6;white-space:nowrap}
.chip.passed{color:var(--ok);border-color:#3fb95055;background:#3fb95014}
.chip.failed,.chip.exec-failed,.chip.stopped{color:var(--bad);border-color:#f8514955;background:#f8514914}
.chip.planned,.chip.executed,.chip.executing{color:var(--user);border-color:#58a6ff55;background:#58a6ff14}
.kv{display:grid;grid-template-columns:62px 1fr;gap:3px 8px;font-size:11.5px;margin-top:8px}
.kv .k{color:var(--dim2)}
.kv .v{color:var(--dim);word-break:break-all}
pre.block{background:var(--bg);border:1px solid var(--line);border-radius:8px;
  padding:8px 10px;font-family:"Cascadia Mono",Consolas,monospace;font-size:10.8px;
  line-height:1.55;color:#9fb0c3;max-height:190px;overflow:auto;white-space:pre-wrap;
  word-break:break-word}
ul.files{list-style:none;display:flex;flex-direction:column;gap:3px;margin-bottom:8px}
ul.files li{font-size:11.5px;color:var(--dim);padding-left:14px;position:relative;
  word-break:break-all}
ul.files li:before{content:"•";position:absolute;left:2px;color:var(--dim2)}
.empty{color:var(--dim2);font-size:11.5px;text-align:center;padding:16px 8px;line-height:1.8}

/* ---------- 控制按钮（暂停/继续/停止/重新执行） ---------- */
.ctrl{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:9px}
.ctrl button{height:27px;border-radius:8px;border:1px solid var(--line2);background:var(--panel);
  color:var(--dim);font-size:11.5px;padding:0 8px;white-space:nowrap;transition:border-color .15s,color .15s}
.ctrl button:hover:not(:disabled){color:var(--fg);border-color:#3d4a5c}
.ctrl button:disabled{opacity:.34;cursor:default}
.ctrl button.hl{color:#cfe4ff;border-color:#2e5a86;background:#16283c}
.ctrl button.hl:hover:not(:disabled){background:#1b3550;border-color:#3d6f9e}
.ctrl button.danger{color:#ffb4ad;border-color:#f8514955;background:#f8514910}
.ctrl button.danger:hover:not(:disabled){color:#ffd7d3;border-color:#f8514999}
.ctrl-hint{margin-top:7px;font-size:10.5px;color:var(--dim2);line-height:1.55}
.paused-banner{margin-top:8px;font-size:11px;color:#f3d08a;background:#2a2213;
  border:1px solid #d2992255;border-radius:8px;padding:6px 8px;line-height:1.55;
  word-break:break-word}
.paused-banner b{color:#ffd27a;font-weight:600}
.live.paused{color:#f3d08a;border-color:#d2992255;background:#d2992214}
.live.paused .dot{background:var(--warn);box-shadow:0 0 0 3px #d2992222}

/* ---------- 访问令牌 ---------- */
.unlock{position:fixed;inset:0;display:grid;place-items:center;padding:32px;z-index:70;
  background:radial-gradient(1100px 560px at 50% -10%,#141b27,#0a0d12 72%)}
.unlock[hidden]{display:none}
.unlock-card{width:min(430px,100%);background:var(--panel);border:1px solid var(--line2);
  border-radius:14px;padding:22px 22px 16px;box-shadow:0 24px 60px #00000066}
.unlock-mark{width:34px;height:34px;border-radius:11px;display:grid;place-items:center;
  background:linear-gradient(135deg,#a371f7,#58a6ff);color:#0a0d12;font-weight:800;
  font-size:14px;margin-bottom:13px}
.unlock-card h2{font-size:15px;font-weight:650;margin-bottom:7px}
.unlock-card p{font-size:12px;color:var(--dim2);line-height:1.7;margin-bottom:13px}
.unlock-card input{width:100%;height:34px;border-radius:9px;border:1px solid var(--line2);
  background:var(--bg);color:var(--fg);padding:0 11px;font-size:12.5px;outline:none;
  font-family:"Cascadia Mono",Consolas,monospace}
.unlock-card input:focus{border-color:#3d5a7d}
.unlock-card button{width:100%;height:34px;margin-top:9px;border-radius:9px;
  border:1px solid #2e5a86;background:#16283c;color:#cfe4ff;font-size:12.5px}
.unlock-card button:hover{background:#1b3550}
.unlock-err{margin-top:9px;font-size:11.5px;color:var(--bad);min-height:16px;line-height:1.5}

/* ---------- 弹层 ---------- */
.modal{position:fixed;inset:0;background:#05070ac7;display:grid;place-items:center;
  padding:44px;z-index:50}
.modal[hidden]{display:none}
.modal-card{width:min(940px,100%);max-height:100%;display:flex;flex-direction:column;
  background:var(--panel);border:1px solid var(--line2);border-radius:12px;overflow:hidden;
  box-shadow:0 24px 60px #00000066}
.modal-head{display:flex;align-items:center;gap:10px;padding:9px 12px;
  border-bottom:1px solid var(--line);background:var(--panel2)}
.modal-head .p{font-family:"Cascadia Mono",Consolas,monospace;font-size:11px;color:var(--dim);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.modal-head button{height:26px;border-radius:7px;border:1px solid var(--line2);
  background:var(--panel);padding:0 9px;font-size:11.5px;color:var(--dim)}
.modal-head button:hover{color:var(--fg)}
.modal-body{flex:1;overflow:auto;padding:12px 14px;margin:0;
  font-family:"Cascadia Mono",Consolas,monospace;font-size:11.8px;line-height:1.65;
  color:#c2d1e0;white-space:pre-wrap;word-break:break-word;background:var(--bg)}
.toast{position:fixed;left:50%;bottom:80px;transform:translateX(-50%);background:#2a1214;
  border:1px solid #f8514966;color:#ffb4ad;font-size:12px;padding:6px 12px;border-radius:8px;
  z-index:60;opacity:0;transition:opacity .2s;pointer-events:none}
.toast.show{opacity:1}
::-webkit-scrollbar{width:9px;height:9px}
::-webkit-scrollbar-thumb{background:#232c38;border-radius:9px;border:2px solid var(--panel)}
::-webkit-scrollbar-thumb:hover{background:#2f3a49}
::-webkit-scrollbar-track{background:transparent}
</style></head>
<body>
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
    <div class="side-foot" id="store-note"></div>
  </aside>

  <main class="main">
    <div class="topbar">
      <div class="topbar-row topbar-title">
        <div class="sess-name" id="crumb">新会话</div>
        <div class="live" id="live"><span class="dot"></span><span id="live-txt">空闲</span></div>
      </div>
      <div class="topbar-row">
        <input class="ws-input" id="ws-input" spellcheck="false"
               placeholder="工作区绝对路径，例如 E:\my-project（回车验证并绑定）">
        <button class="btn-primary" id="ws-btn">验证并绑定</button>
      </div>
      <div class="ws-status" id="ws-status"></div>
    </div>

    <div class="stream" id="stream">
      <div class="welcome" id="welcome">
        <h1>把需求说成一句话就行</h1>
        <p>协调者理解意图、规划者拆步骤、执行者写代码、验收员跑测试、复核员审查。<br>
           这里只显示对话和结果，任务卡与验收证据在右边。</p>
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

<div class="unlock" id="unlock" hidden>
  <div class="unlock-card">
    <div class="unlock-mark">协</div>
    <h2>需要访问令牌</h2>
    <p>协作台只给本机用。把启动时打印的链接里的 token 复制过来，或者直接粘贴
      <span class="mono">room/web-token</span> 文件里的内容。</p>
    <input id="unlock-input" spellcheck="false" autocomplete="off" placeholder="粘贴访问令牌">
    <button id="unlock-btn">进入工作台</button>
    <div class="unlock-err" id="unlock-err"></div>
  </div>
</div>

<script>
const AGENTS = __AGENTS__;
const STATUS_LABEL = __STATUS_LABEL__;
const TOKEN = __TOKEN__;                       // 服务端只在带了有效令牌时才注入，否则是 ""
const ROLES = ['coordinator','planner','executor','tester','reviewer'];
const ALIAS = {deepseek:'coordinator', brain:'coordinator', codex:'executor', relay:'user'};
const CTRL_LABEL = {pause:'暂停', resume:'继续', stop:'停止', approve:'批准', rerun:'重新执行'};
const STAGE_LABEL = {plan:'规划', dispatch:'执行', execute:'执行', exec:'执行',
  verify:'验收', review:'复核', approve:'等你批准', report:'报告'};
const TOKEN_KEY = 'team-token';

/* 令牌三种来源：链接里带来的（服务端验证通过才会注入）、localStorage 里存的、URL 上的 ?token=。 */
const injectedToken = String(TOKEN || '').trim();
const storedToken = (() => { try { return String(localStorage.getItem(TOKEN_KEY) || '').trim(); } catch(e){ return ''; } })();
const urlToken = (() => { try { return String(new URLSearchParams(location.search).get('token') || '').trim(); } catch(e){ return ''; } })();
let ACTIVE_TOKEN = injectedToken || storedToken || urlToken;
let controlCooldown = 0;

const $ = id => document.getElementById(id);
const streamEl = $('stream'), sessionsEl = $('sessions'), inspEl = $('insp');
const wsInput = $('ws-input'), wsBtn = $('ws-btn'), wsStatus = $('ws-status');
const input = $('input'), sendBtn = $('send');
const modal = $('modal'), modalPath = $('modal-path'), modalBody = $('modal-body');
const toastEl = $('toast');
const unlockEl = $('unlock'), unlockInput = $('unlock-input');
const unlockBtn = $('unlock-btn'), unlockErr = $('unlock-err');

let st = {};
let activeId = '';
let renderedSession = '';
let localTyping = null;
let typingEl = null;
let firstPaint = true;
let echoSeq = 0;
const seen = new Map();

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

/* ---------- 令牌与请求封装 ---------- */
function rememberToken(value){
  if(!value) return;
  try { localStorage.setItem(TOKEN_KEY, value); } catch(e){}
}

function withToken(url){
  if(!ACTIVE_TOKEN) return url;
  return url + (url.indexOf('?') >= 0 ? '&' : '?') + 'token=' + encodeURIComponent(ACTIVE_TOKEN);
}

function tokenHeaders(extra){
  const h = Object.assign({'Content-Type': 'application/json'}, extra || {});
  if(ACTIVE_TOKEN) h['X-Team-Token'] = ACTIVE_TOKEN;
  return h;
}

function showUnlock(message){
  document.body.classList.remove('ready');
  unlockEl.hidden = false;
  unlockErr.textContent = message || '';
  try { unlockInput.focus(); } catch(e){}
}

async function apiGet(url){
  try{
    const res = await fetch(url, {cache: 'no-store', headers: tokenHeaders()});
    if(res.status === 401){ showUnlock('访问令牌不对，重新粘贴一次'); return null; }
    if(!res.ok) return null;
    return await res.json();
  }catch(e){ return null; }
}

async function apiPost(url, body){
  try{
    const res = await fetch(url, {method: 'POST', headers: tokenHeaders(),
      body: JSON.stringify(body || {})});
    if(res.status === 401){ showUnlock('访问令牌不对，重新粘贴一次'); return null; }
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
    arts.forEach(path => {
      const btn = document.createElement('button');
      btn.className = 'art';
      btn.title = path;
      btn.innerHTML = '<span>📄</span><span class="nm">' + esc(baseName(path)) + '</span>';
      btn.onclick = () => openArtifact(path);
      host.appendChild(btn);
    });
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
}
function nearBottom(){
  return streamEl.scrollHeight - streamEl.scrollTop - streamEl.clientHeight < 120;
}
function scrollDown(){ streamEl.scrollTop = streamEl.scrollHeight; }

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

function renderSessions(){
  const list = st.sessions || [];
  $('session-count').textContent = list.length;
  if(!list.length){
    sessionsEl.innerHTML = '<div class="empty">还没有会话</div>';
    return;
  }
  sessionsEl.innerHTML = list.map(s => {
    const ws = baseName(s.workspace || '');
    return '<button class="sess ' + (s.id === activeId ? 'sel' : '') + '" data-id="' + esc(s.id) + '">'
      + '<div class="sess-top"><span class="sess-title">' + esc(s.title || '新会话') + '</span>'
      + '<span class="sess-time">' + esc(timeLabel(s.updated_at)) + '</span></div>'
      + '<div class="sess-sub">' + esc(ws || '未绑定工作区') + ' · '
      + esc(String(s.message_count || 0)) + ' 条消息</div></button>';
  }).join('');
  sessionsEl.querySelectorAll('.sess').forEach(el => {
    el.onclick = () => selectSession(el.dataset.id);
  });
}

function renderWorkspace(){
  const ws = st.workspace || (st.session && st.session.workspace) || '';
  const card = $('ws-card');
  const hint = $('composer-ws');
  if(!ws){
    card.innerHTML = '<div class="wname">未绑定工作区</div>'
      + '<div class="wpath">在上方输入仓库绝对路径，回车绑定</div>';
    if(hint) hint.textContent = '';
  } else {
    const known = (st.workspaces || []).find(w => w.path === ws);
    const exists = known ? known.exists : true;
    const count = (st.sessions || []).filter(s => s.workspace === ws).length;
    card.innerHTML = '<div class="wname"><span class="dot ' + (exists ? 'on' : '') + '"></span>'
      + esc(baseName(ws) || ws) + '</div>'
      + '<div class="wpath">' + esc(ws) + '</div>'
      + '<div class="wmeta"><span>' + (exists ? '可用' : '路径不可用') + '</span>'
      + '<span>' + count + ' 个会话</span></div>';
    if(hint) hint.textContent = '工作区：' + ws;
  }
  if(document.activeElement !== wsInput) wsInput.value = ws;
}

function renderInspector(){
  const agents = st.agents || {};
  const task = st.task || null;
  const changes = st.changes || {};
  const busyCount = ROLES.filter(r => agents[r] === 'working').length;

  const agentRows = ROLES.map(r => {
    const a = agentOf(r);
    const busy = agents[r] === 'working';
    return '<div class="agent-row ' + (busy ? 'busy' : '') + '">'
      + '<span class="dot"></span>'
      + '<div><div class="an" style="color:' + esc(a.color) + '">' + esc(a.name) + '</div>'
      + '<div class="ar">' + esc(a.role) + '</div></div>'
      + '<span class="as">' + (busy ? '工作中' : '空闲') + '</span></div>';
  }).join('');

  let taskBlock = '<div class="empty">还没有任务卡<br>发一条需求就会自动建卡</div>';
  if(task){
    const arts = Array.isArray(task.artifacts) ? task.artifacts : [];
    const artHtml = arts.length
      ? '<div class="artifacts">' + arts.map(a =>
          '<button class="art" data-art="' + esc(a.path) + '">📄 ' + esc(baseName(a.path))
          + '</button>').join('') + '</div>'
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
  const pausedInfo = st.paused || null;
  const pausedHere = !!pausedInfo && (!pausedInfo.task || !task || pausedInfo.task === task.id);
  const status = String((task && task.status) || '');
  const runningStatus = ['planned', 'executing', 'dispatched', 'executed'].indexOf(status) >= 0;
  const running = !!engine.alive && runningStatus;
  const cooling = Date.now() < controlCooldown;
  const canPause = running && !pausedInfo;
  const canResume = !!pausedInfo || running;
  const canStop = !!pausedInfo || running;
  const canRerun = !!pausedInfo || running;
  const ctrlBtn = (act, label, on, cls) => '<button'
    + (cls ? ' class="' + cls + '"' : '') + ' data-act="' + act + '"'
    + ((on && !cooling) ? '' : ' disabled') + '>' + label + '</button>';
  const ctrlHtml = '<div class="ctrl">'
    + ctrlBtn('pause', '暂停', canPause, '')
    + ctrlBtn('resume', '继续', canResume, pausedInfo ? 'hl' : '')
    + ctrlBtn('stop', '停止', canStop, 'danger')
    + ctrlBtn('rerun', '重新执行', canRerun, '')
    + '</div>';
  const ctrlHint = !task
    ? '没有任务在跑，四个按钮都灰着。'
    : pausedHere
      ? '已暂停：点「继续」接着干，点「停止」就收工。'
      : running
        ? '引擎在跑，指令会在下一个步骤边界生效。'
        : (engine.alive ? '引擎在跑，不过这个任务不在执行中。' : '引擎没在跑，任务卡是历史记录，按钮点不动。');
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
  const filesHtml = files.length
    ? '<ul class="files">' + files.map(f => '<li>' + esc(f) + '</li>').join('') + '</ul>'
    : '<div class="empty" style="padding:4px 0 8px">没有未提交改动</div>';
  const diffHtml = changes.diff_stat
    ? '<pre class="block">' + esc(changes.diff_stat) + '</pre>' : '';

  inspEl.innerHTML = ''
    + '<section class="panel"><h3>Agent 状态<span class="n">'
    +   (busyCount ? busyCount + ' 个在忙' : '全部空闲') + '</span></h3>'
    +   '<div class="agents">' + agentRows + '</div></section>'
    + '<section class="panel"><h3>当前任务<span class="n">'
    +   (engine.alive ? '引擎在跑' : '引擎没在跑') + '</span></h3>'
    +   taskBlock + pausedBanner + ctrlHtml
    +   '<div class="ctrl-hint">' + esc(ctrlHint) + '</div></section>'
    + '<section class="panel"><h3>测试结果</h3>' + verify + '</section>'
    + '<section class="panel"><h3>代码改动<span class="n">' + files.length
    +   ' 个文件</span></h3>' + filesHtml + diffHtml + '</section>';

  inspEl.querySelectorAll('.art[data-art]').forEach(btn => {
    btn.onclick = () => openArtifact(btn.dataset.art);
  });
  inspEl.querySelectorAll('.ctrl button[data-act]').forEach(btn => {
    btn.onclick = () => sendControl(btn.dataset.act);
  });
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

function applyState(next){
  const wasNear = nearBottom();
  st = Object.assign({}, st, next);
  const sid = (st.session && st.session.id) || '';
  if(sid !== renderedSession){
    resetStream();
    renderedSession = sid;
  }
  activeId = sid;
  if((st.messages || []).length && $('welcome')) $('welcome').remove();
  renderMessages(st.messages || []);
  renderSessions();
  renderWorkspace();
  renderInspector();
  renderChrome();
  if(wasNear || firstPaint){ scrollDown(); firstPaint = false; }
}

async function load(){
  const q = new URLSearchParams();
  if(activeId) q.set('session', activeId);
  const data = await apiGet('/api/state?' + q.toString());
  if(!data) return st;      // 没令牌/服务重启中：下一轮再试
  applyState(data);
  return data;
}

async function selectSession(id){
  if(!id || id === activeId) return;
  await apiPost('/api/session/select', {session_id: id});
  localTyping = null;
  activeId = id;
  await load();
}

async function newSession(){
  const btn = $('new-chat');
  btn.disabled = true;
  const workspace = st.workspace || (st.session && st.session.workspace) || '';
  const data = await apiPost('/api/session', {workspace: workspace}) || {};
  if(data.ok && data.session && data.session.id){
    localTyping = null;
    activeId = data.session.id;
    await load();
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
    await load();
  } else {
    showWsStatus(false, data.error || '这个路径不可用');
  }
  wsBtn.disabled = false;
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
  const list = (next && next.messages) || [];
  if(list.some(m => m.role === 'user' && String(m.content || '').trim() === text)) echo.remove();
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

$('new-chat').onclick = newSession;
wsBtn.onclick = bindWorkspace;
wsInput.addEventListener('keydown', e => {
  if(e.key === 'Enter'){ e.preventDefault(); bindWorkspace(); }
});
sendBtn.onclick = send;
input.addEventListener('input', autosize);
input.addEventListener('keydown', e => {
  if(e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); send(); }
});
$('modal-close').onclick = closeModal;
modal.addEventListener('click', e => { if(e.target === modal) closeModal(); });
document.addEventListener('keydown', e => { if(e.key === 'Escape' && !modal.hidden) closeModal(); });

let lastClock = 0;
function tick(){
  load();
  lastClock += 1;
}

/* ---------- 启动：有令牌才进工作台，没有就先让用户粘贴 ---------- */
function submitUnlock(){
  const value = String(unlockInput.value || '').trim();
  if(!value){ unlockErr.textContent = '先粘贴访问令牌'; return; }
  rememberToken(value);
  unlockInput.value = '';
  unlockErr.textContent = '';
  location.reload();          // 刷新一次，让页面按新令牌重新拉状态
}

function startWorkbench(){
  rememberToken(ACTIVE_TOKEN);
  unlockEl.hidden = true;
  document.body.classList.add('ready');
  autosize();
  load();
  setInterval(tick, 1000);
  input.focus();
}

unlockBtn.onclick = submitUnlock;
unlockInput.addEventListener('keydown', e => {
  if(e.key === 'Enter'){ e.preventDefault(); submitUnlock(); }
});

if(ACTIVE_TOKEN) startWorkbench(); else showUnlock('');

/* 页面开着就一直在（每秒打一次 /api/state，服务端据此判断在线）。
   关页面/关标签页时尽力发一次告别；sendBeacon 不能带头，所以令牌放 URL 上。 */
function sayBye(){
  try{
    const body = JSON.stringify({reason: 'page-unloaded'});
    if(navigator.sendBeacon){
      navigator.sendBeacon(withToken('/api/bye'), new Blob([body], {type: 'application/json'}));
    } else {
      fetch(withToken('/api/bye'), {method:'POST', body, headers: tokenHeaders(), keepalive: true});
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


def _guardian(idle_limit: float, grace: float) -> None:
    """没人看着就收工：关页面立刻走（有 grace 缓冲），崩溃/强杀靠超时兜底。"""
    while True:
        time.sleep(1.5)
        if not PRESENCE["ever"]:
            continue          # 从没人打开过页面，不擅自关
        now = time.time()
        idle = now - PRESENCE["last_seen"]
        bye = PRESENCE["bye_at"]
        trigger = ""
        if bye and (now - bye) >= grace and idle >= grace:
            trigger = "网页已关闭"
        elif idle >= idle_limit:
            trigger = "网页已关闭（长时间没有心跳）"
        if not trigger:
            continue

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
            continue          # 任务没结束，先不收工

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
        self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def _body(self) -> dict | None:
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

    # ---------- 访问令牌 ----------
    def _supplied_token(self, qs: dict) -> str:
        """令牌两种带法都认：请求头 X-Team-Token，或 URL 上的 ?token=。"""
        header = (self.headers.get("X-Team-Token") or "").strip()
        if header:
            return header
        return str((qs.get("token") or [""])[0]).strip()

    def _authorized(self, qs: dict) -> bool:
        if not TOKEN:
            return True                     # 没启用令牌（比如令牌文件写不进去）就别拦人
        supplied = self._supplied_token(qs)
        if not supplied:
            return False
        try:
            return secrets.compare_digest(supplied, TOKEN)
        except TypeError:                   # 极端情况下类型不一致
            return supplied == TOKEN

    def _guard(self, parsed, qs: dict) -> bool:
        """除 /api/alive 外的所有 /api/* 都要令牌；通过了返回 True。"""
        if not parsed.path.startswith("/api/") or parsed.path == "/api/alive":
            return True
        if self._authorized(qs):
            return True
        self._json({"ok": False, "error": "需要访问令牌"}, 401)
        return False

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
            # 带了有效的令牌才把令牌注入页面；没带就注入空串，页面自己弹"输入令牌"卡片。
            page_token = TOKEN if (TOKEN and self._authorized(qs)) else ""
            page = (PAGE
                    .replace("__TOKEN__", json.dumps(page_token))
                    .replace("__AGENTS__", json.dumps(AGENTS, ensure_ascii=False))
                    .replace("__STATUS_LABEL__", json.dumps(STATUS_LABEL, ensure_ascii=False)))
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return

        if not self._guard(parsed, qs):
            return

        def one(name: str) -> str:
            return (qs.get(name) or [""])[0]

        if parsed.path == "/api/state":
            self._touch()
            self._json(_state_payload(one("session"), one("after"), one("task")))
            return
        if parsed.path == "/api/alive":
            self._touch()
            self._json({"ok": True})
            return
        if parsed.path == "/api/control":
            paused, pending = _control_state()
            self._json({"ok": True, "paused": paused, "control": pending,
                        "engine": _engine_info(), "actions": list(CONTROL_ACTIONS),
                        "available": control is not None})
            return
        if parsed.path == "/api/session":
            self._json({"ok": True, "session": STORE.active(one("session")),
                        "sessions": STORE.sessions(),
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
        if not self._guard(parsed, qs):
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
        self._json({"ok": False, "error": "接口不存在"}, 404)

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
        except Exception as exc:    # noqa: BLE001
            self._json({"ok": False, "error": f"切换会话失败：{type(exc).__name__}: {exc}"}, 500)
            return
        self._json({"ok": True, "session": current})


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
    token_note = "链接里的 token 不要外传" if TOKEN else "令牌文件写不进去，本次不启用令牌"

    autostop = not args.no_autostop and os.environ.get("AGENT_TEAM_AUTOSTOP", "1") != "0"
    _write_session_state("running")
    if autostop:
        threading.Thread(target=_guardian, args=(args.idle_seconds, args.grace_seconds),
                         daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"协作台: http://{args.host}:{args.port}/?token={TOKEN}", flush=True)
    print(f"（{token_note}；直接打开不带 token 的地址会要求粘贴令牌）", flush=True)
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
