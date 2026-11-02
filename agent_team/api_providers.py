"""API 提供方：登记 OpenAI 兼容的地址，以及**key 存在哪个环境变量里**。

协作台不保存 key。`room/api_providers.json`（gitignored）里只有地址和环境变量名；
真正的 key 只在发请求那一刻从 `os.environ` 现读。这样页面接口被滥用、日志被贴出去，
都泄漏不到 key。

一个容易踩的坑：协作台进程启动时就继承了环境变量。Windows 上 `setx` 设好之后，
**正在跑的协作台读不到**，要重启（关网页或 `cli stop` 再 `start`）。所以 `status()`
每次都现读环境变量、不缓存，`has_key` 为假时界面上必须把这句话写出来。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from . import agent_clis
from .paths import workbench_root

ROOT = workbench_root()
STORE = ROOT / "room" / "api_providers.json"     # 只有环境变量名，没有 key

PID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{1,63}$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
MAX_MODELS = 100
FETCH_LIMIT = 200
READ_LIMIT = 1_048_576          # /models 响应最多读 1 MB


def _read() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    """原子写：mkstemp、先关句柄、再 os.replace（Windows 上不关句柄会 WinError 32）。"""
    STORE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(STORE.parent), prefix=".api-providers-", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, STORE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def load() -> dict[str, dict]:
    """读文件；坏了/结构不对都返回 `{}`，绝不抛异常（页面轮询会调它）。"""
    out: dict[str, dict] = {}
    for pid, entry in _read().items():
        if not isinstance(entry, dict) or not PID_RE.match(str(pid)):
            continue
        models = entry.get("models") if isinstance(entry.get("models"), list) else []
        out[str(pid)] = {
            "label": str(entry.get("label") or pid)[:60],
            "base_url": str(entry.get("base_url") or "").strip().rstrip("/"),
            "api_key_env": str(entry.get("api_key_env") or "").strip(),
            "models": [str(m) for m in models if agent_clis.MODEL_ID.match(str(m))],
            "fetched_at": str(entry.get("fetched_at") or ""),
        }
    return out


def status(pid: str) -> dict:
    """给界面用：不含 key，`has_key` 每次都现读环境变量。"""
    entry = load().get(str(pid)) or {}
    env = str(entry.get("api_key_env") or "")
    return {"id": str(pid), "label": str(entry.get("label") or pid),
            "base_url": str(entry.get("base_url") or ""),
            "api_key_env": env,
            "has_key": bool(env) and bool(os.environ.get(env, "").strip()),
            "models": list(entry.get("models") or []),
            "fetched_at": str(entry.get("fetched_at") or "")}


def _normalize_base_url(raw: str) -> tuple[str, str]:
    """校验地址，返回 `(去尾斜杠的地址, 错误原因)`。

    只允许 https；http 只放行本机（Ollama / vLLM 这类）。必须按**主机名**判断：
    用字符串前缀判断的话，`http://localhost.evil.com` 会混进来，而协作台会真的把
    用户填的地址拿去发请求。
    """
    text = str(raw or "").strip()
    if not text:
        return "", "地址不能为空"
    try:
        parts = urlsplit(text)
    except ValueError as exc:
        return "", f"地址看不懂：{exc}"
    if parts.scheme not in ("http", "https"):
        return "", "地址要以 https:// 开头（本机服务可以用 http://127.0.0.1）"
    if not parts.netloc or not parts.hostname:
        return "", "地址里没有主机名"
    if "@" in parts.netloc:
        return "", "地址里不要带用户名密码：key 请放环境变量"
    if parts.query or parts.fragment:
        return "", "地址不要带 ? 或 #（只填到 /v1 这一层）"
    host = parts.hostname.lower()
    if parts.scheme == "http" and host not in LOCAL_HOSTS:
        return "", ("协作台只往 https 地址或本机地址发请求；"
                    f"http://{host} 不在本机范围（本机只认 127.0.0.1 / localhost / [::1]）")
    return text.rstrip("/"), ""


def save_provider(pid: str, label: str, base_url: str, api_key_env: str) -> tuple[bool, str]:
    """校验后写入；已有模型列表保留（换地址不用重新勾一遍）。"""
    pid = str(pid or "").strip().lower()
    if not PID_RE.match(pid):
        return False, "提供方 id 只能是小写字母开头、由小写字母/数字/_/- 组成，2–24 位（例如 deepseek）"
    url, why = _normalize_base_url(base_url)
    if why:
        return False, why
    env = str(api_key_env or "").strip().upper()
    if not ENV_RE.match(env):
        return False, "环境变量名只能是大写字母/数字/下划线，且不能以数字开头（例如 DEEPSEEK_API_KEY）"
    name = str(label or "").strip()[:60] or pid
    data = _read()
    old = data.get(pid) if isinstance(data.get(pid), dict) else {}
    models = old.get("models") if isinstance(old.get("models"), list) else []
    data[pid] = {"label": name, "base_url": url, "api_key_env": env,
                 "models": [str(m) for m in models if agent_clis.MODEL_ID.match(str(m))],
                 "fetched_at": str(old.get("fetched_at") or "")}
    _write(data)
    count = len(data[pid]["models"])
    return True, f"已保存 API 提供方 {name}（{count} 个模型）"


def key_for(pid: str) -> str:
    """从环境变量读 key。只在 `api_bridge` 发请求时调用，别处不许读。"""
    entry = load().get(str(pid)) or {}
    env = str(entry.get("api_key_env") or "")
    return os.environ.get(env, "").strip() if env else ""


def _scrub(text: str, key: str) -> str:
    """任何要落盘/回给界面的文本，都先把 key 抹掉——上游报错可能回显请求头。"""
    value = str(text or "")
    if key and key in value:
        value = value.replace(key, "***")
    return value


def _host_of(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return url


def fetch_models(pid: str) -> tuple[bool, str, list[str]]:
    """调 `GET {base_url}/models`，只返回、不写文件。任何信息里都不出现 key。"""
    entry = load().get(str(pid))
    if not entry:
        return False, f"没有这个 API 提供方：{pid}", []
    env = str(entry.get("api_key_env") or "")
    key = key_for(pid)
    if not key:
        return False, f"没读到环境变量 {env}（设置后要重启协作台才生效）", []
    url = str(entry.get("base_url") or "").rstrip("/") + "/models"
    try:
        req = urllib.request.Request(url, method="GET")
    except ValueError:                  # 文件被手改成不是 URL：别让菜单 500
        return False, f"API 地址不合法：{entry.get('base_url') or '（空）'}", []
    req.add_header("Authorization", f"Bearer {key}")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(READ_LIMIT).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, f"key 无效（环境变量 {env}）", []
        if exc.code == 404:
            return False, "这个地址没有 /models，检查 base_url 是否要带 /v1", []
        return False, f"HTTP {exc.code}：{_host_of(url)} 拒绝了这次请求", []
    except (urllib.error.URLError, TimeoutError, OSError):
        return False, f"连不上 {_host_of(url)}", []
    except ValueError:                  # 被手改成不是 URL：别让菜单 500
        return False, f"API 地址不合法：{entry.get('base_url') or '（空）'}", []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return False, "不是 OpenAI 兼容的 /models 响应", []
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return False, "不是 OpenAI 兼容的 /models 响应", []
    ids = sorted({
        str(row.get("id")).strip() for row in rows
        if isinstance(row, dict) and agent_clis.MODEL_ID.match(str(row.get("id") or "").strip())})
    return True, f"读到 {len(ids)} 个模型", ids[:FETCH_LIMIT]


def save_models(pid: str, ids: list[str]) -> tuple[bool, str]:
    """勾选结果写回文件；id 过 `agent_clis.MODEL_ID`，最多 `MAX_MODELS` 个。"""
    data = _read()
    entry = data.get(str(pid))
    if not isinstance(entry, dict):
        return False, f"没有这个 API 提供方：{pid}"
    clean: list[str] = []
    for raw in ids or []:
        mid = str(raw or "").strip()
        if mid and agent_clis.MODEL_ID.match(mid) and mid not in clean:
            clean.append(mid)
    if not clean:
        return False, "这些都不像模型 id，没有保存"
    entry["models"] = clean[:MAX_MODELS]
    entry["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _write(data)
    return True, f"{str(entry.get('label') or pid)} 保存了 {len(entry['models'])} 个模型"


def delete(pid: str) -> tuple[bool, str]:
    """删提供方；还有角色正在用它的模型就拒绝，免得切走的角色指向不存在的型号。"""
    data = _read()
    entry = data.get(str(pid))
    if not isinstance(entry, dict):
        return False, f"没有这个 API 提供方：{pid}"
    from . import models          # 延迟导入：models 在模块级 import 本模块，别绕成环
    used = [f"{role}={mid}" for role, mid in models.overrides().items()
            if str(mid).startswith(f"{pid}:")]
    if used:
        return False, (f"还有角色正在用它的模型（{'、'.join(used)}），"
                       "先在换模型菜单里切走再删")
    label = str(entry.get("label") or pid)
    data.pop(str(pid), None)
    _write(data)
    return True, f"已删除 API 提供方 {label}"


__all__ = ["LOCAL_HOSTS", "delete", "fetch_models", "key_for", "load",
           "save_models", "save_provider", "status"]