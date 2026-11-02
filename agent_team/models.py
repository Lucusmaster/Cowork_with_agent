"""角色 ↔ 模型的绑定：可运行时切换，但**切换必须过白名单**。

原状（0.9.13 之前）：模型写死在 `config/team.json` 里，而且是**两处**——

- `agents[]` 每个角色一个 `model`（页面右栏显示的就是它）；
- `executor` / `reviewer` 两个块里还有一个 `model`（真正派发时用的是它）。

于是"换模型"等于手动改一个被 git 跟踪的配置文件，还得记住改哪处、改完要不要重启。
本模块把这件事收成一条链：

    任务卡覆盖  >  运行时覆盖（room/model_overrides.json）  >  配置文件  >  默认

- 运行时覆盖放 `room/`（gitignored），改它不算改配置，下一张卡立即生效，不用重启
  （协调者是下一条消息生效）；
- 任何名字都必须先 `resolve()` 成注册表里的规范 id，拼错当场拒绝——
  让"切到 claude-opus-5-5"这种笔误在**切换时**报错，而不是烧完一整轮才发现模型不存在；
- 注册表没写的型号会自动从配置里收录（老配置不改也能用，只是没有别名）。

谁能切：`coordinator` 现在也能切——它每条用户消息才调一次大脑（`worker.brain_decide` 里
`models.model_for("coordinator")`），没有常驻会话也没有缓存模型，所以"下一次调用生效"成立，
不用重启引擎。`tester` 不可切：验收跑的是本地命令，只认退出码，根本不调模型——
给它一个模型菜单等于摆设。

模型背后是哪个 CLI（codex / claude / …）由注册表的 `cli` 决定；找没找到、能不能跑交给
`agent_clis`。用户在本机登记的模型（room/agent_clis.json）也算注册表的一部分。
"""
from __future__ import annotations

import json
import os
import shutil  # noqa: F401 - 测试用 models.shutil.which 打桩；agent_clis 共用同一个模块对象
import tempfile
from pathlib import Path

from . import agent_clis
from . import api_providers
from .paths import workbench_root

ROOT = workbench_root()
CONFIG = ROOT / "config" / "team.json"
OVERRIDES = ROOT / "room" / "model_overrides.json"   # 运行时选择，不入库

ROLES = ("coordinator", "planner", "executor", "tester", "reviewer")
ROLE_LABELS = {
    "coordinator": "协调",
    "planner": "规划",
    "executor": "执行",
    "tester": "验收",
    "reviewer": "复核",
}
# 别名 → 角色（群里说话的写法）
ROLE_ALIASES = {
    "coordinator": "coordinator", "coord": "coordinator", "协调": "coordinator", "协调者": "coordinator",
    "planner": "planner", "plan": "planner", "规划": "planner", "规划者": "planner",
    "executor": "executor", "exec": "executor", "执行": "executor", "执行者": "executor",
    "tester": "tester", "test": "tester", "verify": "tester", "验收": "tester", "验收者": "tester",
    "reviewer": "reviewer", "review": "reviewer", "复核": "reviewer", "复核者": "reviewer",
}
# 这些角色真的会调模型、而且派发时读这里的结果（worker 大脑 / 规划 / team dispatch / team review）。
SWITCHABLE = {"coordinator", "planner", "executor", "reviewer"}
UNSWITCHABLE = {
    "tester": "验收跑的是本地验收命令、只认退出码，不调用模型，没有可换的",
}
UNSWITCHABLE_REASON = UNSWITCHABLE["tester"]


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def config() -> dict:
    return _read_json(CONFIG)


def _agent_entries(cfg: dict | None = None) -> list[dict]:
    cfg = config() if cfg is None else cfg
    entries = cfg.get("agents")
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict) and str(e.get("role") or "").strip()]


