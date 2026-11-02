"""API 提供方：登记 OpenAI 兼容的地址，以及 key 放在哪里。

key 有两条路，都在本机、都不进版本库：

1. **环境变量**（推荐，尤其是多台机器共用一份配置时）：文件里只存变量名，key 在系统里；
2. **直接在菜单里粘贴**（省事，免 `setx`、免重启）：存到 `room/api_keys.json`（gitignored，
   尽力设成 600）。这条是本机明文文件，和别家工具的凭据文件同一类风险。

两条都有的情况下环境变量优先（`key_for()` 的顺序），所以"机器上设了变量"永远能盖过文件。
不论哪条路，key 都不会进接口返回值、群消息或日志；`key_for()` 是唯一读 key 的地方。
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
KEYS = ROOT / "room" / "api_keys.json"           # 菜单里直接粘贴的 key（本机明文，gitignored）

PID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,23}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{1,63}$")
_ENV_CHARS = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")   # 输入宽容：小写也收，存之前转大写
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
MAX_MODELS = 100
MAX_KEY = 512
FETCH_LIMIT = 200
READ_LIMIT = 1_048_576          # /models 响应最多读 1 MB


def _read_at(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_at(path: Path, data: dict) -> None:
    """原子写：mkstemp、先关句柄、再 os.replace（Windows 上不关句柄会 WinError 32）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.stem}-", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    try:                              # 有 key 的文件尽量只给当前用户读（Windows 上是 no-op）
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read() -> dict:
    return _read_at(STORE)


def _write(data: dict) -> None:
    _write_at(STORE, data)


def _read_keys() -> dict:
    return {str(pid): str(value) for pid, value in _read_at(KEYS).items()
            if PID_RE.match(str(pid)) and isinstance(value, str) and value.strip()}


