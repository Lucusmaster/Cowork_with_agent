## 0.8.0 — 长任务不再被误判成死引擎

用户真机跑长任务时抓到的问题：Codex 执行超过 90 秒，`room/worker.lock` 心跳过期，
页面显示"引擎没在跑"、四个控制按钮全灰、`start-all --stop` 也说"本来就没在跑"没杀掉进程。

### 修复
- **独立心跳线程**：worker 主循环被长任务占住时，心跳仍每 10 秒刷新。
  真机验证：150 秒连续执行期间心跳年龄始终 0.6~9.5 秒，界面状态一直是 `running`。
- **判断存活只看进程**：新增 `agent_team/procs.py`（`pid_alive` / `lock_state`）。
  `take_lock` 不再因为心跳旧就抢锁（会双开），`start_all` 的 status/stop 也认进程。
- **`os.kill(pid, 0)` 的地雷**：Windows 上那等于 `TerminateProcess`，会把目标进程直接杀掉。
  worker 里原来就在用，统一换成 `procs.pid_alive()`。
- **心跳原子写**：`write_text` 会被网页读到写了一半的 JSON，改成临时文件 + `os.replace`。
- 界面按 `alive` 启用按钮，心跳旧显示黄色「可能在跑长任务」，不再置灰。

### 新增
- 引擎三态 `running|busy|stalled|dead`，`/api/state.engine` 直接给。
- 规划中占位卡：还没建出任务卡时也能看到"正在规划"和当前需求，暂停/停止可用。
- 消息分页：默认最近 120 条 + 「加载更早」 + 轮询只追加（不再每次全量重画）；
  `GET /api/messages?before=&limit=`（limit 上限 500）。

### 修复（顺带）
- 测试会写真实 `room/worker.log`：`log()` 用的是 import 时缓存的路径，改 ROOM 后仍写真实账本。
  改成每次从 ROOM 现算；全量测试跑完后真实日志字节数不变。

### 验证
- 212 项测试全绿，`ruff check .` 干净。
- 真机：150 秒执行期间心跳不过期、界面不误判；任务中途点停止后，任务记为 `stopped / outcome=failure`。

## 0.7.0 — 会拆活了

### 新增
- **多任务拆分**：一句话里有几件互不相干的改动，就拆成几张卡，按 `depends_on` 拓扑排序依次执行；
  某一步没过就停下问你，不会带病往下跑。
- **并行分析**：规划阶段的风险预审（复核者）和验收口径（验收者）同时跑。
  实测 37.3s vs 串行 69.2s；结果写进任务卡 `prescan.md`，复核阶段可追溯。
  配置 `policy.parallel_analysis: false` 可关闭。
- 每个任务卡多一个 `depends_on` 字段（任务卡协议向后兼容，旧卡不受影响）。

### 修复
- **pip 安装后账本写进 site-packages**：新增 `agent_team/paths.py`，仓库里跑用仓库根，
  装成包后用 `AGENT_TEAM_HOME`（默认 `~/.agent-team`）。真机验证：干净 venv 装 wheel 后建卡，
  落在 `AGENT_TEAM_HOME` 下，`site-packages/tasks` 不存在。
- 大脑给了 `tasks` 数组时被旧守卫当成"拆不出来"直接拒掉（真实跑任务时抓到）。

### 验证
- 177 项测试全绿，`ruff check .` 干净。
- 真实跑通拆分：一次需求 → 2 张卡（spaces / punct）→ 各自验收 + 复核 PASS → "2 步全部做完了"。

## 0.6.1 — 收工兜底

- 引擎启动时补记终态：状态已经是 passed/failed 但缺 `finished_at` 的任务（比如引擎在写终态前被关掉），
  现在会自动补上 `outcome`，等结果的一方不会永远等下去。本次在本机一次性补了 12 个历史悬空任务。
- 新增 4 项测试（总 155 项）。

## 0.6.0 — 过程中能插手了

### 新增
- **运行中控制**：暂停 / 继续 / 停止 / 重新执行（`agent_team/control.py` + 右栏四个按钮）。
  引擎在每个步骤边界消费指令，暂停真的会停住不动文件。
- **批准闸门**：`config/team.json` 的 `policy.require_approval=true` 时，计划出来先等你点头。
- 暂停期间引擎继续心跳，界面不会把你正在等的任务显示成「引擎已死」。
- 执行期间发来的新需求会被明确回一句「已排队」，不会让人以为漏掉。

### 安全
- **访问令牌**：网页只允许本机访问，并要求 `room/web-token`（启动自动生成，`X-Team-Token` 头或 `?token=`）。
  不带令牌打开会显示粘贴卡片而不是白屏。
- 验收命令仍然只走 argv，不经过 shell。

### 修复
- **串味误报**：开发者自己在协作仓库里改 `agent_team/`、`tests/` 时，验收不再误判成"工作区串味"而失败；
  现在只有命中任务 scope 的协作仓库改动才算越界。
- 体检 `doctor` 的端口探活带上令牌，不再把自家网页误报成"被别的程序占着"。
- 启动器打印的链接自带访问令牌。
- 网页测试在 Windows 上偶发 `WinError 10053`：改为连接抖动重试一次。

### 验证
- 151 项测试连续三遍全绿，`ruff check .` 干净。
- 真实任务验证控制链路：执行完成 → 点暂停 → 停在验收前（任务目录文件数不再增长）→ 点继续 → 跑到 `passed / PASS`。

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