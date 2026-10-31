# 改造契约 v1（第一阶段：聊天骨架）

这份文件是改造期间前端与后端的唯一接口约定。改接口先改这里，再改两边代码。

## 目标形态

```text
┌────────────┬──────────────────────────────┬─────────────┐
│ 工作区      │  顶部：工作区选择器            │ Agent 状态   │
│ 历史会话    │  ─────────────────────────    │ 当前任务     │
│            │  消息流（ChatGPT 式）          │ 测试结果     │
│            │  ─────────────────────────    │ 代码改动     │
│            │  输入框（只收自然语言）         │             │
└────────────┴──────────────────────────────┴─────────────┘
```

用户不再接触任务 ID、命令和复制粘贴。任务卡、验收、复核仍在后台跑，只是通过消息流呈现。

## 1. 存储布局

```text
room/
  sessions/
    index.json              # 会话索引
    chat-001.jsonl          # 每个会话一个 JSONL
  sessions/legacy.jsonl     # 旧 room/messages.jsonl 迁移过来的内容（阶段一兼容读取）
```

`index.json`：

```json
{
  "sessions": [
    {"id": "chat-001", "title": "给登录页加记住我", "workspace": "E:\\my-project",
     "created_at": "2026-10-31T15:00:00", "updated_at": "2026-10-31T15:10:00",
     "message_count": 12}
  ],
  "active": "chat-001"
}
```

## 2. 消息结构（唯一格式，新代码只写这个）

```json
{
  "id": "msg-001",
  "session_id": "chat-001",
  "role": "user | assistant | system",
  "agent": "user | coordinator | planner | executor | tester | reviewer",
  "task_id": null,
  "content": "给登录页加记住我功能",
  "artifacts": [],
  "status": "pending | streaming | complete | failed",
  "created_at": "2026-10-31T15:00:00"
}
```

- `artifacts` 只放引用，不放正文。两类写法：仓库相对路径 `tasks/T-001/exec-01.last.md`、工作区相对路径 `workspace:src/login.py`。
- `task_id` 关联的任务卡仍在 `tasks/<ID>/task.json`，是执行与验收的事实来源。
- 旧消息（`room/messages.jsonl`，字段 `from/kind/text`）由 `session.py` 在读取时映射成新格式，`agent` 取 `from`，`status` 恒为 `complete`，`artifacts` 为空。

## 3. Python 接口

### `agent_team/session.py`

```python
def sessions() -> list[dict]                      # 索引里的会话（按 updated_at 倒序）
def create_session(workspace: str | None = None, title: str = "") -> dict
def active_session() -> dict                      # 没有就建 chat-001
def set_active(session_id: str) -> dict
def append(session_id: str, *, role: str, agent: str, content: str,
           task_id: str | None = None, artifacts: list[str] | None = None,
           status: str = "complete", meta: dict | None = None) -> dict
def messages(session_id: str, after_id: str | None = None, limit: int = 200) -> list[dict]
def update(session_id: str, message_id: str, **fields) -> dict | None   # 用于把 pending 改成 complete
def context(session_id: str, limit: int = 40) -> str                    # 给大脑看的对话摘要
def title_for(session_id: str, fallback: str = "") -> str               # 用第一条用户消息当标题
```

### `agent_team/workspace.py`

```python
def validate(raw: str) -> tuple[Path | None, str]     # (工作区, 错误原因)，严格模式
def normalize(raw: str) -> str                        # 去引号/空格/尾部斜杠
def is_inside_ledger(path: Path) -> bool              # 不许把协作台自己当工作区
def ensure_workspace_scope(workspace: Path, rel: str) -> tuple[bool, str]  # 路径越界检查
```

规则：
- 必须存在、必须是目录、必须是 git 仓库（或能上浮到最近的 `.git` 根）；
- 不许是协作台自己的目录；
- 路径规范化后必须在工作区之内（`ensure_workspace_scope` 用 `resolve()` 后比较前缀，防 `..` 与符号链接逃逸）；
- 任务 ID 只允许 `T-\d{6}-\d{6}`，不符合直接拒绝。

### `agent_team/agents.py`

```python
@dataclass
class AgentResult:
    agent: str
    ok: bool
    content: str
    artifacts: list[str]
    raw: str = ""
    error: str = ""

def run(agent_role: str, session_context: str, workspace: Path, task: dict | None) -> AgentResult
def available() -> list[dict]      # 配置里的 agent 列表（角色、显示名、模型）
```

映射：`planner` → 现有规划逻辑；`executor` → `team.py dispatch`；`tester` → `team.py verify`；`reviewer` → `team.py review`。全部走 `codex_bridge`，不在这里直接拼命令行。

### `agent_team/room_web.py` 必须提供的 HTTP 接口

| 方法 | 路径 | 入参 | 返回 |
| --- | --- | --- | --- |
| GET | `/api/state` | `?session=<id>&after=<msg-id>` | `{session, sessions, workspaces, messages, agents, typing, task, changes}` |
| POST | `/api/post` | `{session_id?, content}` | 新消息 |
| POST | `/api/workspace` | `{path}` | `{ok, workspace, name}` 或 `{ok:false, error}` |
| POST | `/api/session` | `{workspace?, title?}` | 新会话 |
| POST | `/api/session/select` | `{session_id}` | 切换成功 |
| GET | `/api/task` | `?id=<task_id>` | 任务卡 + 测试输出 + diff（右侧面板用） |

`GET /api/state` 的 `changes` 字段（右侧面板）：

```json
{"files": ["src/login.py"], "diff_stat": "1 file changed, 12 insertions(+)", "last_verify": "Ran 5 tests OK"}
```