def _write_keys(data: dict) -> None:
    _write_at(KEYS, data)


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
    """给界面用：不含 key，`has_key` 现读（环境变量或本机保存的 key）。"""
    entry = load().get(str(pid)) or {}
    env = str(entry.get("api_key_env") or "")
    source = _key_source(str(pid))
    return {"id": str(pid), "label": str(entry.get("label") or pid),
            "base_url": str(entry.get("base_url") or ""),
            "api_key_env": env,
            "has_key": bool(source),
            "key_source": source,          # "env" / "file" / ""，界面上用来区分
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


def _env_looks_like_key(text: str) -> bool:
    """用户把 key 贴进了「环境变量名」那一栏时认出来。

    真实场景：选完 DeepSeek 预设，看到"环境变量"就把 `sk-...` 粘了进去，然后收到
    "只能是大写字母/数字/下划线"的报错——报错本身没错，但用户想要的是"把 key 存上"。
    这里先认出来，后面直接当 key 保存，并提示已经把变量名换成了默认值。
    """
    value = str(text or "").strip()
    if not value:
        return False
    if value.lower().startswith(("sk-", "sk_", "api-", "token-", "bearer ")):
        return True
    return len(value) >= 24 and not _ENV_CHARS.match(value)


def _default_env(pid: str) -> str:
    """没填环境变量名时用 `<提供方>_API_KEY`（id 里的 `-` 换成 `_`）。"""
    return re.sub(r"[^A-Z0-9_]", "_", str(pid).upper()) + "_API_KEY"


def save_provider(pid: str, label: str, base_url: str,
                  api_key_env: str, api_key: str = "") -> tuple[bool, str]:
    """校验后写入；已有模型列表保留（换地址不用重新勾一遍）。

    第 5 个参数是"顺手存下的 key"（菜单里那张直接贴 key 的框）：提供方和 key 一次保存，
    省得用户先存提供方、再回头贴 key 那一趟。key 只进 `room/api_keys.json`。
    """
    pid = str(pid or "").strip().lower()
    if not PID_RE.match(pid):
        return False, "提供方 id 只能是小写字母开头、由小写字母/数字/_/- 组成，2–24 位（例如 deepseek）"
    url, why = _normalize_base_url(base_url)
    if why:
        return False, why
    raw_env = str(api_key_env or "").strip()
    key_here = str(api_key or "").strip()
    env = raw_env.upper()
    pasted_key = False
    if raw_env and not _ENV_CHARS.match(raw_env):
        if _env_looks_like_key(raw_env):
            # 贴在变量名栏的其实是 key：变量名退回默认，内容当 key 存
            key_here = key_here or raw_env
            env = _default_env(pid)
            pasted_key = True
        else:
            return False, ("环境变量名只能用字母/数字/下划线（例如 DEEPSEEK_API_KEY）；"
                           "如果你想贴的是 key，请贴到「直接贴 key」那一栏，会自动存到本机")
    elif not env:
        env = _default_env(pid)
    if not ENV_RE.match(env):
        return False, ("环境变量名只能用字母/数字/下划线，且不能以数字开头（例如 DEEPSEEK_API_KEY）；"
                       "想贴 key 的话请用「直接贴 key」那一栏")
    name = str(label or "").strip()[:60] or pid
    data = _read()
    old = data.get(pid) if isinstance(data.get(pid), dict) else {}
    models = old.get("models") if isinstance(old.get("models"), list) else []
    data[pid] = {"label": name, "base_url": url, "api_key_env": env,
                 "models": [str(m) for m in models if agent_clis.MODEL_ID.match(str(m))],
                 "fetched_at": str(old.get("fetched_at") or "")}
    _write(data)
    count = len(data[pid]["models"])
    message = f"已保存 API 提供方 {name}（{count} 个模型）"
    if key_here:
        ok_key, why_key = save_key(pid, key_here)
        if ok_key:
            message += "；key 已存本机，立即生效"
            if pasted_key:
                message += "（你把 key 贴在了变量名栏，已自动改成 key；变量名用 " + env + "）"
        else:
            message += "；key 没存下：" + why_key
    return True, message


def _key_source(pid: str) -> str:
    """key 从哪来：`env`（环境变量优先）、`file`（菜单里粘过）、空串（没有）。"""
    entry = load().get(str(pid)) or {}
    env = str(entry.get("api_key_env") or "")
    if env and os.environ.get(env, "").strip():
        return "env"
    return "file" if _read_keys().get(str(pid)) else ""


def key_for(pid: str) -> str:
    """取 key：环境变量优先，其次本机保存的那份。只在发请求时调用，别处不许读。"""
    entry = load().get(str(pid)) or {}
    env = str(entry.get("api_key_env") or "")
    from_env = os.environ.get(env, "").strip() if env else ""
    if from_env:
        return from_env
    return str(_read_keys().get(str(pid)) or "").strip()


def save_key(pid: str, key: str) -> tuple[bool, str]:
    """把 key 存到本机（`room/api_keys.json`，gitignored）；立即生效，不用重启。"""
    pid = str(pid or "").strip().lower()
    if pid not in load():
        return False, f"没有这个 API 提供方：{pid or '（空）'}（先保存提供方再贴 key）"
    value = str(key or "").strip()
    if not value:
        return False, "key 是空的：要清掉请点「清除 key」"
    if len(value) > MAX_KEY:
        return False, f"key 太长了（最多 {MAX_KEY} 个字符），是不是粘错了"
    if "\n" in value or "\r" in value:
        return False, "key 里不能有换行，只粘 key 本身"
    keys = _read_keys()
    keys[pid] = value
    _write_keys(keys)
    return True, f"key 已保存到本机（{KEYS.name}，不进 git），立即生效"


def clear_key(pid: str) -> tuple[bool, str]:
    """删掉本机保存的 key；环境变量里设的 key 不受影响。"""
    keys = _read_keys()
    if not keys.pop(str(pid), None):
        return False, "本机没有保存过这个提供方的 key"
    _write_keys(keys)
    return True, "已清除本机保存的 key（环境变量里的不受影响）"


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
        return False, (f"没读到 key：环境变量 {env} 为空，本机也没保存过"
                       "（在菜单里贴 key 立即生效；设环境变量则要重启协作台）"), []
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
    keys = _read_keys()
    if keys.pop(str(pid), None):
        _write_keys(keys)
    return True, f"已删除 API 提供方 {label}"


__all__ = ["KEYS", "LOCAL_HOSTS", "clear_key", "delete", "fetch_models", "key_for", "load",
           "save_key", "save_models", "save_provider", "status"]