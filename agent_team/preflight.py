"""下单前置校验：把「这台机器现在能不能真的跑完一张卡」拆成可判真假的检查项。

只做判断、不做修复，也不自己发 HTTP：引擎/端口/工作区这些要联网才知道的事，
由调用方（mcp_server 读 /api/state、doctor 读本地）把数据喂进来。
这样一套规则两个入口共用，不会出现「doctor 说行、MCP 说不行」。

四项检查对应外部 agent 最容易踩的四种坑：模型拼错、降级链形同虚设、
工作区绑到账本里、引擎根本没在跑。每项都带机器可读的 `key` 和一句可执行的 `fix`。
"""
from __future__ import annotations

from pathlib import Path

from . import models
from .paths import workbench_root  # noqa: E402
from .workspace import is_inside_ledger, normalize, validate

LEVELS = ("ok", "warn", "bad")
ROOT = workbench_root()          # 账本根；测试里可以替换


def item(level: str, key: str, name: str, detail: str, fix: str = "") -> dict:
    """一条检查结果。`key` 给机器判重试/放弃，`fix` 给外部 agent 照着做。"""
    return {"level": level, "key": key, "name": name, "detail": detail, "fix": fix}


def _label(role: str) -> str:
    return models.ROLE_LABELS.get(role, role)


def _available() -> str:
    return "、".join(models.models()) or "（注册表为空，先配 config/team.json）"


def check_models() -> list[dict]:
    """executor / reviewer / planner 的主模型和降级链逐个过一遍。

    主模型坏了是 `bad`（下单必然失败）；降级链里某条坏了只是 `warn`
    （主模型还能用，只是撞上容量满时这条降级会失效）。
    """
    rows: list[dict] = []
    for role in ("executor", "reviewer", "planner"):
        label = _label(role)
        model, source = models.model_for(role)
        if not model:
            rows.append(item("ok", f"model.{role}", label, "默认模型（没指定，交给 CLI 自己挑）"))
        else:
            resolved = models.resolve(model)
            if not resolved:
                rows.append(item("bad", f"model.{role}.unknown", label,
                                 f"模型「{model}」不在注册表；可用：{_available()}",
                                 fix=f"python -m agent_team.cli model set {label} <型号>"))
            else:
                cli = str((models.registry().get(resolved) or {}).get("cli") or "")
                if role == "executor" and cli == "api":
                    rows.append(item("bad", "model.executor.api_not_allowed", label,
                                     "执行者是 API 模型：它只能收发文字，改不了文件，每轮都会空跑",
                                     fix="把执行者换回本机 CLI 模型（协调/规划/复核可以用 API 模型）"))
                else:
                    ready, why = models.cli_ready(resolved)
                    if not ready:
                        rows.append(item("bad", f"model.{role}.cli", label, why,
                                         fix=f"装好/登录它对应的 CLI，或 python -m agent_team.cli model set {label} <别的型号>"))
                    else:
                        note = f"（{source}）" if source and source != "配置" else ""
                        rows.append(item("ok", f"model.{role}", label, f"{resolved}{note}"))

        for name in models.fallback_models(role):
            resolved = models.resolve(name)
            if not resolved:
                rows.append(item("warn", f"model.{role}.fallback.{name}", f"{label}降级链",
                                 f"「{name}」不在注册表，撞上模型容量满时这条降级会失效",
                                 fix="在 config/team.json 里改掉这个型号，或先在 model_registry 登记它"))
                continue
            if role == "executor" and (models.registry().get(resolved) or {}).get("cli") == "api":
                # 执行者的降级链里挂 API 模型，等于"满载时换一个改不了文件的模型"，是坏配置
                rows.append(item("bad", f"model.executor.fallback.api_not_allowed.{resolved}",
                                 f"{label}降级链",
                                 f"{resolved} 是 API 模型，改不了文件；执行者满载时切过去只会空跑",
                                 fix="把它从 executor.fallback_models 里去掉，或换成 CLI 模型"))
                continue
            ready, why = models.cli_ready(resolved)
            if not ready:
                rows.append(item("warn", f"model.{role}.fallback.{resolved}", f"{label}降级链",
                                 f"{resolved} 不可用：{why}", fix="装好对应 CLI 或换一个降级型号"))
    return rows