## 4. 消息通道（阶段一如何呈现过程）

阶段一的流水线不动，只是把每一步发成一条**独立消息**，并带 `artifacts`：

| 时机 | agent | content | artifacts |
| --- | --- | --- | --- |
| 大脑理解完 | `coordinator` | 一句话说理解与计划 | `[]` |
| 建卡 | `planner` | 「拆成 3 步，验收命令是…」 | `tasks/<ID>/plan.md` |
| 执行完 | `executor` | 执行摘要 | `tasks/<ID>/exec-01.last.md`、`workspace:<改动文件>` |
| 验收完 | `tester` | 通过/失败 + 失败用例 | `tasks/<ID>/verify-01.txt` |
| 复核完 | `reviewer` | PASS / NEEDS_FIX + 理由 | `tasks/<ID>/review-01.last.md` |
| 汇总 | `coordinator` | 最终结论与下一步 | `tasks/<ID>/report.md` |

## 5. 验收（第一阶段）

1. 打开网页：左侧有工作区和会话列表，顶部能输入路径并验证，右侧显示 agent 状态。
2. 输入一句自然语言，消息流里依次出现 coordinator → planner → executor → tester → reviewer 的消息，每条带可点开的 artifact。
3. 任务失败时，tester/reviewer 的消息是 `status: "failed"`，界面红字显示原因，不显示"成功"。
4. 旧账本（`room/messages.jsonl`）仍能读出来，不丢历史。
5. 不改任务卡协议与验收机制：`python -m unittest discover -s tests -t .` 全绿。

---

## 进度（更新于 2026-10-31）

### 第一阶段：已完成 ✅

| 交付 | 状态 | 证据 |
| --- | --- | --- |
| `session.py` 会话与消息模型 | ✅ | 36 项测试；旧账本 202 条幂等迁移 |
| `room.py` 新结构 + 旧接口兼容 | ✅ | 旧调用点零改动仍可跑 |
| `workspace.py` 校验与边界 | ✅ | 27 项测试（不存在/文件/非 git/自身/`..` 逃逸/任务 ID） |
| `agents.py` 统一适配层 | ✅ | `run()` 三种角色，artifacts 自动收集 |
| 聊天式三栏工作台 | ✅ | 左工作区+会话、顶选择器、中消息流、右状态面板 |
| 每阶段一条 Agent 消息 | ✅ | coordinator/planner/executor/tester/reviewer 各一条，带 artifacts |
| 失败即失败 | ✅ | 越界/验收失败/复核 NEEDS_FIX 均标红并写 `outcome=failure` |

**真实跑通的证据**（任务 `T-261031-155003`，演示仓库 `E:\scratch-chat-test`）：

```text
user     给 textstats 增加 count_vowels 并补测试
coordinator 我理解成…（一句话）
planner    拆成 4 条验收标准、2 个允许路径
executor   第 1 轮实现完成
tester     第 1 轮验收通过
reviewer   NEEDS_FIX（要求补大小写覆盖）
executor   按复核意见返工（第 2 轮）
tester     第 2 轮验收通过
reviewer   PASS
coordinator 任务走完了：passed
```

任务卡终态：`status=passed`、`outcome=success`、`reviews=[NEEDS_FIX, PASS]`、`finished_at` 有值。

### 第二阶段：已完成 ✅

- ✅ `agents.run(role, session_context, workspace, task) -> AgentResult` 已落地，网页与协调器都不直接认识 Codex。
- ✅ `config/team.json` 有 `agents` 段（五角色、模型、档位、沙箱）。
- ✅ 多任务拆分与依赖排序（`split_cards` / `order_cards`）。
- ✅ `session_context` 注入大脑；规划仍是大脑一次调用，但已能一次给出多张卡。

### 第三阶段：进行中（控制已完成）

- ✅ 暂停 / 继续 / 停止 / 重新执行：`control.py` 状态机 + 引擎逐步闸门 + 右栏按钮。
- ✅ 批准闸门：`policy.require_approval`。
- ✅ 执行中新增需求：进队列并明确回执。
- ✅ 并行分析：风险预审 + 验收口径两份只读分析同时跑（实测 37.3s / 串行 69.2s）。
- ✅ 多任务拆分与依赖排序：`split_cards()` + `order_cards()`，某步失败即停。
- ✅ scope 隔离：每张卡自带 scope，越界改动判 FAIL。

并行分析、scope 隔离下的共享工作区、执行中追加需求入队、暂停/继续/批准/重跑按钮。

### 第四阶段：已完成 6 / 8

| 项 | 状态 |
| --- | --- |
| 执行失败不能显示成功 | ✅ `mark_finished` 要求 `status=passed` 且复核 `PASS` |
| scope 用实际 diff 检查 | ✅ `scope_violations()` 读 `git status`，缓存产物除外 |
| 验收命令结构化、去 `shell=True` | ✅ `parse_verify_argv()`，拒绝 `| & ; < > $ \`` |
| 复核失败不进成功终态 | ✅ `cmd_review` 降级状态 |
| 消息写入加锁 / 原子写 | ✅ `session.py` 用文件锁 + 原子替换 |
| 任务 ID、工作区路径边界校验 | ✅ `workspace.py` |
| 网页访问令牌、默认本机 | ✅ 只绑 `127.0.0.1` + `room/web-token`（头或 `?token=`） |
| wheel 安装后路径失效 | ✅ `paths.workbench_root()` + `AGENT_TEAM_HOME`；干净 venv 实测账本不落 site-packages |
