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