def registry(cfg: dict | None = None) -> dict[str, dict]:
    """当前认识的所有模型：`{id: {cli, backend, aliases, note}}`。

    显式注册的优先；配置里出现但没注册的自动收录（老配置零改动），
    只是没有别名，`note` 标成"自动收录"。
    """
    cfg = config() if cfg is None else cfg
    out: dict[str, dict] = {}
    raw = cfg.get("model_registry")
    if isinstance(raw, dict):
        for mid, spec in raw.items():
            key = str(mid or "").strip()
            if not key or not isinstance(spec, dict):
                continue
            out[key] = {
                "cli": str(spec.get("cli") or "codex"),
                "backend": str(spec.get("backend") or ""),
                "aliases": [str(a) for a in (spec.get("aliases") or []) if str(a).strip()],
                "note": str(spec.get("note") or ""),
                # 静态登记的 API 模型：必须带 provider + remote_id，否则不知道发到哪、发什么型号
                "provider": str(spec.get("provider") or "").strip(),
                "remote_id": str(spec.get("remote_id") or "").strip(),
            }
    # 用户在本机登记的（换模型菜单里粘贴识别后保存的）
    try:
        for agent in agent_clis.AGENTS:
            for mid in agent_clis.saved_models(agent):
                if mid not in out:
                    out[mid] = {"cli": agent, "backend": "", "aliases": [], "note": "本机登记",
                                "provider": "", "remote_id": ""}
    except Exception:  # noqa: BLE001 - 登记文件坏了不能让注册表跟着挂
        pass
    # API 提供方里勾选过的模型。id 一律加 `<提供方>:` 前缀：中转站的模型常常也叫 `gpt-*`，
    # 不加前缀会和本机 CLI 的型号撞名；两个提供方有同名模型时也得能分清发到哪个地址。
    try:
        for pid, prov in api_providers.load().items():
            for mid in prov.get("models") or []:
                key = f"{pid}:{mid}"
                out.setdefault(key, {"cli": "api", "backend": pid, "aliases": [],
                                     "note": str(prov.get("label") or pid),
                                     "provider": pid, "remote_id": str(mid)})
    except Exception:  # noqa: BLE001 - 提供方文件坏了不能让注册表跟着挂
        pass
    # 自动收录：配置文件里凡是提到过的型号都算"认识"。
    # 但要先按别名归一：`fallback_models: ["luna"]` 里的 luna 是 gpt-6-luna 的别名，
    # 不能因此凭空多出一个叫 luna 的型号（否则模型菜单里出现幽灵条目，
    # 而 `resolve("luna")` 也会直接命中这个幽灵 id，别名等于失效）。
    alias_to_id = {str(alias).lower(): mid
                   for mid, spec in out.items() for alias in spec.get("aliases") or [] if str(alias).strip()}
    mentioned: list[str] = []
    for entry in _agent_entries(cfg):
        mentioned.append(str(entry.get("model") or "").strip())
    for role in ("executor", "reviewer", "planner", "tester"):
        block = cfg.get(role)
        if isinstance(block, dict):
            mentioned.append(str(block.get("model") or "").strip())
            mentioned.extend(str(m).strip() for m in (block.get("fallback_models") or []))
    for mid in mentioned:
        if not mid:
            continue
        canonical = alias_to_id.get(mid.lower(), mid)
        if canonical not in out:
            out[canonical] = {"cli": "codex", "backend": "", "aliases": [], "note": "自动收录"}
    return out


def models() -> list[str]:
    return sorted(registry())


def resolve(name: str) -> str | None:
    """把用户写的名字变成规范 id：支持 id、别名、大小写、唯一前缀。认不出返回 None。"""
    want = str(name or "").strip()
    if not want:
        return None
    reg = registry()
    if want in reg:
        return want
    low = want.lower()
    for mid, spec in reg.items():
        if mid.lower() == low:
            return mid
        if any(str(a).lower() == low for a in spec.get("aliases") or []):
            return mid
    # 唯一前缀（"gpt-6.1" → 只有 gpt-6.1-terra 时才算命中，含糊就不猜）
    hits = [mid for mid in reg if mid.lower().startswith(low)]
    return hits[0] if len(hits) == 1 else None


def resolve_role(name: str) -> str | None:
    return ROLE_ALIASES.get(str(name or "").strip().lower())


def overrides() -> dict[str, str]:
    data = _read_json(OVERRIDES)
    return {str(k): str(v) for k, v in data.items() if str(k) in ROLES and str(v).strip()}


def _write_overrides(data: dict[str, str]) -> None:
    """原子写：切换过程中崩了也不会留下半个 JSON 把整条链读挂。

    Windows 上必须先关掉临时文件句柄再 replace（mkstemp 返回的 fd 不关就是
    WinError 32「另一个程序正在使用此文件」）；失败时把临时文件清掉，别在 room/ 里堆垃圾。
    """
    OVERRIDES.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=str(OVERRIDES.parent), prefix=".model-", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, OVERRIDES)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def switchable(role: str) -> bool:
    return role in SWITCHABLE