def check_fallback_health() -> list[dict]:
    """只管执行者的降级链：没有降级链是提醒，整条都不可用才阻塞。"""
    chain = models.fallback_models("executor")
    if not chain:
        return [item("warn", "fallback.executor.empty", "执行者降级链",
                     "执行者没配降级链，撞上模型容量满会直接空跑一轮",
                     fix='在 config/team.json 的 executor.fallback_models 里加两个型号，'
                         '例如 ["gpt-6.1-sol", "gpt-6-luna"]')]
    usable = [name for name in chain if models.resolve(name) and models.cli_ready(name)[0]]
    if not usable:
        return [item("bad", "fallback.executor.all_dead", "执行者降级链",
                     f"降级链上的型号都不可用（{'、'.join(chain)}），主模型一满就整轮空跑",
                     fix="在 config/team.json 里换成本机能跑的型号")]
    detail = f"可用降级：{'、'.join(usable)}"
    dead = [name for name in chain if name not in usable]
    if dead:
        detail += f"（不可用：{'、'.join(dead)}）"
    return [item("ok", "fallback.executor", "执行者降级链", detail)]


def check_workspace(workspace: str) -> list[dict]:
    """工作区：空、非法、落在账本里、不可写——四种都别等跑到一半才发现。"""
    raw = normalize(workspace)
    if not raw:
        return [item("bad", "workspace.unbound", "工作区", "还没绑定工作区",
                     fix="先在群里说「工作区是 X:\\某个仓库」")]
    path = Path(raw).expanduser()
    # 相对路径只能按进程当前目录解析，历史上的绑错工作区就是这么来的：先让 validate 说
    # 「必须是绝对路径」，别把它当成"落在账本里"——后者会误导人去查账本。
    if path.is_absolute() and is_inside_ledger(path):
        return [item("bad", "workspace.in_ledger", "工作区",
                     "工作区落在协作台账本里，产物会污染仓库",
                     fix="换一个独立的项目目录当工作区")]
    found, why = validate(raw)
    if found is None:
        return [item("bad", "workspace.invalid", "工作区", why,
                     fix="先建好目录再绑定工作区，路径要写绝对路径")]
    probe = found / ".write-probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return [item("bad", "workspace.readonly", "工作区", f"写不了 {found}：{exc}",
                     fix="换一个可写目录，或修好这个目录的权限")]
    return [item("ok", "workspace", "工作区", str(found))]


def check_engine(state: dict | None, root: str | Path = "") -> list[dict]:
    """引擎在不在、是不是本仓库的实例。

    `state` 是 `/api/state` 的返回，本地调用传 `None`。**只认 `engine.alive`**：
    `_state_payload` 在引擎不在时会把 `busy` 清成 idle，拿 `typing`/`busy` 判断会得到假信号。
    """
    if state is None:
        return [item("bad", "engine.offline", "协作台引擎", "协作台没在跑（/api/state 连不上）",
                     fix="用 team_launch 起协作台")]
    engine = state.get("engine") if isinstance(state, dict) else None
    engine = engine if isinstance(engine, dict) else {}
    if not engine.get("alive"):
        return [item("bad", "engine.dead", "协作台引擎",
                     "引擎进程不在了（端口也许还开着，页面能打开但任务不会动）",
                     fix="用 team_launch 重启，或看 room/worker.log")]
    served = str(state.get("root") or "").strip()
    if not served:
        return [item("warn", "engine.root_unknown", "协作台引擎",
                     "接口没返回 root，无法确认是不是这个仓库的实例")]
    root_path = str(root or ROOT)
    try:
        same = Path(served).resolve() == Path(root_path).resolve()
    except OSError:
        same = False
    if not same:
        return [item("bad", "engine.other_repo", "协作台引擎",
                     f"被另一个仓库的实例占着（{served}）",
                     fix="换一个端口起本仓库的实例，或直接用那个仓库")]
    return [item("ok", "engine", "协作台引擎", f"{served} 在跑")]


def run(workspace: str = "", state: dict | None = None, *,
        include_remote: bool = True) -> dict:
    """跑全部检查，返回 `{"ok", "items", "blocking", "warnings"}`。

    `blocking` 的顺序就是修复建议的顺序：引擎 → 工作区 → 模型。
    引擎没起来时工作区和模型的报错多半是连带的，别让调用方从末端开始修。
    `include_remote=False` 只跑本地模型检查（服务没起来时 doctor 用）。
    """
    items: list[dict] = []
    if include_remote:
        items += check_engine(state, ROOT)
        items += check_workspace(workspace)
    items += check_models()
    items += check_fallback_health()
    blocking = [row for row in items if row["level"] == "bad"]
    warnings = [row for row in items if row["level"] == "warn"]
    return {"ok": not blocking, "items": items, "blocking": blocking, "warnings": warnings}


__all__ = ["LEVELS", "item", "check_models", "check_fallback_health",
           "check_workspace", "check_engine", "run"]