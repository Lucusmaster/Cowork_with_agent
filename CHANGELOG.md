## 0.5.0 — 从任务流水线变成对话式工作台

界面与消息协议重做，任务卡内核（建卡/验收/复核/报告）保持不变。

### 新增
- `session.py`：会话与消息模型，`room/sessions/<id>.jsonl` 每会话一份；旧 `room/messages.jsonl` 幂等迁移，历史不丢。
- `agents.py`：统一 Agent 适配层 `run(agent_role, session_context, workspace, task) -> AgentResult`，换模型/换执行器只改这一层。
- `workspace.py`：工作区校验（存在、目录、git 根、不许是协作台自身）、路径越界检查、任务 ID 格式校验。
- 聊天式工作台：三栏布局、工作区选择器、历史会话、消息流、artifact 预览、右侧 Agent 状态/测试结果/代码改动。
- 每个流水线阶段发一条独立 Agent 消息（coordinator/planner/executor/tester/reviewer），带 artifacts 引用。

### 可靠性
- 验收命令改为 argv 列表执行，拒绝 shell 元字符，注入面消失（`parse_verify_argv`）。
- scope 用真实 `git status` 核对，越界改动直接判 FAIL；缓存产物（`__pycache__`、`*.pyc`）不算越界。
- 复核 `NEEDS_FIX` 会降级任务状态；成功终态必须有 `PASS` 复核作证，否则 `outcome=failure`。
- `reset_ledger` 现在会清 `room/sessions/`、`room/projects/`、`nudge`、`typing.json`。
- wheel 安装后 `config/`、`agents/` 走内置副本（`agent_team/bundled/`），加了漂移守卫测试。

### 修复
- `worker.do_report` 里 `import team` 在包结构下必失败（每次都要退回子进程），改成包内导入。
- `team.py` 建卡先建目录后校验 `--workspace`，参数非法会留下空任务目录。
- 复核提示词里未定义的 `{tid}`（`F821`）。

### 实跑验证
- 单测 110 项全绿，`ruff check .` 干净。
- 真实任务跑通完整闭环：NEEDS_FIX → 自动返工 → 验收通过 → 复核 PASS，任务终态 `passed / outcome=success`。

# 更新日志

## 0.4.0 — 开源首发（[Lucusmaster/Cowork_with_agent](https://github.com/Lucusmaster/Cowork_with_agent)）

首次公开发布：从自用脚本集整理成可安装、可参与的项目。

### 从脚本集变成项目

### 交互
- 群聊模式：每条用户消息都由大脑读整段对话 + 项目设置后回话，再决定 `chat / set_project / run / continue`。
- 模糊需求可接：按最合理理解动手并在群里说明假设；连项目都没绑才追问。
- 项目绑定：`room/project.json` 记工作区与项目名，页面底栏显示。
- 立刻响应：发消息即写 `room/nudge`，引擎 0.5 秒内认领（实测 0.36s）。
- 正在思考：处理期间 `room/typing.json` 亮起，页面显示三点跳动指示器；发送方先看到自己的话，不等服务器。
- 各角色发言进同一条消息流：Codex 汇报、复核结论都以各自身份贴进群。

### 阶段推进
- 「继续」接上一阶段：自动带报告、失败用例原文、复核意见、工作区状态、跨阶段台账。
- 新卡记 `stage` / `continues` / `unfinished_after`；每阶段写 `handoff.md` 与 `PROGRESS.md`。

### 守卫（都是踩坑换来的）
- 工作区绑定：路径 → 最近的 `.git` 祖先；缺失/多路径/不存在一律拒绝并追问。
- 账本基线：验收只报本轮新增/变更，避免把账本仓库的脏文件算进产物。
- 工作区基线：派发前记录未提交改动与 `baseline.diff`，复核不再误判"越界"。
- 无黑框：所有子进程带 `CREATE_NO_WINDOW`，并有测试守住。
- 意图判定收紧：带问号/冒号/逗号的闲聊不再被当成「继续」白跑流水线；问句不擅自开工。

### 工程化
- 代码迁入 `agent_team/` 包，`bin/` 保留兼容壳（老命令仍可用）。
- `pyproject.toml` + `agent-team` 统一命令（start/stop/status/doctor/task/say/tail/wait）。
- 文档：`docs/ARCHITECTURE.md`、`docs/ROADMAP.md`、本文件、`CONTRIBUTING.md`。
- 清理仓库根目录的历史临时目录。

## 0.3.0 及更早

- 三进程协作（网页 + 引擎 + Codex），任务卡状态机，验收看证据。
- 关网页即收工（无活动任务时自动停），`--no-autostop` 可保留后台。
- 工作区绑定与防污染基线。
- 修复：复核/报告步骤在黑窗口模式下的 STATUS_CONTROL_C_EXIT 崩溃。