def set_model(role: str, model: str) -> tuple[bool, str]:
    """运行时改一个角色的模型；`model` 为空或 "reset" 表示恢复配置默认。

    返回 `(ok, 说明)`。所有失败都在这里说清楚，别让调用方猜。
    """
    role = resolve_role(role) or str(role or "").strip()
    if not role:
        return False, "缺少角色"
    if role not in ROLES:
        return False, f"未知角色：{role}（可用：{'、'.join(ROLE_LABELS[r] for r in ROLES)}）"
    if not switchable(role):
        return False, f"{ROLE_LABELS[role]}不能运行时切换：{UNSWITCHABLE.get(role, UNSWITCHABLE_REASON)}"

    clean = str(model or "").strip()
    data = overrides()
    if clean.lower() in ("", "reset", "default", "默认"):
        if role in data:
            data.pop(role)
            _write_overrides(data)
            return True, f"{ROLE_LABELS[role]}已恢复配置默认（{model_for(role)[0] or '模型默认'}）"
        return True, f"{ROLE_LABELS[role]}本来就是配置默认（{model_for(role)[0] or '模型默认'}）"

    canonical = resolve(clean)
    if not canonical:
        return False, (f"不认识模型「{clean}」；可用：{'、'.join(models()) or '（注册表为空，先配 config/team.json）'}")
    spec = registry().get(canonical) or {}
    if role == "executor" and str(spec.get("cli") or "") == "api":
        return False, ("API 模型只能收发文字，不能改文件、也不能跑命令：当执行者每轮都会空跑。"
                       "协调 / 规划 / 复核可以用它")
    ready, why = cli_ready(canonical)
    if not ready:
        return False, why
    if data.get(role) == canonical:
        return True, f"{ROLE_LABELS[role]}已经是 {canonical}"
    data[role] = canonical
    _write_overrides(data)
    where = "下一条消息" if role == "coordinator" else "下一张卡"
    return True, f"{ROLE_LABELS[role]} → {canonical}（{where}生效）"


def model_for(role: str, task: dict | None = None) -> tuple[str, str]:
    """解析某角色当前该用哪个模型，返回 `(model, 来源)`；来源用于界面上解释"为什么是它"。"""
    role = resolve_role(role) or str(role or "").strip()
    over = overrides()
    card_models = (task or {}).get("models")
    if isinstance(card_models, dict) and str(card_models.get(role) or "").strip():
        return str(card_models[role]).strip(), "卡"
    card_single = str((task or {}).get("model") or "").strip()
    if card_single and role in SWITCHABLE:
        return card_single, "卡"
    # 老卡片的写法：task["executor"]["model"] / task["reviewer"]["model"]
    card_block = (task or {}).get(role)
    if isinstance(card_block, dict) and str(card_block.get("model") or "").strip():
        return str(card_block["model"]).strip(), "卡"
    if role in over:
        return over[role], "临时"
    cfg = config()
    for entry in _agent_entries(cfg):
        if str(entry.get("role") or "").strip() == role:
            if str(entry.get("model") or "").strip():
                return str(entry["model"]).strip(), "配置"
            break
    block = cfg.get(role)
    if isinstance(block, dict) and str(block.get("model") or "").strip():
        return str(block["model"]).strip(), "配置"
    return "", "默认"


def effort_for(role: str) -> str:
    """推理档位：优先 agents[] 里的 `effort`，再退到 executor/reviewer 块的 `reasoning_effort`。"""
    role = resolve_role(role) or str(role or "").strip()
    cfg = config()
    for entry in _agent_entries(cfg):
        if str(entry.get("role") or "").strip() == role and str(entry.get("effort") or "").strip():
            return str(entry["effort"]).strip()
    block = cfg.get(role)
    if isinstance(block, dict) and str(block.get("reasoning_effort") or "").strip():
        return str(block["reasoning_effort"]).strip()
    return ""


def fallback_models(role: str, exclude: str = "") -> list[str]:
    """降级链：模型容量满载（`at capacity`）时按顺序尝试的备用模型。

    只读配置里这个角色块的 `fallback_models`（`executor` / `reviewer` 都支持），
    没有就返回空列表（不自动降级）。返回的是**规范 id**：配置里写别名（`luna`）
    也认，认不出的型号原样保留，让下游 CLI 自己报错；`exclude` 用来排掉主模型
    ——运行时把执行者切到 luna 之后，链里不该再出现 luna 试第二遍。
    """
    role = resolve_role(role) or str(role or "").strip()
    cfg = config()
    block = cfg.get(role)
    if not isinstance(block, dict):
        return []
    raw = block.get("fallback_models")
    if not isinstance(raw, list):
        return []
    skip = str(exclude or "").strip().lower()
    chain: list[str] = []
    seen = {skip} if skip else set()
    for item in raw:
        text = str(item or "").strip()
        if not text:
            continue
        name = resolve(text) or text
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        chain.append(name)
    return chain


def clis() -> dict[str, bool]:
    """各个 CLI 在不在本机（只做提示：注册表里有名字但没装 CLI，切过去也是白切）。"""
    return {name: bool(agent_clis.resolve_path(name)[0]) for name in agent_clis.AGENTS}


