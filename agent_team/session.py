#!/usr/bin/env python3
"""会话存储：一个会话一个 JSONL，索引在 ``room/sessions/index.json``。

阶段一只做「聊天骨架」：消息是唯一事实来源，任务卡仍在 ``tasks/<ID>/task.json``。
旧账本 ``room/messages.jsonl``（字段 ``from/kind/text``）在读取时映射成新格式，
落到 ``room/sessions/legacy.jsonl``（会话 id = ``legacy``）；迁移是幂等的：
靠每条消息 meta 里的 ``legacy_id`` 去重，重复调用不会重复迁移、也不会丢消息。
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

# 包的定位：room/、tasks/ 都在仓库根下
ROOT = Path(__file__).resolve().parent.parent

# 运行目录。测试里会整体替换 ROOM，其余路径一律在调用时从 ROOM 派生（别在导入时固化）
ROOM = ROOT / "room"

DEFAULT_TITLE = "新会话"
LEGACY_SESSION = "legacy"
LEGACY_TITLE = "历史记录"

ROLES = ("user", "assistant", "system")
STATUSES = ("pending", "streaming", "complete", "failed")

# 旧成员名 → 新 agent 角色（deepseek 是 coordinator 的兼容别名）
AGENT_ALIASES = {
    "user": "user",
    "system": "system",
    "deepseek": "coordinator",
    "codex": "executor",
    "relay": "coordinator",
    "coordinator": "coordinator",
    "planner": "planner",
    "executor": "executor",
    "tester": "tester",
    "reviewer": "reviewer",
}

# 给大脑看的对话摘要里用的中文前缀
AGENT_LABELS = {
    "user": "用户",
    "coordinator": "协调",
    "planner": "规划",
    "executor": "执行",
    "tester": "验收",
    "reviewer": "复核",
    "system": "系统",
}

# 会话 id 只允许这些字符，既要好认，也要挡住 ../ 这种路径穿越
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_MESSAGE_ID_RE = re.compile(r"^msg-(\d+)$")
_CONV_ID_RE = re.compile(r"^chat-(\d+)$")

# 一个进程里可能同时有网页线程和 worker 线程在写，用可重入锁串起来
_LOCK = threading.RLock()


# ---------------- 路径与底层读写 ----------------
def _sessions_dir() -> Path:
    return ROOM / "sessions"


def _index_path() -> Path:
    return _sessions_dir() / "index.json"


def _session_file(session_id: str) -> Path:
    return _sessions_dir() / f"{session_id}.jsonl"


def _legacy_file() -> Path:
    return _sessions_dir() / "legacy.jsonl"


def _legacy_ledger() -> Path:
    """旧账本：只读，内容在首次读取时迁移进 legacy 会话。"""
    return ROOM / "messages.jsonl"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ensure() -> None:
    """确保目录与索引存在；索引缺失时补一个空索引，别让读路径炸掉。"""
    _sessions_dir().mkdir(parents=True, exist_ok=True)
    if not _index_path().exists():
        _write_index({"sessions": [], "active": None})


def _read_json_lines(path: Path) -> list[dict]:
    """读 JSONL：坏行/半行直接跳过，不因为一行脏数据丢掉整个会话。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _write_lines(path: Path, records: list[dict]) -> None:
    _atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def _append_line(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


# ---------------- 索引 ----------------
def _read_index() -> dict:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    sessions_list = data.get("sessions")
    if not isinstance(sessions_list, list):
        sessions_list = []
    data["sessions"] = [item for item in sessions_list if isinstance(item, dict) and item.get("id")]
    data.setdefault("active", None)
    return data


def _write_index(index: dict) -> None:
    _atomic_write(_index_path(), json.dumps(index, ensure_ascii=False, indent=2))


def _find_entry(index: dict, session_id: str | None) -> dict | None:
    wanted = str(session_id or "").strip()
    if not wanted:
        return None
    for entry in index.get("sessions") or []:
        if str(entry.get("id")) == wanted:
            return entry
    return None


def _valid_session_id(session_id: str) -> bool:
    return bool(_SESSION_ID_RE.match(str(session_id or "").strip()))


def _next_session_id(index: dict) -> str:
    top = 0
    for entry in index.get("sessions") or []:
        match = _CONV_ID_RE.match(str(entry.get("id") or ""))
        if match:
            top = max(top, int(match.group(1)))
    return f"chat-{top + 1:03d}"


# ---------------- 字段清洗 ----------------
def _clean_workspace(value) -> str:
    if isinstance(value, Path):
        value = str(value)
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    while len(text) > 3 and text[-1] in ("/", "\\"):
        text = text[:-1]
    return text


def _clean_agent(agent: str | None, role: str) -> str:
    value = str(agent or "").strip()
    if value:
        return AGENT_ALIASES.get(value, value)
    return AGENT_ALIASES.get(role, role)


def _clean_role(role: str | None, agent: str | None) -> str:
    value = str(role or "").strip()
    if value in ROLES:
        return value
    agent_value = str(agent or "").strip()
    if agent_value == "user":
        return "user"
    if agent_value == "system":
        return "system"
    return "assistant"


def _clean_status(status: str | None) -> str:
    value = str(status or "").strip()
    return value if value in STATUSES else "complete"


def _clean_artifacts(artifacts) -> list[str]:
    """artifacts 只放引用（仓库相对路径 / workspace: 前缀），字符串也接受。"""
    if artifacts is None:
        return []
    if isinstance(artifacts, str):
        artifacts = [artifacts]
    out: list[str] = []
    for item in artifacts:
        text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def _clean_meta(meta) -> dict:
    return dict(meta) if isinstance(meta, dict) else {}


def _make_title(content: str) -> str:
    text = " ".join(str(content or "").split()).strip()
    if not text:
        return DEFAULT_TITLE
    return text[:30] + "…" if len(text) > 30 else text


def _should_retitle(entry: dict) -> bool:
    """标题还是默认值时，用第一条用户消息补上（历史会话不参与）。"""
    if entry.get("legacy"):
        return False
    return str(entry.get("title") or "").strip() in ("", DEFAULT_TITLE)


def _title_from_records(records: list[dict]) -> str:
    for record in records:
        if str(record.get("role") or "") == "user" and str(record.get("content") or "").strip():
            return _make_title(record["content"])
    return ""


def _next_message_id(records: list[dict]) -> str:
    seq = 0
    for record in records:
        match = _MESSAGE_ID_RE.match(str(record.get("id") or ""))
        if match:
            seq = max(seq, int(match.group(1)))
    return f"msg-{seq + 1:03d}"


# ---------------- 旧账本迁移 ----------------
def _legacy_to_message(record: dict, legacy_id: str, position: int) -> dict:
    """旧账本一条记录 → 新消息结构（agent 取 from，status 恒 complete，artifacts 为空）。"""
    author = str(record.get("from") or "user").strip() or "user"
    kind = str(record.get("kind") or "msg").strip() or "msg"
    meta = dict(record.get("meta") or {}) if isinstance(record.get("meta"), dict) else {}
    task_id = meta.get("task") or meta.get("task_id") or None

    match = re.search(r"(\d+)$", legacy_id)
    seq = int(match.group(1)) if match else position + 1
    # 旧记录的 meta（如 {"task": ...}）要留着，旧接口映射回去时还能还原
    meta.update({"from": author, "kind": kind, "legacy_id": legacy_id})
    return {
        "id": f"msg-{seq:04d}",
        "session_id": LEGACY_SESSION,
        "role": "user" if author == "user" else ("system" if author == "system" else "assistant"),
        "agent": AGENT_ALIASES.get(author, author),
        "task_id": str(task_id) if task_id else None,
        "content": str(record.get("text") or ""),
        "artifacts": [],
        "status": "complete",
        "created_at": str(record.get("at") or record.get("created_at") or _now()),
        # 保留原始作者与类型，旧接口（room.read_all）要能原样映射回去
        "meta": meta,
    }


def _migrate_legacy_locked() -> int:
    """把旧账本迁进 legacy 会话，返回这次新迁入的条数（调用方须持锁）。"""
    index = _read_index()
    ledger = _legacy_ledger()
    legacy_file = _legacy_file()
    try:
        ledger_stat = ledger.stat() if ledger.is_file() else None
    except OSError:
        ledger_stat = None
    ledger_mtime = float(ledger_stat.st_mtime) if ledger_stat else 0.0
    ledger_size = int(ledger_stat.st_size) if ledger_stat else 0

    entry = _find_entry(index, LEGACY_SESSION)
    # 快路径：上次迁移之后账本没动过（时间和大小都没变），连文件都不用读
    if (entry is not None
            and float(entry.get("ledger_mtime") or 0.0) >= ledger_mtime
            and int(entry.get("ledger_size") or 0) == ledger_size):
        return 0
    if not ledger_mtime and not legacy_file.is_file():
        return 0

    old_records = _read_json_lines(ledger) if ledger_mtime else []
    existing = _read_json_lines(legacy_file)
    seen: set[str] = set()
    for message in existing:
        meta = message.get("meta")
        if isinstance(meta, dict) and meta.get("legacy_id") is not None:
            seen.add(str(meta.get("legacy_id")))

    added: list[dict] = []
    for position, record in enumerate(old_records):
        legacy_id = str(record.get("id", position + 1))
        if legacy_id in seen:
            continue
        seen.add(legacy_id)
        added.append(_legacy_to_message(record, legacy_id, position))

    if added:
        _write_lines(legacy_file, existing + added)
        existing = existing + added
    if not existing:
        return 0            # 没历史就别在侧栏挂一个空会话

    first_stamp = str(existing[0].get("created_at") or "")
    last_stamp = str(existing[-1].get("created_at") or "")
    entry = entry or {}
    entry.update({
        "id": LEGACY_SESSION,
        "title": LEGACY_TITLE,
        "workspace": entry.get("workspace") or "",
        "created_at": entry.get("created_at") or first_stamp or _now(),
        "updated_at": last_stamp or entry.get("updated_at") or first_stamp or _now(),
        "message_count": len(existing),
        "legacy": True,
        "ledger_mtime": ledger_mtime,
        "ledger_size": ledger_size,
    })
    if _find_entry(index, LEGACY_SESSION) is None:
        index["sessions"].append(entry)
    _write_index(index)
    return len(added)


def migrate_legacy() -> int:
    """把旧 ``room/messages.jsonl`` 映射进 legacy 会话；幂等，返回本次新迁入条数。"""
    with _LOCK:
        _ensure()
        return _migrate_legacy_locked()


# ---------------- 对外接口（契约第 3 节） ----------------
def sessions() -> list[dict]:
    """索引里的会话，按 updated_at 倒序（最近聊过的在最上面）。"""
    with _LOCK:
        _ensure()
        _migrate_legacy_locked()
        items = [dict(entry) for entry in _read_index().get("sessions") or []]
    items.sort(key=lambda item: (str(item.get("updated_at") or ""),
                                str(item.get("created_at") or "")), reverse=True)
    return items


def create_session(workspace: str | None = None, title: str = "") -> dict:
    """新建会话（chat-001、chat-002…）并切为活动会话，返回会话条目。"""
    with _LOCK:
        _ensure()
        _migrate_legacy_locked()
        index = _read_index()
        session_id = _next_session_id(index)
        stamp = _now()
        entry = {
            "id": session_id,
            "title": str(title or "").strip() or DEFAULT_TITLE,
            "workspace": _clean_workspace(workspace),
            "created_at": stamp,
            "updated_at": stamp,
            "message_count": 0,
        }
        index["sessions"].append(entry)
        index["active"] = session_id
        _write_index(index)
        _session_file(session_id).touch(exist_ok=True)   # 空会话也要有文件
        return dict(entry)


def active_session() -> dict:
    """当前活动会话；一个都没有就建 chat-001。"""
    with _LOCK:
        _ensure()
        _migrate_legacy_locked()
        index = _read_index()
        entry = _find_entry(index, index.get("active"))
        if entry is not None and not entry.get("legacy"):
            return dict(entry)
        candidates = [item for item in index.get("sessions") or [] if not item.get("legacy")]
        if candidates:
            latest = max(candidates, key=lambda item: str(item.get("updated_at") or ""))
            index["active"] = str(latest.get("id"))
            _write_index(index)
            return dict(latest)
        return create_session()


def set_active(session_id: str) -> dict:
    """切换活动会话；会话不存在时抛 ValueError（前端应提示用户而不是默默忽略）。"""
    with _LOCK:
        _ensure()
        _migrate_legacy_locked()
        index = _read_index()
        entry = _find_entry(index, session_id)
        if entry is None:
            raise ValueError(f"会话不存在：{session_id}")
        index["active"] = str(entry.get("id"))
        _write_index(index)
        return dict(entry)


def append(session_id: str, *, role: str, agent: str, content: str,
           task_id: str | None = None, artifacts: list[str] | None = None,
           status: str = "complete", meta: dict | None = None) -> dict:
    """往会话追加一条消息，返回新消息（含 id/session_id/created_at）。"""
    with _LOCK:
        _ensure()
        _migrate_legacy_locked()
        index = _read_index()
        entry = _find_entry(index, session_id)
        if entry is None:
            raise ValueError(f"会话不存在：{session_id}")
        sid = str(entry.get("id"))
        role_value = _clean_role(role, agent)
        message = {
            "id": _next_message_id(_read_json_lines(_session_file(sid))),
            "session_id": sid,
            "role": role_value,
            "agent": _clean_agent(agent, role_value),
            "task_id": str(task_id) if task_id else None,
            "content": "" if content is None else str(content),
            "artifacts": _clean_artifacts(artifacts),
            "status": _clean_status(status),
            "created_at": _now(),
        }
        if meta:
            message["meta"] = _clean_meta(meta)
        _append_line(_session_file(sid), message)
        entry["message_count"] = int(entry.get("message_count") or 0) + 1
        entry["updated_at"] = message["created_at"]
        if role_value == "user" and message["content"].strip() and _should_retitle(entry):
            entry["title"] = _make_title(message["content"])
        _write_index(index)
        return dict(message)


def messages(session_id: str, after_id: str | None = None, limit: int = 200) -> list[dict]:
    """读会话消息（按写入顺序）。

    - ``after_id`` 给了就只返回它之后的消息；找不到这条就返回空（轮询场景不重复推）；
    - ``limit`` <= 0 表示不截断（内部给旧接口做全量映射用）。
    """
    with _LOCK:
        _ensure()
        _migrate_legacy_locked()
        sid = str(session_id or "").strip()
        if not sid:
            sid = str(_read_index().get("active") or "")
        if not _valid_session_id(sid):
            return []
        path = _session_file(sid)
        if not path.is_file():
            return []
        records = _read_json_lines(path)
        if after_id:
            wanted = str(after_id)
            position = next((i for i, item in enumerate(records) if str(item.get("id")) == wanted), None)
            if position is None:
                return []
            records = records[position + 1:]
        if isinstance(limit, int) and limit > 0:
            records = records[-limit:]
        return [dict(item) for item in records]


def update(session_id: str, message_id: str, **fields) -> dict | None:
    """改一条消息（典型用法：把 pending 改成 complete），返回改后的消息；找不到返回 None。"""
    protected = ("id", "session_id", "created_at")
    with _LOCK:
        _ensure()
        _migrate_legacy_locked()
        sid = str(session_id or "").strip()
        if not _valid_session_id(sid):
            return None
        path = _session_file(sid)
        if not path.is_file():
            return None
        records = _read_json_lines(path)
        updated: dict | None = None
        for position, record in enumerate(records):
            if str(record.get("id")) != str(message_id):
                continue
            merged = dict(record)
            for key, value in fields.items():
                if key in protected:
                    continue
                if key == "artifacts":
                    merged[key] = _clean_artifacts(value)
                elif key == "status":
                    merged[key] = _clean_status(value)
                elif key == "meta":
                    merged[key] = _clean_meta(value)
                else:
                    merged[key] = value
            records[position] = merged
            updated = merged
            break
        if updated is None:
            return None
        _write_lines(path, records)
        index = _read_index()
        entry = _find_entry(index, sid)
        if entry is not None:
            entry["updated_at"] = _now()
            entry["message_count"] = len(records)
            if _should_retitle(entry):
                title = _title_from_records(records)
                if title:
                    entry["title"] = title
            _write_index(index)
        return dict(updated)


def context(session_id: str, limit: int = 40) -> str:
    """给大脑看的对话摘要：带 agent 中文前缀，失败标注「（失败）」，跳过空占位。"""
    records = messages(session_id, limit=0)
    lines: list[str] = []
    for message in records:
        content = str(message.get("content") or "").strip()
        status = str(message.get("status") or "")
        # pending 的空消息是还没落地的占位，喂给大脑只会干扰判断
        if not content:
            continue
        text = " ".join(content.split())
        if len(text) > 1500:
            text = text[:1500] + "…"
        agent = str(message.get("agent") or "")
        label = AGENT_LABELS.get(agent, agent or AGENT_LABELS["system"])
        lines.append(f"{label}（失败）: {text}" if status == "failed" else f"{label}: {text}")
    if not isinstance(limit, int) or limit <= 0:
        limit = 40
    return "\n".join(lines[-limit:])


def title_for(session_id: str, fallback: str = "") -> str:
    """用第一条用户消息当标题；没有就用会话标题，再不行用 fallback。"""
    for message in messages(session_id, limit=0):
        if str(message.get("role") or "") == "user" and str(message.get("content") or "").strip():
            return _make_title(message["content"])
    with _LOCK:
        entry = _find_entry(_read_index(), session_id)
    title = str((entry or {}).get("title") or "").strip()
    if title and title != DEFAULT_TITLE:
        return title
    return str(fallback or "").strip() or title or DEFAULT_TITLE