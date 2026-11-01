"""本机 Agent CLI：找得到就自动用，找不到就让用户去问那个 Agent、把回答贴回来识别。

换模型菜单是「先选 Agent，再选模型」。选 Claude Code 之前，得先确认本机真有能跑的 `claude`：

1. **检测**：已保存路径 → 环境变量（CODEX_EXE / CLAUDE_EXE / DEEPSEEK_EXE）→ PATH → 常见安装目录。
   `--version` 跑得通才算能用；只有用户点「检测」「保存」时才真的跑，页面轮询只看文件在不在。
2. **识别粘贴**：用户问那个 Agent「你的可执行文件在哪、能用哪些模型」，把整段回答贴回来。
   这里只做文本提取（路径 + 模型 id），**不执行任何东西**。
3. **保存**：文件名必须是这个 Agent 的命令、文件真在、`--version` 跑通；模型 id 只许安全字符。
   原子写 `room/agent_clis.json`（gitignored），不动被 git 跟踪的 `config/team.json`。

`runnable=False` 的 Agent（目前是 DeepSeek）只能登记、不能派发：协作台还没有它的执行适配，
界面和 `set_model` 都如实说，不让人切过去白烧一轮。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .paths import workbench_root
from .winproc import quiet_flags

ROOT = workbench_root()
STORE = ROOT / "room" / "agent_clis.json"

AGENTS: dict[str, dict] = {
    "codex": {"label": "Codex CLI", "bins": ("codex",), "env": "CODEX_EXE", "runnable": True,
              "prefixes": ("gpt-", "codex-", "o1", "o3", "o4"), "aliases": ()},
    "claude": {"label": "Claude Code", "bins": ("claude",), "env": "CLAUDE_EXE", "runnable": True,
               "prefixes": ("claude-",), "aliases": ("opus", "sonnet", "haiku")},
    "deepseek": {"label": "DeepSeek", "bins": ("deepseek", "dsh"), "env": "DEEPSEEK_EXE",
                 "runnable": False, "prefixes": ("deepseek-",), "aliases": (),
                 "why": "协作台还没有 DeepSeek CLI 的执行适配：能登记，暂时不能派发任务"},
}

# 模型 id 会进命令行（Windows 上还要过一层 .cmd），只放行不会被 shell 解释的字符
MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{1,79}$")
_RUNNABLE_EXT_NT = (".exe", ".cmd", ".bat", ".com")
_NOT_MODEL = ("-code", "-cli", "-desktop", "-app", ".exe", ".cmd", ".bat", ".ps1", ".json", ".md",
              ".js", ".py", ".sh")
_QUOTE = "\"'`“”‘’"
_TRAIL = ".,;:!?)]}>，。；：！？）】」』`*"
_CJK = re.compile(r"[\u3000-\u9fff\uff00-\uffef]")
_PATH_QUOTED = re.compile(r"[\"'“”‘’`]((?:[A-Za-z]:[\\/]|~[\\/]|/)[^\"'“”‘’`\r\n]{1,260})[\"'“”‘’`]")
_PATH_WIN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|*?`，。；：（）]+")
_PATH_POSIX = re.compile(r"(?<![A-Za-z0-9:/.])(?:~|/)(?:[^\s\"'<>|*?`，。；：（）/]+/)+[^\s\"'<>|*?`，。；：（）/]+")
_LINE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|~[\\/]|/)")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]*")
_FLAG = re.compile(r"--model[=\s]+[\"'`]?([A-Za-z0-9][A-Za-z0-9._:@-]*)")
_TICK = re.compile(r"`([A-Za-z0-9][A-Za-z0-9._:@-]*)`")


def _name_parts(path: str) -> tuple[str, str]:
    name = re.split(r"[\\/]", str(path))[-1]
    stem, dot, ext = name.rpartition(".")
    return (stem.lower(), "." + ext.lower()) if dot and stem else (name.lower(), "")


def _bin_matches(path: str, spec: dict) -> bool:
    return _name_parts(path)[0] in spec["bins"]


def _usable_suffix(path: str, nt: bool | None = None) -> bool:
    """Windows 上 .ps1 和没后缀的 npm 垫片都不能被 CreateProcess 直接起，要指向 .cmd / .exe。"""
    nt = (os.name == "nt") if nt is None else nt
    ext = _name_parts(path)[1]
    return ext in _RUNNABLE_EXT_NT if nt else ext in ("", ".sh")


def _family(token: str) -> str:
    low = token.lower()
    for agent, spec in AGENTS.items():
        if any(low.startswith(p) for p in spec["prefixes"]):
            return agent
    return ""


def _clean_path(raw: str) -> str:
    return str(raw or "").strip().strip(_QUOTE).rstrip(_TRAIL).strip().strip(_QUOTE)


def _extract_paths(text: str) -> list[str]:
    found: list[str] = []

    def add(raw: str) -> None:
        item = _clean_path(raw)
        if len(item) >= 3 and item not in found:
            found.append(item)

    for line in text.splitlines():
        line = line.strip().lstrip("-*>• ").strip()
        line = line.strip(_QUOTE)
        # `where claude` 的输出就是一整行一个路径，路径里可以有空格（C:\Program Files\...）
        last = re.split(r"[\\/]", line)[-1]
        if (_LINE_PATH.match(line) and not _CJK.search(line) and len(line) <= 260
                and last and not re.search(r"\s", last)):
            add(line)
    for regex in (_PATH_QUOTED, _PATH_WIN, _PATH_POSIX):
        for match in regex.finditer(text):
            add(match.group(1) if regex is _PATH_QUOTED else match.group(0))
    return found


def _extract_models(text: str, agent: str, paths: list[str]) -> tuple[list[str], list[str]]:
    """返回 (属于这个 Agent 的模型 id, 看着像别家的模型 id)。"""
    body = text
    for path in paths:
        body = body.replace(path, " ")
    spec = AGENTS[agent]
    mine: list[str] = []
    others: list[str] = []

    def put(token: str, explicit: bool) -> None:
        token = token.strip(".-_:")
        low = token.lower()
        if not MODEL_ID.match(token) or low.endswith(_NOT_MODEL) or low in spec["bins"]:
            return
        family = _family(token)
        if family == agent or (explicit and not family) or (explicit and low in spec["aliases"]):
            bucket = mine
        elif family:
            bucket = others
        else:
            return
        if token not in bucket:
            bucket.append(token)

    for match in _FLAG.finditer(body):
        put(match.group(1), True)
    for match in _TICK.finditer(body):
        if match.group(1).lower() in spec["aliases"]:
            put(match.group(1), True)
    for match in _TOKEN.finditer(body):
        put(match.group(0), False)
    return mine, others


def parse_paste(agent: str, text: str, nt: bool | None = None) -> dict:
    """从用户贴回来的整段文字里认出可执行文件路径和模型 id。纯文本处理，不执行任何东西。"""
    spec = AGENTS.get(agent)
    if spec is None:
        raise ValueError(f"不认识的 Agent：{agent}（可用：{'、'.join(AGENTS)}）")
    text = str(text or "")[:20000]
    raw_paths = _extract_paths(text)
    paths = []
    ignored = []
    for item in raw_paths:
        if _bin_matches(item, spec):
            paths.append({"path": item, "exists": Path(item).expanduser().is_file(),
                          "usable": _usable_suffix(item, nt)})
        elif any(_bin_matches(item, other) for other in AGENTS.values()):
            ignored.append(item)          # 贴成了别家 Agent 的路径，界面上提醒一句
    rank = {".exe": 0, ".cmd": 1, ".bat": 2, "": 3}
    paths.sort(key=lambda p: (not p["usable"], not p["exists"], rank.get(_name_parts(p["path"])[1], 9)))
    models, others = _extract_models(text, agent, [p["path"] for p in paths] + ignored)
    models.sort(key=lambda m: m.lower() in spec["aliases"])   # 完整 id 在前，简称在后
    hint = ""
    if not paths:
        hint = f"没认出 {spec['label']} 的可执行文件路径。可以直接在终端跑 `{guide(agent)['command']}`，把输出贴进来。"
    elif not models:
        hint = "路径认出来了，没认出模型 id；可以只保存路径，模型以后再补。"
    return {"agent": agent, "label": spec["label"], "paths": paths, "models": models,
            "other_models": others, "ignored_paths": ignored[:10], "hint": hint}


# ---------------------------------------------------------------- 探活 / 存储
_PROBES: dict[str, dict] = {}       # path -> {"ok", "version", "error"}；只缓存本进程


def probe(path: str, timeout: float = 15.0) -> dict:
    """真的跑一次 `<path> --version`。只在用户点「检测」「保存」时调用。"""
    try:
        proc = subprocess.run([str(path), "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=quiet_flags())
    except subprocess.TimeoutExpired:
        result = {"ok": False, "version": "", "error": f"--version 超过 {timeout:.0f} 秒没返回"}
    except OSError as exc:
        result = {"ok": False, "version": "", "error": f"起不来：{exc}"}
    else:
        lines = [ln.strip() for ln in (proc.stdout or proc.stderr or "").splitlines() if ln.strip()]
        version = lines[0][:120] if lines else ""
        ok = proc.returncode == 0
        result = {"ok": ok, "version": version,
                  "error": "" if ok else f"--version 退出码 {proc.returncode}：{version}"}
    _PROBES[str(path)] = result
    return result


def _load() -> dict[str, dict]:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict] = {}
    for agent, entry in data.items():
        if agent not in AGENTS or not isinstance(entry, dict):
            continue
        raw_models = entry.get("models") if isinstance(entry.get("models"), list) else []
        out[agent] = {"path": str(entry.get("path") or ""),
                      "models": [str(m) for m in raw_models if MODEL_ID.match(str(m))]}
    return out


def saved_models(agent: str) -> list[str]:
    return list((_load().get(agent) or {}).get("models") or [])


def _write(data: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(STORE.parent), prefix=".agent-clis-", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, STORE)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- 查找
def _common_candidates(agent: str) -> list[Path]:
    """PATH 之外的常见安装位置。从开始菜单/桌面启动时 PATH 往往比终端里短，靠这个补。"""
    spec = AGENTS[agent]
    home = Path.home()
    dirs = [home / ".local" / "bin", home / ".npm-global" / "bin", Path("/usr/local/bin"),
            Path("/opt/homebrew/bin")]
    names = list(spec["bins"])
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", "") or home / "AppData" / "Roaming")
        local = Path(os.environ.get("LOCALAPPDATA", "") or home / "AppData" / "Local")
        dirs = [appdata / "npm", home / ".local" / "bin", local / "Programs" / spec["label"]] + dirs
        names = [b + ext for b in spec["bins"] for ext in (".exe", ".cmd", ".bat")]
        if agent == "codex":
            base = local / "OpenAI" / "Codex" / "bin"
            try:
                exes = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            except OSError:
                exes = []
            dirs = [p.parent for p in exes] + dirs
    out: list[Path] = []
    for folder in dirs:
        for name in names:
            path = folder / name
            if path not in out:
                out.append(path)
    return out


def resolve_path(agent: str) -> tuple[str, str]:
    """返回 (路径, 来源)；找不到返回 ("", "")。不执行，只看文件。"""
    spec = AGENTS.get(agent)
    if spec is None:
        return "", ""
    saved = (_load().get(agent) or {}).get("path") or ""
    if saved and Path(saved).expanduser().is_file():
        return str(Path(saved).expanduser()), "已保存"
    env = os.environ.get(spec["env"], "").strip()
    if env and Path(env).is_file():
        return env, "环境变量"
    for name in spec["bins"]:
        hit = shutil.which(name)
        if hit:
            return hit, "PATH"
    for path in _common_candidates(agent):
        try:
            if path.is_file():
                return str(path), "常见目录"
        except OSError:
            continue
    return "", ""


def status(agent: str) -> dict:
    """给界面用：找没找到、在哪、能不能派发、上次检测结果。轮询也调，所以不执行任何东西。"""
    spec = AGENTS[agent]
    path, source = resolve_path(agent)
    checked = _PROBES.get(path) if path else None
    reason = ""
    if not path:
        reason = f"本机没找到 {spec['label']}"
    elif not spec["runnable"]:
        reason = spec.get("why", "")
    elif checked and not checked["ok"]:
        reason = checked["error"]
    return {"id": agent, "label": spec["label"], "runnable": spec["runnable"],
            "found": bool(path), "path": path, "source": source,
            "checked": checked is not None, "works": bool(checked and checked["ok"]),
            "version": (checked or {}).get("version", ""), "reason": reason,
            "guide": guide(agent)}


def detect(agent: str) -> dict:
    """用户点「检测」：找到了就真跑一次 --version。"""
    path, _ = resolve_path(agent)
    if path:
        probe(path)
    return status(agent)


def ready(agent: str) -> tuple[bool, str]:
    """这个 Agent 现在能接任务吗。不认识的 CLI 名字一律算"没有执行适配"。"""
    spec = AGENTS.get(agent)
    if spec is None:
        found = bool(shutil.which(agent)) if agent else False
        return False, (f"协作台没有 `{agent}` 的执行适配" if found
                       else f"本机没装 `{agent}` 这个命令（装上之后就能切）")
    path, _ = resolve_path(agent)
    if not path:
        return False, (f"本机没装 {spec['label']}（`{spec['bins'][0]}` 命令找不到）："
                       "在换模型菜单里点它，按提示检测或粘贴路径")
    if not spec["runnable"]:
        return False, spec.get("why", "")
    checked = _PROBES.get(path)
    if checked and not checked["ok"]:
        return False, f"{spec['label']} 在 {path}，但跑不起来：{checked['error']}"
    return True, ""


def guide(agent: str) -> dict:
    """找不到时给用户的两条路：自己在终端查，或者问那个 Agent 本身。"""
    spec = AGENTS[agent]
    name = spec["bins"][0]
    command = f"where {name}" if os.name == "nt" else f"command -v {name}"
    ask = (f"我想让本机的「协作台」调用你（{spec['label']}）的命令行。请告诉我两件事，不用解释：\n"
           f"1. `{name}` 可执行文件的完整路径（Windows 上请给 .cmd 或 .exe 那个，可以运行 `{command}` 查看）；\n"
           f"2. 你的 CLI 能用的 `--model` 取值（完整模型 id），每行一个。")
    return {"command": command, "ask": ask}


def save(agent: str, path: str = "", model_ids: list[str] | None = None,
         nt: bool | None = None) -> tuple[bool, str, dict]:
    """校验后保存：路径必须是这个 Agent 的命令且 --version 跑得通；模型 id 只许安全字符。

    `path` 留空 = 不改路径（只补模型），但这时本机必须已经找得到它。
    返回 (ok, 说明, 最新 status)。
    """
    spec = AGENTS.get(agent)
    if spec is None:
        return False, f"不认识的 Agent：{agent}", {}
    data = _load()
    entry = dict(data.get(agent) or {"path": "", "models": []})
    clean = _clean_path(path)
    if clean:
        target = Path(clean).expanduser()
        if not _bin_matches(clean, spec):
            return False, (f"「{target.name}」不是 {spec['label']} 的命令"
                           f"（应该叫 {' / '.join(spec['bins'])}）"), status(agent)
        if not _usable_suffix(clean, nt):
            return False, (f"「{target.name}」不能直接执行：Windows 上请用同目录下的 .cmd 或 .exe"
                           if (os.name == "nt" if nt is None else nt) else
                           f"「{target.name}」不像可执行文件"), status(agent)
        if not target.is_file():
            return False, f"文件不存在：{target}", status(agent)
        checked = probe(str(target))
        if not checked["ok"]:
            return False, f"{target} 跑不起来：{checked['error']}", status(agent)
        entry["path"] = str(target)
    elif not resolve_path(agent)[0]:
        return False, f"还没有 {spec['label']} 的路径：先贴路径，或者点「重新检测」", status(agent)

    accepted, rejected = [], []
    for raw in model_ids or []:
        mid = str(raw or "").strip()
        if not mid:
            continue
        (accepted if MODEL_ID.match(mid) else rejected).append(mid)
    if rejected:
        return False, f"这些不像模型 id，没保存：{'、'.join(rejected[:5])}", status(agent)
    merged = list(entry.get("models") or [])
    merged += [m for m in accepted if m not in merged]
    entry["models"] = merged[:50]
    data[agent] = entry
    _write(data)
    added = f"，登记了 {len(accepted)} 个模型" if accepted else ""
    where = f"路径 {entry['path']}" if clean else "路径不变"
    return True, f"{spec['label']} 已保存：{where}{added}", status(agent)