def cli_ready(model: str) -> tuple[bool, str]:
    """这个模型现在真能跑吗：CLI 找不到、跑不起来、或协作台没有它的执行适配，都别让切。

    `cli == "api"` 时不碰任何 CLI，也不联网（`snapshot()` 每次页面轮询都会调这里）：
    只看提供方在不在、环境变量里有没有 key。
    """
    spec = registry().get(model) or {}
    cli = str(spec.get("cli") or "codex").strip()
    if cli == "api":
        provider = str(spec.get("provider") or "").strip()
        if not provider:
            return False, f"{model}：注册表里没有 provider，不知道该发到哪个 API"
        entry = api_providers.load().get(provider)
        if not entry:
            return False, f"{model}：API 提供方 {provider} 不存在（在换模型菜单里添加）"
        env = str(entry.get("api_key_env") or "")
        if not api_providers.key_for(provider):
            return False, (f"{model}：没读到 key（环境变量 {env or '（未填写）'} 为空，本机也没保存过）；"
                           "在菜单里贴 key 立即生效，设环境变量则要重启协作台")
        return True, ""
    ok, why = agent_clis.ready(cli)
    return (True, "") if ok else (False, f"{model}：{why}")


def snapshot(task: dict | None = None) -> dict:
    """给网页/接口用的一份完整快照：每个角色现在用什么、为什么、能不能切。"""
    reg = registry()
    rows = []
    for role in ROLES:
        model, source = model_for(role, task)
        spec = reg.get(model) or {}
        rows.append({
            "role": role,
            "name": ROLE_LABELS[role],
            "model": model,
            "source": source,
            "switchable": switchable(role),
            "reason": "" if switchable(role) else UNSWITCHABLE.get(role, UNSWITCHABLE_REASON),
            "cli": str(spec.get("cli") or ""),
            "known": bool(model) and model in reg,
            "ready": (not model) or cli_ready(model)[0],
        })
    entries = []
    for mid, spec in sorted(reg.items()):
        ok, why = cli_ready(mid)
        row = {"id": mid, "cli": spec.get("cli", ""), "backend": spec.get("backend", ""),
               "aliases": list(spec.get("aliases") or []), "note": spec.get("note", ""),
               "ready": ok, "why": why}
        if spec.get("provider"):
            row["provider"] = str(spec["provider"])
        entries.append(row)
    agents = []
    for name in agent_clis.AGENTS:
        row = agent_clis.status(name)
        row["models"] = [e["id"] for e in entries if (e["cli"] or "codex") == name]
        agents.append(row)
    # API 不是一个本机 CLI，但菜单里得和 Codex / Claude 并排显示，所以补一行伪 Agent。
    # 前端 `renderModelList` 按 `m.cli === agent` 过滤，api 模型会自动归到这一组。
    providers = [api_providers.status(pid) for pid in api_providers.load()]
    agents.append({
        "id": "api", "label": "API（OpenAI 兼容）", "runnable": True,
        "found": bool(providers), "checked": True,
        "works": any(p["has_key"] for p in providers),
        "version": f"{len(providers)} 个提供方", "path": "", "source": "",
        "reason": "" if providers else "还没有 API 提供方：点这里添加一个",
        "guide": {}, "models": [e["id"] for e in entries if (e["cli"] or "codex") == "api"],
        "providers": providers,
    })
    return {
        "roles": rows,
        "registry": entries,
        "agents": agents,
        "clis": {row["id"]: row["found"] for row in agents},
        "overrides": overrides(),
    }


def parse_command(text: str) -> tuple[str, str, str] | None:
    """解析切换命令，返回 `(动作, 角色, 模型)`；不是切换命令就返回 None。

    只认**显式前缀**——`/model ...` 或「切换模型 ...」。不做自然语言猜测：
    一句「用 python 换实现方式」被当成切模型，会把正在跑的任务悄悄换掉执行者。
    动作：`set` / `reset` / `list`。
    """
    raw = str(text or "").strip()
    low = raw.lower()
    if low.startswith("/model") or low.startswith("model "):
        body = raw.split(None, 1)[1] if " " in raw else ""
    elif raw.startswith("切换模型") or raw.startswith("换模型"):
        body = raw[len("切换模型"):] if raw.startswith("切换模型") else raw[len("换模型"):]
    else:
        return None
    body = body.strip().lstrip("：:").strip()
    if not body or body.lower() in ("list", "ls", "列表", "all"):
        return ("list", "", "")
    # 分隔符：空白 / = / -> / → / 换成 / 改为
    for sep in ("->", "→", "=>", "换成", "改为", "=", "：", ":", " "):
        if sep in body:
            left, _, right = body.partition(sep)
            role = resolve_role(left)
            if role:
                target = right.strip()
                if target.lower() in ("reset", "default", "默认", "恢复"):
                    return ("reset", role, "")
                if target:
                    return ("set", role, target)
    return None