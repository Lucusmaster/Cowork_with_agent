## 0.9.31 — 降级跑成了，却被上一个型号留下的"连不上"便签误杀

0.9.30 上线后第一次实测就抓到自己的 bug：T-261102-205317 第 2 轮里，
`gpt-6.1-sol` 容量满载（还伴随 `failed to refresh available models`）→ 自动降级
`gpt-6-luna` 跑成、`exec` 退出码 0、文件确实改对了；可编排读到第一轮/第一个型号
留下的 `exec-02.hint.txt`，仍判"端点不可达"直接停手，一张已经干完的卡被记成 failed。

### 1. 三处改动

- **编排只看退出码再读便签**（`agent_team/worker.py` 的 `_env_gate()`）：`code == 0`
  时一律忽略便签——跑成了（哪怕中途降级过）说明文件已经改完，便签是噪声。
- **成功就把便签删掉**（`agent_team/codex_bridge.py` 的 `run_codex()`）：降级后整体成功
  时清空 `env_error` 并删掉 `.hint.txt`，别把上一个型号的失败留给下一轮判断。
- **派发前清旧便签**（`agent_team/team.py` 的 `cmd_dispatch`）：同一轮次重派时先
  `unlink` 掉 `exec-NN.hint.txt`，不拿上一回的结论判这一回。

### 2. 证据

- 新增 3 项测试：成功轮忽略旧便签（`_env_gate(tid, n, 0) == ""`）、退出码非 0 才停手、
  整体成功时删掉便签并清空 `env_error`。
- 全套 726 项通过（`python -m unittest discover -s tests -t .`），`ruff check agent_team tests` 干净。
- 实测核对那张卡第 2 轮的产物：`crankSpeed=0.126 度/ms` 是上一版 `360/1400≈0.2571`
  的 **0.49 倍**；静态踏板 `(563,405)`、`(417,405)` 到曲柄中心 `(490,405)` 的距离都正好
  **73px**（= `pedalRadius`），首帧不再跳；注释里写着 `1400`/`360`/`crankSpeed` 的换算。
  也就是说复核提的两条，执行者第 2 轮真的修好了。

## 0.9.30 — 不再「写完 plan.md 就停」：端点可配、连不上立刻停手、超时真能杀

用户实测反馈：这一轮在 `plan.md` 写完之后就不动了，干等四分钟后直接报错，代码一行没写。
查下来是**三个叠在一起的问题**，这次一并修掉。

### 1. 抓到的三层原因

1. **执行者根本没连上模型端点。** `~/.codex/config.toml` 里只有 `model`/`effort`，没有任何
   `[model_providers]`；这台机器直连 `chatgpt.com` / `api.openai.com` 超时，
   `exec-*.err.txt` 里是 `workspace routing discovery timed out`、
   `error sending request for url`，`--output-last-message` 落出 0 字节。
2. **编排把"环境失败"当成"代码没写对"。** 验收自然不过 → 自动返工第二轮 → 再撞同一个网络，
   于是用户看到的是"两轮 × 四分钟"的静默，压根不是"卡在计划阶段"。
3. **超时在 Windows 上根本杀不掉。** 换到可用端点后暴露出来：执行者 900s 超时后，
   `subprocess.run(timeout=)` 只 kill 直接子进程，收尾那次 `communicate()` 没有超时，
   孙进程还握着 stdout 管道 → dispatch 又挂了 20 分钟不返回，编排也跟着吊死。

### 2. 改了什么

- **端点按型号配**（`agent_team/models.py` 的 `model_provider()` + `agent_team/codex_bridge.py`）：
  `room/model_providers.json` 优先、`config/team.json` 的 `model_providers` 兜底，给单个型号写
  `base_url` / `env_key` / `key_store` / `wire_api`；桥把它翻成
  `-c model_provider=workbench -c model_providers.workbench.*` 并注入密钥。
  **不碰 `~/.codex/config.toml`**——这台机器上别的 Codex 用法、CC Switch 的 Claude 路由都不受影响。
  密钥优先读环境变量，读不到就取 API 菜单里存过的那份（`room/api_keys.json`），配置文件里不放 key。
  `wire_api` 只支持 `responses`：Codex 0.166 起 `chat` 已被移除，写 `chat` 会直接报错退出。
- **连不上就停手**（`codex_bridge.classify_env_failure()` + `worker._stop_env_blocked()`）：
  认 Codex 自己的网络报错措辞（`workspace routing discovery timed out` /
  `failed to refresh available models` / `error sending request for url` / `transport channel closed` /
  `failed to connect` / `connection refused`），命中就写 `tasks/<id>/exec-NN.hint.txt`、
  在 `rounds[]` 里记 `env_error` 与 `endpoint`、群里发一条说明；编排看到便签**只跑一轮就停**，
  不验收、不返工、不烧第二遍，并在卡上留 `note` 讲清怎么修。
- **提示直接说人话、能照着做**：不再只说"连不上端点"，而是写清目标端点
  （没配中转时点名 `OpenAI 默认端点（chatgpt.com / api.openai.com）`）、点出最常见的原因
  （没开 VPN/代理，或者**代理只接管了浏览器、没接管命令行里的 codex**），然后给三条替代路：
  ① 连上 VPN 后把这张卡重投（不用改配置）；② 换本机可达的模型（网页右栏模型徽章，或群里
  `/model 执行 <型号>`，并附上当前可选型号清单）；③ 在 `room/model_providers.json` 里给这个型号配中转端点。
- **超时真能杀**（`agent_team/winproc.py` 的 `run_capped()` / `kill_tree()`）：
  `subprocess.run(timeout=)` 换成 Popen + `communicate(timeout)`，超时先 `taskkill /PID … /T /F`
  杀整棵进程树，再最多等 10s 收尾；收尾还收不回来就关管道返回 `124`。
  Codex 桥和编排的 `run_step()` 都改走它；编排给执行者单轮留 1900s（比 `executor.timeout` 的 1800s 略大，
  否则编排先把自己的子进程掐了，执行者的日志就全丢了）。
- **默认 `executor.timeout` 900 → 1800s**：走中转时一轮就是很慢，900s 会把正常的轮次掐掉。
- **顺带两处提示修正**：执行者模板里写死的「模型 gpt-6-luna」改成「任务卡指定的模型」；
  prompt 里的「当前工作目录」改成真实产物工作区（之前写的是协作台仓库根）。

### 3. 证据

- 新增 13 项测试：`tests/test_codex_bridge.py`（端点表读取、`-c` 覆盖、密钥从本机存储注入、
  真跑一次假 codex 看 argv/env、网络失败分类、便签落盘、便签读取）与
  `tests/test_capped_run.py`（**孙进程握着管道时也必须几秒内返回**、正常命令输出、stdin 透传、缺 timeout 报错）。
  第一项就是这次 20 分钟挂死的回归守卫。
- 全套 722 项通过（`python -m unittest discover -s tests -t .`），`ruff check agent_team tests` 干净；
  `config/team.json` 与 `agent_team/bundled/config/team.json`、`agents/executor.md` 与内置副本已同步。
- 实测：`room/model_providers.json` 把 `claude-sonnet-5` 指到本机可达的中转后，
  同一句鹈鹕需求重投，执行者 `codex exec` 的 argv 里带上
  `-c model_providers.workbench.base_url="https://api.xunnia.com/v1"`，
  真的开始改 `E:\测智商\pelican_bicycle.html`（不再是 0 字节 last message）。

## 0.9.29 — API 那栏「环境变量名」的坑：小写/留空/贴错 key 都不再报错

用户实测反馈：在 API 组里填 DeepSeek 官方地址后，把 `sk-...` 贴进「key 所在的环境变量」
那一栏，得到一句"环境变量名只能是大写字母/数字/下划线"——报错没错，但意思没传达到，
而且用户想做的只是"把 key 存上"。

### 1. 三处改动（`agent_team/api_providers.py`）

- **大小写都收**：输入 `deepseek_api_key` 也认，存之前统一转大写
  （Windows 环境变量不区分大小写，转大写后 POSIX 上也能对上）。
- **留空就自动填**：不填变量名时用 `<提供方>_API_KEY`（`deepseek` → `DEEPSEEK_API_KEY`），
  不必先想好名字。
- **贴错位置自己纠**：如果那一栏里明显是 key（`sk-`/`token-` 开头，或一长串带 `-`/`.` 的东西），
  不再报错，而是**当 key 存进本机**、变量名退回默认，并在提示里说明"你把 key 贴在了变量名栏"。
  真写错的（带空格之类）报错里直接指路："想贴 key 的话请用「直接贴 key」那一栏"。

### 2. 表单顺手少一步（`agent_team/room_web.py`）

- 「直接贴 key」输入框改成**新建时也可见**，和提供方一起保存（`save` 请求多带一个 `key`），
  存完就能直接「拉取模型」——不用"先存提供方、再回头贴 key"两趟。
- 变量名输入框的占位符写明"留空自动填"；贴 key 的框直接写"贴在这里，不要贴到上面变量名栏"。
- 已保存的提供方仍有「单独保存 key / 清除本机 key」两个按钮。

### 3. 证据

- 新增 5 组测试：小写变量名转大写、留空默认名、把 key 贴进变量名栏会被存成 key 且
  `room/api_providers.json` 里没有 key、一次请求同时存提供方 + key 后能立刻拉模型、
  真正写错的变量名报错里带"直接贴 key"指路。
- 全套 709 项通过；`ruff` 干净；页面内联 JS `node --check` 通过。

## 0.9.28 — 换协调者：点一下就换，引擎自己重启（先给提示）

0.9.27 让协调者可以运行时切换了，但用户还得自己 `cli stop/start` 才觉得"真的生效了"。
这次把这个动作收进界面：点选协调者的模型 → 先弹一句确认（说清会重启、约 3 秒）→
切换后**空闲就把引擎重启掉**，不用再开终端。

### 1. 什么时候才重启（`room_web._engine_busy`）

- 有没跑完的任务卡（`start_all.active_cards()`）→ **不重启**，只切换（下一条消息生效）；
- 引擎正在回复/规划（`room/typing.json` 且 3 分钟内）→ **不重启**；
- 都空闲才重启：先杀旧引擎、等它真退出、再拉起新的（`start_all.restart_engine()`），
  最多等 8 秒心跳；失败也只在消息里说明并写一条群事件，不影响切换本身。

这样既回答了"点完自己重启"的需求，又不会在任务跑到一半时把它连根拔掉——
重启本来只是顺手让状态干净，不是切换生效的前提。

### 2. 界面

- 协调者的模型列表顶部加一行说明；点任何型号（含「恢复配置默认」）先 `confirm`：
  "切换协调者（大脑）会顺手重启引擎，约 3 秒。有任务或回复在跑时只切换、不重启……"
- 等接口期间先 toast「正在切换并重启引擎，约 3 秒…」，返回后再报最终结果
  （含"引擎已重启 (pid …)"或为什么没重启）。

### 3. 别的角色不受影响

规划 / 执行 / 复核的切换只写 `room/model_overrides.json`，**从不重启引擎**（派发时现读）。

### 4. 证据

- `tests/test_procs.py`：`active_cards()` 只列未结束的卡；`restart_engine()` 先杀旧进程再拉起
  新进程（打桩 `kill_pid` / `spawn` / `worker_pid`，不碰真进程）。
- `tests/test_model_api.py`：协调者切换在"忙"时只切换（`restart == ""`）、在"空闲"时调用
  `restart_engine` 并回报 pid、其它三个角色**从不调用**重启；页面里必须有确认文案。
- 全套 704 项通过；`ruff` 干净；页面内联 JS `node --check` 通过；技能文档三份已重装。

## 0.9.27 — 协调者（大脑）也能换模型了

`COORDINATOR_API_SPEC.md` 的 A 部分。之前右栏「协调」徽章上显示的模型和真正调用的模型
**对不上**：`brain_decide()` 里写死 `model="gpt-6.1-terra", reasoning_effort="low"`，
配置里的 `coordinator.model` 和运行时切换全被忽略。现在统一走和规划/执行/复核同一条链。

### 1. 为什么以前标"不可切"的理由不成立

原来的说法是"换它要重启引擎"。实际上 `brain_decide()` 每条用户消息才调一次 `run_codex`，
没有常驻会话、没有缓存模型——所以"切完下一条消息生效、不用重启"是成立的，
这个限制纯属历史遗留。

### 2. 改了什么

- `models.py`：`SWITCHABLE` 加 `coordinator`，`UNSWITCHABLE` 只留 `tester`；
  `UNSWITCHABLE_REASON` 改指 tester（原来那句会让模块导入直接 KeyError）；
  模块 docstring 同步。
- `worker.py`：新增 `_brain_model()`（`models.model_for("coordinator")`，没有值才落到
  `gpt-6.1-terra`；effort 用 `models.effort_for("coordinator") or "low"`），
  `brain_decide()` 改用它，并把 `fallback_models=models.fallback_models("coordinator")` 传下去
  ——大脑现在也走容量降级链。
- 前端不用改：徽章读的是 `snapshot().roles[].switchable`，协调者变成可点之后，
  菜单、置灰、来源标签自然就对上了。

### 3. 边界没变

- `tester` 仍然不可切（不调模型）；切换仍然"下一次调用生效"，不打断正在跑的任务。
- API 模型可以当协调者（配合 0.9.25 的第四组菜单 / 0.9.26 的贴 key），
  但**仍然不能当执行者**，三处拦截都在。

### 4. 证据

- 新增 `tests/test_agent_clis.py::BrainWiringTests`：`_brain_model()` 读运行时切换；
  **打桩 `run_codex` 断言 `brain_decide()` 真的把切换后的 model / effort / 降级链传出去**
  （不是只测"配置读到了"）；源码里不再出现写死的 `model="gpt-6.1-terra"`。
- 三处旧断言（`test_models` 两处、`test_model_api` 一处）改成断言可切；
  `/api/set-model` 切协调者后 `room/model_overrides.json` 里出现 `coordinator` 键。
- 全套 699 项通过；`ruff` 干净；`skills/` 与 bundled 镜像同步，`install-skills` 已重装三份。

## 0.9.26 — key 直接在菜单里贴：不用设环境变量，也不用重启

0.9.25 把 API 通道做通了，但给 key 的路只有一条：`setx` 设环境变量，**再重启协作台**。
这两步都卡人：`setx` 不生效（正在跑的进程继承不到新变量）、重启还得换引导链接。
所以补一条不折腾的路——在菜单里直接把 key 粘进去，存本机、马上能用。

### 1. key 存哪、怎么生效（`agent_team/api_providers.py`）

- 新增 `room/api_keys.json`（在 `/room/*` 忽略规则里，**不进 git**；写入后尽力 `chmod 600`），
  只存 `提供方 id → key`。这点变化要说清楚：**key 会落本机明文文件**，和别家 CLI 的
  `auth.json` / `.aws/credentials` 同一类风险；多人共用同一台机器时，仍建议用环境变量。
- 两条路都保留，**环境变量优先**：`key_for()` 先看环境变量，没有才读本机文件。
  `status()` 多返回一个 `key_source`（`env` / `file` / 空），界面上分别显示"已读到（环境变量 X）"
  "已保存在本机（立即生效）"。
- `save_key(pid, key)` / `clear_key(pid)`：校验提供方存在、key 非空、≤512 字符、不带换行；
  删除提供方时连同本机保存的 key 一起清掉。`clear_key` 只清本机那份，环境变量不受影响。
- key **依旧不出现在任何返回值、群事件、日志、快照里**；上游报错回显请求头时照旧先抹掉。

### 2. 菜单里那一格（`agent_team/room_web.py`）

- 提供方表单里多一个 `type="password"` 的「直接贴 key」输入框 + 「保存 key」/「清除本机 key」，
  保存后**重画整个表单**，key 不在页面上停留。
- 新增两条路由：`POST /api/api-provider/key`（`{id, key}`）和 `POST /api/api-provider/clear-key`（`{id}`），
  和别的写接口一样走令牌 + `_write_guard`；成功的动作会往群里记一条事件（不含 key）。
- 没有 key 时的提示改成两选一："在提供方里直接贴（立即生效、不用重启），或者设好环境变量再重启协作台"。

### 3. 证据

- `tests/test_api_providers.py` 从 24 项加到 32 项：本机保存的 key 立即生效（不设环境变量）、
  环境变量优先于本机文件、`clear_key` 不连带清环境变量、空 key/多行 key/未知提供方被拒、
  删提供方连 key 一起清、快照里 grep 不到 key、路由保存后能直接拉模型且响应不含 key、
  六条 `/api/api-provider/*` 路由不带令牌 401。
- 真机演练（本地 stub `/models`，**完全没设环境变量**）：保存提供方 200 → 未贴 key 拉取 400 →
  贴 key 200 且响应不含 key → 拉取拿到 `['drill-chat']` → 勾选 200 → `/api/state` 有
  `drill:drill-chat`、无 key → 规划者切换 200 → 执行者 400 → 清除 key 后再拉 400。
- 全套 696 项通过；`ruff check agent_team tests` 干净；页面内联 JS `node --check` 通过；
  `skills/` 与 bundled 镜像已同步，`install-skills` 已重新安装三份。

## 0.9.25 — 第四个模型组：API（OpenAI 兼容）

换模型菜单原来只有 Codex CLI / Claude Code / DeepSeek 三组，都得本机装 CLI。想在规划、
复核上用 DeepSeek 官方接口或中转站，之前只能改配置、自己拼模型 id，拼错了要跑到下一张卡
才炸。现在菜单里多一个「API（OpenAI 兼容）」组：填地址和环境变量名，点「拉取模型」，
勾选真实型号就能切给角色。

### 1. 提供方登记（`agent_team/api_providers.py`）

- `room/api_providers.json`（gitignored）只存 `label / base_url / api_key_env / models`——
  **没有 key 本身**；`key_for()` 是唯一读 key 的地方，只在发请求那一刻从 `os.environ` 现读。
- 地址校验用 `urlsplit` 的**主机名**：只放行 https，本机 http 只认 `127.0.0.1` /
  `localhost` / `[::1]`，所以 `http://localhost.evil.com` 会被拒；地址里带 `@` 用户密码也拒。
- `fetch_models()` 调 `GET {base_url}/models`，10 秒超时、最多读 1 MB、只取 `data[].id`；
  401/403 报「key 无效（环境变量 X）」、404 提示 base_url 可能要带 `/v1`、超时报主机名，
  **任何一句信息里都不出现 key 的值**（上游报错回显请求头时也会先抹掉）。
- 提供方 id 会做模型前缀：`deepseek:deepseek-chat`。中转站的型号常常也叫 `gpt-*`，
  不加前缀会和本机 CLI 的型号撞名，两个提供方有同名型号时也分不清发到哪个地址。

### 2. 注册表与角色限制（`agent_team/models.py`）

- `registry()` 在"本机登记"之后并入 API 模型（`setdefault`，撞名时本机 CLI 的条目不动）；
  静态配置里也可以写 `cli: api` + `provider` + `remote_id`。
- `cli_ready()` 对 `cli == "api"` **不联网、不碰 CLI**：提供方在不在、环境变量里有没有 key。
  没 key 时原因里带变量名和"设置后要重启协作台"——`setx` 之后正在跑的进程读不到新变量。
- `snapshot()["agents"]` 补一行伪 Agent `id == "api"`（带 `providers`），前三组一个不少。
- `set_model()`、`preflight.check_models()`（含执行者降级链）、菜单三处都拦
  "API 模型当执行者"：它只能收发文字、改不了文件，当执行者每轮都会空跑。

### 3. 调用与路由（`agent_team/api_bridge.py`）

- `run_api()` 返回和 `run_codex` 同形的结果，同样写 `.last.md` / `.out.txt` / `.err.txt`；
  请求体里的 `model` 是**远端型号**（`deepseek-chat`），不是带前缀的注册表 id；
  推理档位不用（响应里如实写"已忽略"）。
- `codex_bridge.run_codex()` 的 attempt 闭包加 `route == "api"` 分支，降级链照旧工作；
  429/503 在 `.err.txt` 写一行带 `at capacity` 的说明，`fallback.is_capacity_error()` 直接复用。

### 4. 接口与界面（`agent_team/room_web.py`）

- 新增 `POST /api/api-provider/{save,fetch,models,delete}`，和 `/api/agent-cli/*` 一样走
  令牌 + `_write_guard`；成功的 save / models / delete 会往群里记一条事件。
- 菜单新增「API（OpenAI 兼容）」分组：提供方列表（地址、key 状态、模型数）→ 添加/编辑表单
  （预设 DeepSeek / Ollama / 自定义，四个字段）→ 保存 → 拉取模型 → 勾选 → 保存勾选。
  **没有填 key 的输入框**，旁边写明"key 放在环境变量里，协作台不保存 key"。
- 当前角色是执行者时，api 组置灰并提示"不能改文件，只能做协调/规划/复核"；
  `/models` 返回的型号 id 来自外部服务，插入页面前一律过 `esc()`。

### 证据

- 新增 `tests/test_api_providers.py`（24 项）：地址白名单（含 `localhost.evil.com`）、
  拉模型解析/401/404/网络错/地址被改坏、`save_models` 过滤非法 id、注册表前缀与撞名、
  没 key 不就绪、snapshot 保留三个 CLI 行、snapshot 与 `/api/state` 里 grep 不到假 key、
  `set_model` 拒绝执行者但接受规划者、被占用时拒绝删除、请求体用远端型号、
  四种路由不带令牌 401。`urllib.request.urlopen` 全部打桩，**不联网**。
- 真机演练（本地起一个 stub `/models` 服务，走真实 HTTP 路由）：保存 → 拉取 → 勾选 →
  `/api/state` 里出现 `drill:drill-chat` 且没有假 key → 规划者切换 200 → 执行者 400 →
  被占用时删除 400。
- 全套 688 项通过；`ruff check agent_team tests` 干净；`skills/` 与
  `agent_team/bundled/skills/` 镜像已同步；`room/*` 本来就在 `.gitignore` 里，提供方文件不会进版本库。

## 0.9.24 — 下单前先体检：闸门排在发消息之前

外部 agent 走 MCP 下单时，环境坏着也照样把需求发进群：消息一发，大脑就被激活开始规划，
轮次和额度开始烧，往往烧完一轮才在日志里看到"模型拼错了 / 引擎没在跑 / 工作区是空的"。
`doctor` 也帮不上忙——它只把配置里的模型名打印出来，拼错的型号照样报 ✓。

### 1. 新增 `agent_team/preflight.py`（纯函数，不发 HTTP）

把「这台机器现在能不能真的跑完一张卡」拆成四项可判真假的检查，每项带机器可读的
`key` 和一句可执行的 `fix`：

| 检查 | 拦什么 | key |
| --- | --- | --- |
| 模型 | 主模型不在注册表 / 背后 CLI 跑不了（executor、reviewer、planner 各查一遍） | `model.<role>.unknown`、`model.<role>.cli` |
| 降级链 | 链里每个 id 逐个查；坏一条只是提醒 | `model.<role>.fallback.<id>` |
| 工作区 | 空、不存在、落在台账本里、写不进去（探针写一个 `.write-probe` 再删） | `workspace.unbound/.invalid/.in_ledger/.readonly` |
| 引擎 | 没在跑、进程没了、端口上是别的仓库的实例 | `engine.offline/.dead/.other_repo` |

`blocking` 的顺序就是修复顺序（引擎 → 工作区 → 模型）：引擎没起来时后两项多半是连带的，
别让外部 agent 从末端开始修。只做判断、不做修复——不自动起引擎、不自动改配置。

### 2. 闸门与入口

- `mcp_server.team_task_create` 在 `POST /api/post` **之前**跑体检，不过就抛错：
  「环境校验未通过，没有建卡（不消耗轮次）。必须先解决：1. [engine.dead] … → 用 team_launch 起协作台」。
  消息没发出去 = 大脑没被激活 = 一张卡、一轮额度都没烧；
- 新增第 7 个工具 `team_preflight`，可以单独调来看全部项；其余五个工具**不加闸门**
  （环境坏了的时候它们正是用来诊断和修复的，拦住等于把人锁在门外）；
- 逃生口 `skip_preflight=true` 默认关闭，绕过时在群消息 `meta.preflight` 写 `"skipped"`——
  误报要能绕过，绕过这件事要能查到；
- `doctor.check_config` 改成逐角色渲染 preflight 的结果（配置文件的"在不在/解析得开"
  仍由 doctor 自己判），现在拼错的型号会显示 `✗` 而不是 `✓`。

### 3. 证据

- 新增 `tests/test_preflight.py`（18 项）：`cli_ready` 全部打桩，跑在哪个机器上结论都一样；
  覆盖坏主模型、坏降级项（只警告，`run()["ok"]` 仍为真）、空链 / 全死链、
  工作区四种坏法、引擎三种坏法、`blocking` 顺序、`include_remote=False` 的纯本地模式；
- `tests/test_mcp_server.py` 新增 5 项：体检不过时 `team_task_create` 抛错**且一个 HTTP 请求都没发**
  （断言的是"没发消息"，不是只断言抛错）；`skip_preflight=true` 能发出去且 `meta.preflight=="skipped"`；
  `team_preflight` 已登记且 `tools/list` 能列出来；
- 全套 664 项通过，`ruff check .` 干净；`skills/` 正本与 `agent_team/bundled/skills/` 镜像已同步，
  `docs/MCP.md` 与 README 的工具清单从 6 个改成 7 个。

## 0.9.23 — 鹈鹕骑车那一晚：验收命令看产出，模型满载会降级

一张"用 HTML 画一只骑自行车的鹈鹕"的卡（T-261102-012458）两轮都没过，两轮的原因还不一样。
顺着它修了三处。

### 1. 验收命令不再一律 unittest（9c4d18a / 029f83b）

空工作区里做一个静态页，规划器却给了 `python -m unittest discover -s tests -t . -v`——
`ImportError: Start directory is not importable: 'tests'`，两轮都死在验收的第一步。

- 规划者 / 验收者 / 大脑的提示词改成按产出挑命令，并把工作区现状（顶层有哪些文件、
  有没有 `tests/`）一起喂进去，空目录里不会再照抄 `discover -s tests`；
- 白名单补 `python -c`、`python -m json.tool`、`node --check`；`parse_verify_argv`
  改成引号感知：`python -c "import sys; …"` 整段作为参数传下去，引号外的 `| ; >`
  照旧拒绝，仍然不经过 shell；
- 白名单外的命令，**兜底也看产出**：scope 点名了 HTML / 文档 / 配置 →
  用 `python -c` 断言这些产物都在；点名了 `.py` 且工作区没有 `tests/` →
  `python -m py_compile`；只有工作区真有 `tests/` 才回到 `unittest discover`。

### 2. 模型满载会降级，不再拿同一张卡撞同一堵墙

执行者第一轮烧掉 9.7 万 tokens 之后空手而归，日志里只有一句
`ERROR: Selected model is at capacity. Please try a different model.`；
第二轮 13 秒后又用同一个模型、同一个打满的额度再撞一次。

- 新增 `agent_team/fallback.py`：只认"容量 / 限流"这一类结论才换模型；普通失败
  （补丁冲突、语法错）立刻收手，不拿别的模型把真问题重跑一遍；
- `run_codex` / `run_claude` 共用这条重试：每个候选模型各自按注册表路由，
  降级链里混着别的 CLI 的型号也跑得对；某条降级链指向本机没装的 CLI 就跳过它，
  不按死整条链；
- `config/team.json` 的 `executor.fallback_models`（复核者本来就有同名配置）支持写别名，
  自动归一、去重、排掉本轮实际用的主模型；换过模型会在 `task.json` 的
  `rounds[].fallback_note`、`room` 事件和命令行里写明"原定 A，容量满载，降级为 B"；
- 顺手修了注册表的一个坑：`fallback_models: ["luna"]` 这种别名会被"自动收录"成一个
  叫 `luna` 的幽灵型号、菜单里多一条、`resolve("luna")` 直接命中它——现在先按别名
  归一，再决定要不要收录。

### 3. 证据

- 新增 `tests/test_fallback.py`（28 项）：容量判定、降级顺序与留痕、跨 CLI 路由、
  配置归一、验收命令兜底；其中一组**真的起一个假 codex 子进程**跑完整条链
  （满载 → 降级 → 拿到产物），另有一组把验收命令**真跑**一遍（产物没生成 → 退出码 1，
  生成后 → 0）；
- 全套 641 项通过，`ruff check .` 干净。

## 0.9.22 — 图标不再是白纸（Pillow 的 ICO 写入器不能用）

0.9.21 的图标装上桌面后显示成一张白纸——就是"无法识别的文件"那个样子。
原因不是画错了像素，是**文件格式**：`Image.save(format="ICO", sizes=..., append_images=...)`
把每一帧都压成了 PNG。PNG 帧只有 256 那一档 Windows 认；16..128 必须是 BMP
（32 位 DIB + 1bpp 掩码）。Explorer 读不出来，就画成空白。

### 证据

直接调 shell 自己取图标的那条 API `SHDefExtractIconW`：

| 文件 | 结果 |
|---|---|
| 旧（七帧全 PNG） | `hr=0x00000001`(S_FALSE)，大小图标句柄都是 0 |
| 新（16..128 BMP + 256 PNG） | `hr=0x00000000`(S_OK)，句柄都在 |

再用 `SHGetFileInfo` + `GetDIBits` 把桌面图标真画出来数颜色：托盘 `#121821`、
节点 `#3FD3A6`、连线 `#2C6F5B` 都在——不是白板。

### 修法

`bin/make_icon.py` 自己拼 ICO（`_bmp_frame` / `_png_frame` / `write_ico`），
128 及以下写成 BMP，256 用 PNG，不再走 Pillow 的 ICO 写入器。

### 验证

桌面启动器测试 35 → 37 项：16..128 必须是 BITMAPINFOHEADER、256 必须是 PNG；
以及真的问 Windows 要 48/16、32/16 两套图标，都得是 S_OK 且拿到句柄。
全套 556 项通过；`ruff check .` 干净。

## 0.9.21 — 自己的图标（不再借系统那个通用方块）

桌面快捷方式原来借用 `shell32.dll,13`，就是那个谁都认识的默认图标。这一版有了自己的：

- `bin/make_icon.py` 画出 `assets/agent-team.ico`（16/24/32/48/64/128/256 七档）
  和 `assets/agent-team.png`（256 预览，给人眼看的）；
- 图案：深色圆角托盘 + 三个节点连成三角形，顶上那个节点里一个白点——大脑加两个干活的；
- `install-shortcut.ps1` 加了 `-Icon`，`install-desktop` 看到图标就自动带上，
  没有就退回系统图标（不让快捷方式变白方块）。

### 小尺寸不是把大图缩小

16 这一档只留三个点：连线在 16 的网格上会把三个点粘成一根横杠，加了还不如不加。
24 不放白点（`8×24/256` 不到一个像素），从 32 起才有。两档都在自己的网格上重算坐标，
不是缩放——缩放在 16 上必然糊。

### 这张图是协作台自己跑出来的

图标是在协作台里交给 Codex 生成的（任务 `T-261101-202739`）：我写视觉规格和验收，
它出脚本和文件；16px 那一档和快捷方式接线是我按小网格重算、重接的。

### 验证

桌面启动器测试 28 → 35 项（图标脚本可重跑、ico 七档齐全、png 是 256×256、16px 无白点、
快捷方式真能用上新图标）；全套 554 项通过；`ruff check .` 干净。

## 0.9.20 — 桌面多一个「像应用一样的窗口」的快捷方式

0.9.15 的桌面图标双击后打开的是**浏览器标签页**。这一版多给一个选择：独立窗口、没有地址栏，
关掉就是关掉，更像一个桌面应用。

### `install-desktop --app`

```bash
agent-team install-desktop --app   # 生成 agent-team-app.lnk（独立窗口版）
agent-team install-desktop         # 还是老样子：agent-team.lnk（默认浏览器）
```

两个图标可以并存，互不覆盖（文件名不同）。

### 为什么不能把 URL 写进快捷方式

一次性引导码每次启动都会变（`?k=`，用掉就换新的），写死等于下次打开只剩解锁页。
所以快捷方式指向 `pythonw -m agent_team.launcher --app`，由启动器在运行时取码，
再交给 Edge/Chrome 的 `--app=` 开窗口。**必须是 pythonw**：换成 python.exe，双击会闪一个黑框。

### 兜底

- 找不到 Edge/Chrome → 退回默认浏览器（不会白开一次）；
- 找不到 pythonw.exe → 直接报错说清楚，不偷偷改用 python.exe 弹黑框；
- `--no-browser` 优先于 `--app`。

### 验证

桌面启动器测试 22 → 28 项（`--app` 走 `--app=` 而不是标签页、无浏览器时回退、`--no-browser` 优先、
`.ps1` 的 `-App`/`-PythonW` 契约、真生成一个指向 pythonw 的 `.lnk`）；
全套 548 项通过；`ruff check .` 干净。

## 0.9.19 — 给别的 Agent 一个「停」的按钮（而且不撒谎）

0.9.16 的五个 MCP 工具能起任务、等结果，但停不掉：网页上有停止按钮，外部 agent 没有。
这一版补上第六个工具，顺手把一个会"撒谎"的边界堵上。

### 新工具 `team_task_stop`

把 `POST /api/control {"action":"stop","task_id":...}` 包成工具，引擎在下一个步骤边界停下、
任务记 `stopped`——和网页点「停止」走同一条链路，`room_web.py` 一行没改。
`team_task_create` 的 `inputSchema` 也补上了 `session_id`（函数本来就支持，只是没暴露）。

### 先验任务，再写指令

`/api/control` 不校验任务号。工具如果照发，停一个打错的 id 就会写进一条**永远没人消费**的指令，
躺在 `room/control.json` 里，然后回一句「已停止」——对调用方来说就是假话。所以 stop 先查 `/api/task`：

- 任务不存在 → 直接报错，并提示用 `team_task_list` 确认；不写指令；
- 已经是 `passed/failed/...` → 回 `already_finished`，如实说「没写指令」。

### 三处小修

- **任务号先转义再拼进 URL**：`/api/task?id=` 是直接插值的，任务号来自调用方模型、不可控。
  带 `&` 会被拆成两个参数，**查到的不是这张卡**（静默查错）；带空格或 `#` 直接发不出去。
  统一走 `_task_path()`（`quote(..., safe="")`），三个调用点一起改；
- `docs/MCP.md` 自检里还写着「26 项」，实际早不是这个数，改成与真实用例一致；
- `team_task_wait` 默认等 30 分钟，但有的 MCP 客户端自己的调用超时更短。文档写明
  `wait` **没有副作用、可以重复调**：被掐断就再等一次，不为迁就客户端改默认值。

### 验证

- MCP 测试 26 → 36 项：真服务落盘 `control.json`、JSON-RPC 调用路径、不存在的任务不写指令、
  已结束的任务不写指令、令牌错要报错、任务号带 `&` 也能查对卡、路径转义；
- 全套 542 项通过；`ruff check .` 干净；skill 正本与包内镜像逐字节一致。

## 0.9.18 — 给"从 GitHub 下载的人"一条明路（不是只给我这台机器）

0.9.17 把 skill 做出来了，但那条命令只有**我这台机器**知道。别人 clone 下来看到的是
570 行 README，不会自己猜到"还要给自己设备上的 agent 装个 skill"。

### README 第一屏就有"刚下载下来，先做这几步"

`doctor` → `start`（用打印出来的 `?k=` 链接）→ `install-skills`，并给了一张表说明每个客户端
装到哪个路径、豆包这类没有 skill 机制的走 `--mcp`。之后想看这几步：`agent-team onboard`。

### 首次启动会主动说一次

`agent-team start` 第一次跑完会打印上面这几步（写 `room/onboarding.json` 当标记，只啰嗦一次）。
双击启动器的人也逃不掉——提示就印在他看得到的那一行。

### skill 正本跟着包走

`skills/agent-team-collab/SKILL.md` 是正本，另有一份镜像在 `agent_team/bundled/skills/` 下
（`pyproject` 只打 `agent_team` 包，仓库根的 `skills/` 进不了 wheel）。取用时走
`resources.resource()`：仓库优先、包内兜底。有测试盯着两份必须**逐字节一致**，
改了正本忘了同步会直接挂。

### 新增命令

```bash
agent-team onboard                  # 新装之后该做哪几步
agent-team install-skills --where    # 只看会装到哪儿，不动文件
```

### 验证

530 项测试全绿、ruff 干净。新增 7 项：包内副本一致、没有仓库时回落 bundled、
上手说明覆盖四个入口、没检测到客户端时如实说、首次提示只出现一次、
`onboard` 命令可跑、README 里必须留着这条引导（防止哪天被手滑删掉）。

## 0.9.17 — 把「怎么用协作台」做成可分发的 skill（一份正本，装到各家 agent）

协作台是**项目**，但用它的 agent 各有各的加载方式：DSH 读 `~/.dsh/skills/<名>/SKILL.md`，
Codex CLI 读 `~/.codex/skills/<名>/SKILL.md`，Claude Code 读 `~/.claude/skills/<名>/SKILL.md`
（三处格式相同：YAML frontmatter + 正文）。

所以正本只放一份在仓库里（`skills/agent-team-collab/SKILL.md`），跟着版本走：

```bash
agent-team install-skills            # 装到 DSH / Codex / Claude Code 三处（已有会先备份）
agent-team install-skills --dry-run  # 只看会做什么
agent-team install-skills --mcp      # 没有 skill 机制的客户端（豆包等）拿 MCP 配置
```

### 安装器不粗暴

- 内容一致 → 跳过（幂等，可以塞进部署脚本）；
- 内容不同 → 先存 `SKILL.md.bak-<时间>` 再写，`--force` 才不备份；
- 目标不认识（比如 `--targets cursor`）→ 直接拒绝，不猜路径。

### 顺带治了"文档漂移"这个病

旧版 skill 里写着 `python bin/room.py post --as user ...`，而 `bin/room.py` **早就不存在**——
照着做的 agent 第一条命令就失败。所以 `tests/test_skills.py` 会把 skill 里提到的
**每条命令、每个模块、每个 `bin/` 脚本**都拿去和真实 CLI/文件比对：

- `agent-team <子命令>` 必须在 `--help` 里存在；
- `python -m agent_team.<模块>` 必须有这个文件；
- `bin/<脚本>` 必须存在；
- 明确禁止再出现 `bin/room.py`、`bin/start_all.py --status` 这类已删除的写法。

另外钉住三个必须写清楚的坑：必须用 `?k=` 一次性链接（裸地址只是解锁页）、
90 秒静默会收工且后台标签页节流会误判、MCP 的调用方身份只在 `meta.client` 而不在任务卡里。

### 顺带修的小东西

`agent-team reset` 之前把 `--yes` 吞掉了（在无终端的会话里会卡在交互提示），现在能透传。

### 验证

18 项新测试（frontmatter、命令漂移、三个目标布局、幂等、备份不丢手改、`--force`、dry-run 不动盘、
未知目标拒绝、MCP 片段是真 JSON 且 cwd 不被转义成四重斜杠）。

## 0.9.16 — MCP 工具：让 Claude Code / Codex / 豆包 直接指挥协作台

5 个工具，标准 stdio JSON-RPC，纯标准库：

| 工具 | 作用 |
| --- | --- |
| `team_status` | 在不在跑（`/api/alive`，免令牌）+ 最近几张卡 |
| `team_launch` | 没在跑就起、在跑就不动；返回一次性链接 |
| `team_task_create` | 把需求发进群，**等到真任务号**再返回 |
| `team_task_wait` | 阻塞到终态，给结论/越界改动/复核是否动过文件/报告 |
| `team_task_list` | 最近的卡 |

接入见 [docs/MCP.md](docs/MCP.md)（Claude Code、Codex、通用 JSON、CLI 四种方式）。

### 设计上做的取舍

- **砍掉「生成协作建议文本」这类工具**：那是调用方模型自己就能写的东西。工具应该返回事实
  （状态、任务号、结论），不该替模型说话；
- **`team_task_create` 必须返回任务号**：`POST /api/post` 只回消息 id，卡是后台 worker 建的。
  所以工具盯住新消息 + 任务列表，等到真 id 才返回；等不到就如实说「已收下、还在规划」，
  而不是给个假 id 让调用方拿着去 `wait` 然后 404；
- **令牌只走 `X-Team-Token`**：绝不拼进 URL（0.9.12 的硬约束）；
- **`team_launch` 不重启**：已经在跑就只给新链接，重启会打断正在执行的任务；
- **身份记在 `meta.client`**：任务卡的 `owner` 是执行者 `codex`，没有"谁提的需求"字段；
  陌生名字还会被 `_agent_from` 折成 `coordinator`，所以 `agent` 固定 `user`、身份放 `meta`。

### 顺手补的

`GET /api/alive` 现在返回 `version`：外部工具能一眼看出对面是哪一版（之前 `team_status`
只能显示"在线"，看不到版本）。

### 验证

- 26 项新测试（协议：initialize/tools/list/tools/call/未知方法/坏 JSON/EOF；语义：等出任务号、
  等不到不编造、身份落 `meta`、终态结果、复核写盘可见、超时不算错误；安全：令牌不进 URL、
  401 不吞、缺令牌给可操作提示）；
- 真 MCP 子进程走完整会话（隔离 room + 隔离端口）：握手 → 5 个工具 → 真调
  `team_status`/`team_task_wait`/`team_task_list` → 未知工具 `isError` → 离线状态 → EOF 退出码 0。

## 0.9.15 — Windows 桌面启动器（双击就开，已经在跑就不动）

双击桌面的 agent-team 图标 → 协作台起来 → 浏览器打开**一次性链接**；
已经在跑就只打开页面，**不重启**（重启会打断正在执行的任务）。

```powershell
agent-team install-desktop        # 桌面生成 agent-team.lnk
python -m agent_team.launcher     # 不想装快捷方式时，命令行等价用法
```

### 为什么逻辑在 Python 里、脚本只有 20 行

cmd 的引号/错误码/代码页太容易写错，而且写在 .bat 里没法测。所以：

- `bin/agent-team-launcher.bat` 只做 `cd /d %~dp0..` + 转交 `python -m agent_team.launcher`；
- `agent_team/launcher.py` 负责判断：`GET /` 已 200 → 只取新码开页面；否则起服务并轮询
  `GET /` 直到真能打开（≤30s，不是睡固定几秒赌）；端口被别的程序占着就直接说清楚、不硬起；
- 两个脚本**纯 ASCII**：cmd.exe 按控制台代码页读 .bat、PowerShell 5.1 把无 BOM 的 UTF-8 当 ANSI 读，
  中文分别会让命令写坏、让 .ps1 报 `The string is missing the terminator`。中文提示全由 Python 输出；
- 快捷方式 `TargetPath` **直接指向 .bat**，不绕 `cmd.exe /c "..."`（少一层引号嵌套）。

### 顺手修掉的外部方案三处硬伤

外部方案给的启动器"能提交"但**双击必失败**：`cli start --workspace`（这个参数不存在，
argparse 报错、服务根本没起）、探活打不存在的 `/api/status`、打开裸地址（0.9.12 起是解锁页
而不是界面）。它那 6 个 pytest 风格"测试"在本项目的 `unittest discover` 里一条都不会被收集。

### 验证

- 23 项新测试：launcher 六种分支（已在跑/没在跑/起不来/超时/端口被占/`--no-browser`）、
  .bat 与 .ps1 的内容契约（纯 ASCII、不对不存在的参数和接口）、真调 PowerShell 解析 .ps1、
  真建 `.lnk` 并核对 `TargetPath` 与"已存在不覆盖";
- 隔离 `AGENT_TEAM_HOME` + 端口 8895 跑完整双击链路：首启 rc=0 并给出 `?k=` 链接，
  链接第一次 302、第二次 401，二次启动识别"已在跑"不重启，服务仍 200。

## 0.9.14 — 角色↔模型可以运行时切换（网页点一下 / 群里说一句 / 命令行）

原来是**静态配置**：模型写死在 `config/team.json`，而且有两处（`agents[]` 和
`executor`/`reviewer` 块），换一次要手动改文件、记住改哪处、还可能忘了重启。
现在多一层运行时覆盖，三个入口共用同一条链：

    dispatch --model  >  任务卡  >  room/model_overrides.json  >  config/team.json  >  默认

- **网页**：右栏每个角色名下面一个模型徽章（`gpt-6.1-terra`），点开菜单选型号，
  当前项打勾、没装 CLI 的置灰并写明原因、"恢复配置默认"单列一行；
- **群里**：`/model 执行 luna`、`切换模型 复核=terra`、`/model list`、`/model 执行 reset`；
- **命令行**：`agent-team model` / `model set 执行 luna` / `model reset 执行`。

### 三条硬规矩（切错了要当场知道，而不是跑完一轮才发现）

1. **只认注册表**：`config/team.json` 新增 `model_registry`（型号 + CLI + 后端 + 别名）。
   拼错 → 400 并列出可用型号；配置里出现过但没注册的型号自动收录，老配置零改动；
2. **没装 CLI 不许切**：注册表里有 `claude-opus-5-5` 但本机没 `claude` 命令 → 拒绝并说明。
   否则"切成功"的下场是下一张卡必然失败、白烧一轮；
3. **协调者不可切**：它跑的是引擎自己的大脑模型，换要改配置 + 重启引擎。
   页面标虚线、接口直接 400 并说明原因，而不是假装切了。

### 刻意不做的两件事

- **不做自然语言猜测**：只认 `/model`、「切换模型」这种显式前缀。一句
  「用 python 换实现方式」被当成切模型，会把正在跑的任务悄悄换掉执行者；
- **不在 `agents.run()` 里透传 `--model`**：解析放在 `team.py` 子进程里做，
  它每次都是新起的进程、读的是最新覆盖文件。父进程透传旧值反而会把刚切的模型又盖回去。

### 验证

- **454 项测试**（新增 33 项：注册表/别名/歧义前缀、优先级四层、拒绝拼错与缺 CLI、
  原子写不留临时文件、坏配置不崩、命令解析的正负例、接口鉴权与 400 语义、页面契约）。
- 真浏览器（headless Edge，隔离 room + 一次性码）：
  `BADGE badges=4 fixed=1 exec=gpt-6.1-terra` / `MENU open=yes items=4 disabled=claude-opus-5-5` /
  `PICK nowExec=gpt-6-luna src=临时`，且 `room/model_overrides.json` 落盘 `{"executor": "gpt-6-luna"}`。
- 线上 8899：`/api/models` 200（5 角色）、拼错型号 400、切协调者 400、页面徽章齐全。

## 0.9.13 — 复核者只读做成硬约束（配置也改不动）+ 事后对账

接 0.9.12 那份外部审核的最后一问：**"为什么审核者能写文件？"**
那位作者当时有写盘权限，顺手提交了一个 CI 修复（`af4ab68`，修得对）。但权限模型错了——
我们的复核者可信任，是因为 `--sandbox read-only` **技术上做不到写**，不是"这轮它没改"。

### 沙箱硬约束

以前：`sandbox = args.sandbox or cfg["sandbox"]`——默认只读，但 `config/team.json` 或
`--sandbox workspace-write` 一改就能写。现在 `team._review_sandbox()` **只认 `read-only`**：

- 请求 `workspace-write` / `danger-full-access` → 强制降级成 `read-only`，
  并在群聊发一条告警（降级必须留痕，静默改等于没有审计）；
- 命令行还留着 `--sandbox`，纯粹为了**记录谁想开写权限**。

### 事后对账（沙箱万一失效也瞒不过）

复核前后各拍一次工作区指纹（`路径 → 大小:mtime_ns`）：

- 变了 → 文件名写进 `task.json.review_wrote` + 群聊报警；
- `reviews[]` 每条现在带 `sandbox` 和 `wrote`，能反向追溯"这一轮是谁复核的、动没动手"。

### 测试

新增 `tests/test_reviewer_readonly.py`（12 项）：降级、留痕、本来就只读时不刷告警、
配置写可写也照样被降级、源码级断言 `cmd_review` 不再直接吃 `args.sandbox`、
指纹能抓"改了内容 / 新增文件"、任务卡记录字段齐全。**共 414 项全绿**。

> 另：`.local-notes/案例-审核误判-照注释不照代码.md` 记录了这次审核的 4 个事实错误
> 与"把权限问题当流程问题"的教训（该目录不入库）。

## 0.9.12 — 按外部审核报告修的三件事（P0-a/b/c）+ 改掉一条误导人的注释

收到一份外部审核（`审核报告-0.9.11.md`，Kiro/Claude 写的）。方向对，但**4 处事实错误、
1 条优先级放反**，逐条核过之后按自己的排序修了三条真正会"丢东西 / 烧钱"的：

### P0-a 执行完立刻核对 scope（原来是等验收才判 FAIL）

越界是执行**期间**发生的，等 verify 才发现，等于白烧一整轮（几百秒 + 十几万 token）。
现在 `cmd_dispatch` 执行一结束就调 `report_scope_now()`：

- 越界文件立刻写进 `task.json.out_of_scope`，并在群聊发一条 `⚠ 第 N 轮有 K 个越界改动`；
- 改回来了自动清掉标记，不会一直挂着吓人；
- 只报告不拦截（拦截仍由验收闸门负责），核对本身失败也不许影响派发。

### P0-b 任务卡 ID 秒内去重（原来会静默覆盖）

`T-YYMMDD-HHMMSS` 只到秒，同一秒建两张卡 → `mkdir(exist_ok=True)` 不拦 → **直接盖掉前一张**，
事后完全看不出来。现在 `new_task_id()` 发现重名就加 `-2`、`-3`…（会话 `chat-001` 本来就是序号，不受影响）。

### P0-c 长期令牌不再进 URL，改用一次性引导码

`cli start` 打印的是 `/?k=<一次性码>`：换了 Cookie 就**立刻作废并自动换新码**，
旧链接再点一次就是 401。`/?token=<长期令牌>` 直接拒绝（只能粘到页面输入框），
因为链接会留在浏览器历史和 Referer 里。Cookie 本身没变（30 天，HttpOnly + SameSite=Strict），
已登录的页面重启后照常可用。

### 改掉一条把审核带偏的注释

`worker.py` 文件头写着「3 秒轮询 / `--interval 3`」，实际默认 **0.5 秒**（网页 `/api/state` 是 1 秒）。
外部审核照这行注释写成了「3 秒轮询延迟」，所以把注释、`cli start` 的文案
（"3 秒内自动开工" → "约 1 秒"）、引擎上线事件都改对了。

### 没采纳的两条（说明理由）

- **「执行前 scope 白名单预检」**：挡不住 Codex 执行期乱写，提前查"声明是否合法"没有防护作用；
  真正有用的是上面 P0-a 的「执行后立刻核对」。
- **「抽象 Agent 后端 ABC」**：`agents.py` 本来就是适配层（`run(role, …)` 分派到任务卡内核），
  换执行器只需再写一个 `run_codex` 等价物，不是"大幅改造"。
  （前端拆分的建议认可，但那是维护性收益，排在 P1。）

### 验证

- 402 项测试（新增 10 项：引导码一次性/轮换、长期令牌拒收、越界即时报告、ID 撞名）。
- 真服务（8899）手工核：`GET /` 200、`GET /?token=<长期令牌>` 401、
  `GET /?k=<码>` 302+Cookie、同码二次 401、码已自动轮换、新码可用 → `RESULT ALL PASS`。

## 0.9.11 — 产物是蓝色的文件名了；工作区不再堆空临时目录

用户原话：**「他这产出一堆不知名的玩意在工作区，完事我又找不到他产物」**。
两件不同的事，一起修。

### 一、工作区里的"不知名玩意" = Codex 留下的空临时目录

为了让 Codex 沙箱能写临时文件，`TEMP/TMP/TMPDIR` 被指到 `<工作区>/.tmp`。Codex 每跑
一条命令就在里面建一个 `.tmpXXXXXX` **空目录**，跑完不回收入——一次任务能堆几十个
（实测 `E:\scratch-chat-test\.tmp` 里有 46 个）。因为是空目录，`git status` 看不见，
资源管理器里却一片，正好符合"一堆不知名的东西"。

- 新增 `codex_bridge.prune_scratch(workspace)`：每轮跑完收掉空目录，`.tmp` 也空了就删掉它。
- **只删空目录，一个文件都不碰**（产物绝不会被误删）；正被占用的目录留到下一轮。
- 没给工作区就直接返回 0——不猜、更不顺手清自己的仓库（这条是被测试逼出来的）。

### 二、产物找不到：聊天和右栏都只有一串死路径

那张"鹈鹕骑车"的卡其实**产出成功了**（`pelican_bike.html` 就在工作区根目录），
但 `task.json` 里没有产物字段，右栏「代码改动」只有一行灰色文本，聊天里什么也没有。

按用户要求做成 **Codex 那样：产出后发一个蓝色的文件名，单击打开，右击出菜单**。

- 任务卡/消息的 artifacts 里，**产物排在最前**：`workspace:<工作区相对路径>`。
  `team.workspace_products()` 从 `git status` 算，账本目录（`tasks/ room/ .tmp/ …`）
  和缓存垃圾（`__pycache__ *.pyc *.log`）不算产物。
- 执行者/验收者那一步的消息自动带上产物引用（`agents._artifact_paths`），
  所以完成消息里直接就是一排蓝色文件名。
- 单击 → 预览弹层（`/api/artifact`，本来就支持 `workspace:` 前缀，这次接上了）；
  右击 → 菜单：**打开（预览）/ 用默认程序打开 / 在资源管理器里显示 / 复制完整路径**
  （后两个是新增接口 `/api/artifact/open`、`/api/artifact/reveal`，只对已放行的路径生效）。
- 右栏「代码改动」从死文本改成同一套蓝色文件名，能点能右击。
- **清单外**（任务卡没声明要改、却被改了）的产物：换成警示色 + 「清单外」小标签，
  右栏顶部还会写一句"任务卡没声明要改这几个文件"——**只提醒，不拦**（用户选的）。

### 验证

- 新探针 `.tmp/probe12/shot_products.py`（隔离 room + 真 git 仓库 + 注入动作）：
  `chips=5 firstName=pelican_bike.html outTagged=yes color=rgb(108,182,255)`；
  单击 → `modalOpen=yes hasCanary=yes`（弹层里真有文件内容）；
  右击 → `items=preview,open,reveal,copy`；右栏 `inspChips=4`、越界提示按预期出现。
- **392 项测试**（新增 20 项 `tests/test_products.py`）、`ruff` 干净、内联 JS 过 `node --check`。

## 0.9.10 — 修 z-index：点「清空回收站」的二级确认被回收站压在下面

用户报的准：**「确认清空回收站的页面优先级没有回收站高，需要先取消回收站页面才能看到确认」**。
0.9.6 把回收站弹层挪到 `z-index:81`，而确认框一直停在 `80`——于是从回收站里点「清空」，
弹出来的确认框正好钻到回收站遮罩**底下**：dom 里有、`hidden` 也是 false，但人眼看不见，
必须先把回收站关掉才露出来。一级弹窗（会话菜单删会话）不受影响，所以之前没暴露。

- 确认框 `z-index:80 → 90`，明确压在所有面板之上（回收站 81 / 会话菜单 78 / 选择器 75）。
- **连带修掉同源的第二处**：两层叠着按 Esc 时，确认框关了、回收站也跟着关。
  现在确认框那一层 `stopImmediatePropagation()`（模态优先），回收站那层再看一眼
  `defaultPrevented`，Esc 只收最上面一层，焦点回到「清空回收站」按钮上。
- 加了 2 项页面契约测试：
  - 确认框的 z-index 必须**大于** `.trash` / `.picker` / `.sess-menu`（以后谁再调层级会立刻红）；
  - Esc 的两道保险必须在（`stopImmediatePropagation` + `defaultPrevented` 守卫）。
- 真浏览器验（8915 隔离 room，注入点击后量几何，不靠看图）：
  `zConfirm=90 zTrash=81`，确认按钮中心点 `elementFromPoint` 命中 **`confirm-ok`**（改之前会命中回收站遮罩），
  卡片可见宽度 >0；按 Esc → `confirmHidden=true trashOpen=true focus=BUTTON`；
  再走一遍完整流程 → toast「已清空 3 个会话」、面板关闭、磁盘 `sessions/trash/` 真空。
- 372 项测试全绿，`ruff` 干净，内联 JS 过 `node --check`。

## 0.9.9 — 回收站能清空了（之前只进不出）

用户一句话点破：**「回收站要能清空呀，不能就摆在那里」**。0.9.6 只做了「还原」，
删掉的会话进了 `sessions/trash/` 就永远躺在那儿，只能手动去磁盘上删文件。

### 存储层（`session.py`）
- 新增 `empty_trash()`：把 `sessions/trash/` 里的 `.jsonl` **和同名 sidecar** 一并删掉，
  返回 `{"removed": 删了几个会话, "failed": [删不掉的文件名], "names": [...]}`。
- 只删回收站目录里的**普通文件**（不递归、不跟符号链接）：主列表、`sessions/` 根下的
  其它文件、磁盘上别的地方一个字节都不动（有测试钉住）。
- 单条删不掉（被占用 / 权限不够）**不抛异常**，记进 `failed` 继续删别的；
  空回收站或目录不存在算正常情况，`removed: 0`。
- 0.9.3～0.9.5 时代删的、没有 sidecar 的老记录也能一起清掉。

### 接口（`room_web.py`）
- `POST /api/session/empty-trash` → `{ok, removed, failed, trash, error}`；
  `error` 只在有文件删不掉时给一句话（「有 N 个文件删不掉（可能被占用）」），不影响其它文件已删除。
- `ChatStore.empty_trash()` 适配层：`session.py` 版本旧（没有这个函数）时明确报错，不假装删了。

### 界面
- 回收站卡片底部加一行：左边说明「「还原」能捡回来；「清空」是彻底删除，找不回来」，
  右边是 **`清空回收站（N）`** 按钮，**空的时候自动禁用**（不让人点一个啥也不会发生的按钮）。
- 确认框从「只会删会话」改成**通用**的（`askConfirm(opts)` + `runConfirm()`）：
  标题 / 目标 / 警示 / 确认按钮文字 / 要跑的动作都由调用方给，焦点仍然默认落在「取消」上。
  清空的确认文案是「这里的 N 个会话会被彻底删除」＋按钮「彻底删除」——
  跟「删除会话」区分开，免得以为是同一个动作。
- 清完自动关面板、左栏计数归零。

### 验证
- 单测 **360 → 370 项**：存储层 6 项（含 sidecar 一并删、空回收站连清两次、不牵连没删过的会话、
  老记录、回收站外的文件不许动、删不掉时照删其它）＋ 接口 3 项（整条链、重复清空、无令牌不可删）
  ＋ 页面契约 1 项（按钮/接口/文案/通用确认框都在）。
- 真浏览器端到端（8915 隔离 room，Edge headless 注入点击）：开面板 → 点「清空回收站（3）」
  → 确认框文案正确 → 点「彻底删除」→ toast「已清空 3 个会话」、面板自动关闭、
  **磁盘上 `sessions/trash/` 真的空了**。
- `ruff check .` 干净，内联 JS 过 `node --check`。

## 0.9.8 — 去掉全屏扫描带（用户看过之后：太抢眼）

0.9.7 里那条 140px 宽、斜着扫过整个窗口的光带（react-bits `Scanner` 的观感）被用户否了：
「全屏扫描大波纹不要」。整屏有一道东西一直在动，确实不像工作台，像屏保。

- 删掉 `drawScan()`（含 max 档的边缘色散）、扫描周期常量、`clock.phase` 推进和 `status().scanPhase`。
- 保留：点阵纹理、粒子连线、指针视差、加强档底部透视网格，以及**顶栏那条 2px 扫描线**（贴着窗口边缘，
  不跨内容，所以留着）。
- 副作用：峰值 alpha 从 0.102 降到 0.09 附近，对比度只会更好；单帧少一次全屏渐变填充。
- 16 条端到端判定重跑仍全过；360 项测试、ruff、内联 JS `node --check` 全绿。

## 0.9.7 — 高科技 HUD：青／品红双霓虹、扫描线、状态心跳，以及一个能关掉的开关

按你指的 Codex 那套 skill 来的：本机 `react-bits` 整包（`C:\Users\lenovo\.codex\skills\react-bits`）
里 64 个背景 / 36 个动画 / 33 个文字动画 / 43 个组件，我把需要的挑出来，**读机制用 canvas 2D + CSS 重写成
零依赖的原生版本**（原版是 React + three.js/OGL，我们的页面是单文件无框架，直接抄跑不起来）。

强度按你选的「标准 HUD：看得见，字还是主体」；配色按你选的「青 + 品红双霓虹」：
`--neon-a:#22e0ff`、`--neon-b:#ff3ea5`，只新增这两个 token，其余全走既有调色板。

### 看得见的那部分，全部放在没有文字的地方
这是这轮最关键的一条决定：**任何会提亮底色的东西都不许压在正文下面**。
`--dim2` 这档最暗的辅助文字在实心面板上只有 5.63:1，底色一亮就破 4.5。所以：

- **视口顶部一条 2px 扫描线**（青→品红渐变，7 秒一次光斑扫过），窗外没有文字；
- **四角 HUD 括角**（20px 折线，左上/右下青、右上/左下品红）；
- **输入框外圈**：常态一条淡青底光，聚焦时变 2px 青→品红，铺在框下面；
- **左栏右边缘 / 检查器左边缘**两道 1px 霓虹描边（面板缝里，上方无文字）；
- **正文区只留极淡的点阵纹理**（2px 点 / 32px 栅格 / alpha 9%）。它确实是"细看才有"的，
  这是对比度预算逼出来的取舍，不是偷懒——见下面的实测数字。

### 会动的那部分，都对应一个真实状态
react-bits 那套组件多半是纯装饰，这里按「动效必须解释状态」的原则接：

| 特效 | 来源组件 | 什么时候动 |
| --- | --- | --- |
| 信号场（点阵 + 斜向扫描带 + 粒子连线 + 指针视差 ≤8px） | `Backgrounds/Particles` `DotGrid` `Scanner` | 引擎活着才亮；引擎忙时扫描 12s→4s、点阵提亮、顶部加品红辉光；**引擎离线时整场降饱和、扫描冻结**——一眼看出"仪器没通电" |
| 点击迸溅（8 个 2px 粒子，青→品红，300ms） | `Animations/ClickSpark` | 只在发送/绑定/新建会话/危险操作上炸 |
| 状态心跳环（1.8s 扩散） | 自写（`Micro/StatusMark` 是 SVG 勾叉，不对路） | 引擎在跑、有 Agent 在干活时才出现 |
| 旋转 conic 描边（4s 一圈） | `Components/BorderGlow` `Animations/StarBorder` | 选中会话、输入框聚焦、toast、三个弹窗卡片 |
| 文字解码入场（450ms 逐位定格） | `TextAnimations/DecryptedText` `ScrambledText` | 只在空会话的欢迎标题上，一次 |
| 四角括角 + 扫描线质感 | `CRTWarp` `FaultyTerminal` `Micro/RefineFrame` | 常驻 |

### 开关与降级
- 顶栏多一个「特效·关 / 标准 / 加强」，点一下换一档，落 `localStorage`（`team-fx-level`）；
  URL 上带 `#fx=off`、`#fx=std`、`#fx=max` 直接锁定档位（截图、排障不用手点）。
- `prefers-reduced-motion: reduce`：rAF 根本不启动（只画一帧静态），扫描线不扫、心跳环不动、点击退化成纯 opacity。
- 关掉档位：`cancelAnimationFrame` + `clearRect` + `display:none`，body 上不留 `fx-on`，一个动画都不跑。
- 特效层全 `pointer-events:none`，z 60/62/70（弹窗在 75+，所以对话框永远是干净的）。

### 顺手修的：图标不再用 emoji
`🗑` `📄` `📁` `📦` 换成一套自绘 1.5px 描边 SVG（sprite 里四个 symbol），跟 `currentColor` 走。
加了测试钉住：这四个 emoji 再出现就红。

### 性能与可访问性（都是量出来的，不是宣称）
- 单个 rAF 循环画两个 canvas，**30fps 上限**（实测 27–29 帧/秒，从未超）、dpr 上限 2、
  `document.hidden` 就停；点阵/扫描线预渲染成 `CanvasPattern`（每帧 2 次填充，不是 1400 次 `fillRect`）；
  粒子数 std 28 / max 64，连线按 alpha 分 4 档批量 stroke；单帧 JS 侧 0.02–0.05ms。
- 对比度（把信号场最亮的像素叠到面板底色上算最坏情况）：正文 `--fg` 11.69:1、
  `--dim2` 真实口径 p99 **5.01:1**、保守口径中位 5.44:1、全场最坏 4.58:1，峰值 alpha 0.102。
- 360 项测试全绿（356 + 4 项页面契约），`ruff check .` 干净，内联 JS 两段过 `node --check`。

### 端到端验收（隔离 room、8915 端口、Edge headless，不碰你的 8899）
自检 16 条全过：画布真的画了东西（1326×803、平均亮度>0、`z=60`）、HUD 层挂上（4 个括角、扫描线 2px、动画名 `hud-sweep`）、
四档切换、off 档清空、localStorage 落盘、无 JS 异常、顶栏没被开关挤坏、
关掉/标准/加强三张截图里顶部扫描线的青色像素 0 → 1286 → 1309（品红 0 → 1095 → 1200）。
另外验了窄窗 1000 / 900 / 640 宽下 HUD 与开关都在，reduced-motion 下 `running=false / frames=0`、扫描线 `animation:none`。

### 我没法验的
1. **观感**：这个模型没有图像输入能力（`read_image` 直接报错），我只能量像素和计算样式。
   截图在 `.tmp/fx/shot-std.png`、`shot-max.png`、`shot-off.png`，以及窄窗 `shot-n*.png`，请你自己过一眼。
2. **真实帧率**：headless 是软件光栅，只能证明"没超 30 的闸门"，GPU 下的观感流畅度要你看。
3. **headless 里 rAF 不跑**：所以截图和动效采样是靠 `FX.__paintOnce()` 同步画一帧量的，
   "运动本身好不好看"这条我确实证明不了。

## 0.9.6 — 回收站能还原、会话能改名、协作台自己的目录不再让人白点一次

上一轮留下的四条「未决策」全定了：**做回收站还原**、**恢复气泡尾巴**、**目录对话框直接禁掉自指目录**、
**窄窗保持现状**（620px 以下仍只留聊天，不做抽屉）。范围就是这些，没夹带别的。

### 回收站：删错了能捡回来（`session.py` + 左栏面板）
- `session.list_trash()` / `session.restore(trash_name)` / `session.rename()` 三个新接口；
- `delete()` 挪文件时**顺手写一份 sidecar**（`sessions/trash/<id>-<stamp>.json`），
  记下删除前的标题/工作区/创建时间/归档状态/`title_locked`，还原时原样捡回来；
  0.9.3～0.9.5 时代删的（没有 sidecar）也能还原，从首条消息推标题、用文件 mtime 当删除时间；
- `restore()` 只认回收站目录里的文件名：`../index.json`、`C:\Windows\win.ini`、错后缀一律拒，不会变任意文件搬运；
  id 撞车时自动换 `chat-NNN` 并改名，消息一条不丢；归档会话还原后**不抢**活动会话；
- 界面：左栏底部「🗑 回收站 · N」，点开是一张列表（标题 / 条数 / 删除时间 / 「还原」按钮），
  Esc 或点遮罩关闭，还原后自动打开那个会话；删除确认框的文案也跟着改了（不再说"去 sessions/trash/ 里找"）。

### 会话重命名
- 双击左栏任意会话行，或在「⋯」菜单里选「重命名」，行内出输入框：回车保存、Esc 取消、点别处也算保存；
- 改完写 `title_locked: True`，**之后再发消息不会被首条消息的标题覆盖**（连改回「新会话」也锁得住）；
- 空标题 400、不存在的会话 404、超过 60 字截断。

### 目录对话框：协作台自己的目录直接灰掉
- `GET /api/browse` 每条目录多两个字段 `blocked` / `block_reason`，`selected` 里也带；
- 前端把「选它」换成禁用的「不能选」，停在该目录上时「确认使用这个工作区」直接禁掉并写明原因
  （`E:\多Agent协作` 及其子目录都算，跟后端 `_ledger_inside` 同一条规则）；
- 后端 `POST /api/workspace` 的兜底校验**一行没动**，只是不再让用户白点一次才被拒。

### 气泡尾巴回来了
- 贴头像那一角收成 6px，其余三角仍是 14px：助手气泡左上、用户气泡右上。
  （0.9.2 收敛成了一律 14px，用户看过之后要「有尾巴」这一版。）

### 测试
- `tests/test_session_ops.py` 13 → 40 项：删除/还原/改名存储层，含「id 冲突不丢消息」「路径穿越被拒」
  「没有 sidecar 的老 trash 也能还原」「还原后标题锁定还在」「归档会话还原后 active 不变」；
- `tests/test_web_control.py` 新增 22 项：回收站列表与还原的接口链、改名的接口链（含空标题/不存在/鉴权）、
  目录 `blocked` 标记、以及**页面契约**（回收站钩子、改名钩子、气泡尾巴 CSS 一旦被删就红）；
- 全量 **356 项全绿**，`ruff check .` 干净，内联 JS 过 `node --check`。

### 端到端活体验收（隔离 room，端口 8911/8912/8913，临时目录，不碰用户的 8899）
- HTTP 全链路 33 项断言全过：删除 → 回收站有 1 条（标题/条数/删除时间对）→ 还原（id 沿用、消息完整、切为活动）→
  改名 → 再发消息标题不被冲掉 → 空标题 400 → 归档会话还原后 active 不变 → 4 种路径穿越全拒 →
  `E:\` 列表里 `多Agent协作` 标成 blocked 且后端绑定仍拒；
- 真浏览器（Edge headless，注入一小段调试脚本，生产代码没动）：
  **回车改名真的写进去了**（列表标题变成「改名成功啦」）、**回收站点「还原」真的把会话拿回来了**
  （徽标归 0、面板关闭、toast「已还原：待删除的实验」、列表多出一行）、
  **点灰掉的那行不选中**（体检行显示「这是协作台自己的目录，不能当选工作区」、确认按钮保持禁用）；
- 气泡尾巴量了实际计算样式：助手 `6px/14px/14px/14px`、用户 `14px/6px/14px/14px`。

### 我没法做的那部分
观感还是得你亲眼看（这个模型没有图像输入能力，`read_image` 直接报错）。截图留在
`.tmp/probe10/shot-chat.png`，重点看：气泡尾巴的方向、左栏底部回收站那一行的位置、回收站面板的排版。

## 0.9.5 — 关网页自动收工：判定逻辑变成可测函数

用户问「关闭网页自动退出还在吧」。功能一直在（`_guardian`），但只有接口层的 bye 测试，
**没有一条测试覆盖"没人看着就收工"本身**——这种兜底最容易在重构里悄悄失效。

### 改了什么（行为不变，只拆函数 + 补测试）
- `_guardian_trigger(idle_limit, grace, now=None)`：纯判定，返回收工原因或空串
  （从没人开过页面 / 页面活着 / bye 过了 grace / 心跳断太久）；
- `_guardian_tick(idle_limit, grace)`：判定 + "有任务在跑就先不打断"的处置，只提示一次；
- `_guardian()` 变成 1.5 秒轮询 + 调上面两个，可读性提升，逻辑一行没改。

### 新增测试（10 项，`tests/test_presence.py`）
- 从没开过页面不退出（命令行起的服务不该自己死）；
- 页面活着不退出；bye 过了 grace 才走；grace 内刷新不算关闭；刷新会清掉 bye 标记；
- 没有 bye 时靠 `idle_limit` 兜底（原因是"长时间没有心跳"）；
- 有任务在跑：不收工、只提示一次「干完再收工」、任务到终态后才走。

### 端到端活体验证（隔离 room，端口 8921/8922/8923）
- 发 bye → **3.2 秒**后退出，`session_state.collab=ended`、原因「网页已关闭」，聊天里留了「协作已结束」；
- 不发 bye（模拟浏览器崩溃）→ **7.8 秒**后按 idle 6s 兜底退出，原因「网页已关闭（长时间没有心跳）」；
- 有任务在跑 → 8 秒后仍活着、提示「还有 1 个任务在跑，干完再收工」、任务置终态后 **1.4 秒**退出。

### 顺带确认
真实服务在 23:52:34 就是被这条逻辑收掉的（`room/session_state.json`：`网页已关闭`）——用户关掉页面后它自己退出了，
这正是这次要回答的问题。

## 0.9.4 — 修 CI 在 Windows/py3.12 上的偶发 PermissionError

推送 0.9.3 后 CI 红了：windows-latest · py3.12 的 	est_heartbeat_keeps_beating_while_main_loop_is_blocked
报 PermissionError: [Errno 13] worker.lock。另外三个 job 都过，是 Windows 特有的竞态，不是测试写错。

### 根因
Windows 上 os.replace 替换目标文件的瞬间，目标会短暂处于 **delete-pending** 状态；此时 open()
拿到的是 sharing violation（PermissionError）。心跳线程每 10 秒重写一次 worker.lock，网页每 1.5 秒
读一次，撞上就出错。

这不是测试挑刺——**生产路径上它会让界面把在跑的引擎显示成没在跑**，控制按钮全灰一次轮询。

### 修了两处
1. procs.read_lock()：对 OSError 重试 6 次（累计 ~150ms），文件不存在或 JSON 坏行仍然立即返回空；
2. worker.beat()：临时文件名带上 pid + 线程 id。心跳线程和主循环都会 eat()，共用一个
   worker.lock.tmp 会互相截断，偶尔写出半截 JSON（读者看到空 → 又是一次误判）。

### 测试
- 新增 5 项：ReadLockRetryTests（不存在直接返回、瞬时 PermissionError 重试成功、持续失败 ~150ms 内返回空、
  坏 JSON 不重试）、ConcurrentBeatTests（4 写 1 读并发 60 轮，锁文件始终可解析、pid 正确）；
- 心跳用例改走公开的 procs.read_lock，顺带覆盖重试路径；
- **295 项全绿**（3.14 本机 + 3.11 py），心跳/并发 5 个用例连跑 30 轮 0 失败；
uff 干净。

## 0.9.3 — 会话能归档、能删了

用户：「删除会话，归档等功能呢」。之前左栏只能切换会话，删不掉也收不起来，会话会一直堆着。

### 存储层（`agent_team/session.py`）
- `archive(session_id, archived=True)`：置/清 `archived_at`，消息保留；归档当前会话时活动会话切到最近一个未归档的；
- `delete(session_id)`：消息文件挪进 `room/sessions/trash/<id>-<时间>.jsonl`（**不真删**），索引移除、`active` 修正；
  迁移出来的旧账本会话不允许删（只能在归档里收起）；
- `sessions()` 每条都带 `archived_at`；`active_session()` 跳过归档；`set_active()` 拒绝打开已归档会话（`会话已归档，先恢复再打开`）。

### 接口与界面
- `POST /api/session/archive {id, archived}` → `{ok, session, active}`；
- `POST /api/session/delete {id, confirm:true}` → `{ok, deleted, active}`，缺 `confirm` 或为 false 一律 400；
- 错误按 `会话不存在` → 404、其余（已归档/旧账本） → 400，原因原样透出；自动继承 Cookie 鉴权 + CSRF + JSON 校验；
- 左栏会话行 hover/聚焦出「⋯」菜单（归档 / 删除），键盘 `Enter` 开、`↑/↓` 选、`Esc` 关并还焦点；
- 已归档分组可折叠、归档后自动展开；归档行点开只提示「先恢复」，行尾「恢复」按钮；
- 删除确认框显示标题与消息条数、写明文件留在 `sessions/trash/`，初始焦点在「取消」，`Esc` 取消，Tab 在两键间循环；
- 全删空时显示「还没有会话 · 在下面说一句话就会开一个新的」，发第一条消息才建 chat-001。

### 顺带修掉
- 早先 `active` 由前端重算，在同一秒创建的两个会话上会误判，导致删掉**非当前**会话时界面被切走；改为以索引里的 `active` 为准；
- `/api/session/select` 的 `ValueError` 从 500 改成 400（已归档会话点开会得到人话提示）。

### 验证
- 新增 31 项测试（`tests/test_session_ops.py` 13 + `tests/test_web_control.py` 18），全项目 **290 项全绿**、`ruff` 干净；
- Lead 独立脚本 18 项断言全过（归档/恢复/列出透出/切已归档被拒/删除需 confirm/文件进 trash/重复删 404/已归档也能删/未鉴权 401/跨站 Origin 403/GET 打写接口 404/URL 令牌 401）；
- headless 8 个场景 DOM 证据（菜单键盘链、归档分组、恢复、删除确认框、删非当前不切、删当前切走、全空空状态），页面无 JS 报错。

## 0.9.2 — 界面底座收敛

按 Impeccable 的 polish 规程做的一轮精修：**只重写 `<style>`**，markup / id / class / 文案 / JS 一格没动
（去掉 `<style>` 后与上一版逐字一致，114698 字符）。问题不在观感，在设计底座不统一。

### 改前 → 改后（同一个脚本量两边）
| 指标 | 改前 | 改后 |
| --- | --- | --- |
| 最低文字对比度 | 2.53:1（`--dim2` 是次级文字色） | 4.86:1 |
| 字号档位 | 13 档（10–19px，含 10.5/10.8/11.5/11.8/12.5/13.5） | 5 档（11/12/13/15/19），最小 11px |
| 写死 px 的 font-size | 80 处 | 0 处，全走 `var(--fs-*)` |
| 间距取值 | 24 种，17 种不在栅格 | 6 种（4/8/12/16/24/32），越界 0 |
| 圆角形态 | 13 种 + 2 个单角特殊值 | 4 种（6/10/14/pill） |
| transition | 4 处、4 个时长、无缓动 | 统一 140ms + 一条缓动曲线 |
| prefers-reduced-motion | 没有 | 有（blink/pulse 动画实测 `none`） |
| ::selection / caret-color / ::placeholder | 没有 | 都有，placeholder 4.86:1 |
| disabled | 7 处靠 `opacity:.34~.55` 糊掉 | 0 处，显式底色 + 文字 5.29:1 |
| 断点 | 1 个（620） | 2 个（1180 / 620）+ reduced-motion |

### 结构安全
- 选择器 244 → 269，旧选择器一个没丢（仅 3 条有意演进：`:hover` 补 `:not(:disabled)`、噪音行改按文字分档）；
- DOM 的 id / class 集合改前改后完全相同，JS 依赖的元素都在；
- headless 五个视口（1440/1200/1024/620/400）实测：`body.ready`、`app` 仍 grid、横向零溢出。

### 验证
- 259 项测试全绿，`ruff` 干净；Lead 独立复量脚本 30 项断言全过（对比度 12 组、档位、栅格、圆角、
  四态、表面、断点、DOM 关键节点）；
- 新 CSS 下工作区对话框仍能开合、Esc 关闭、体检与确认按钮正常。

### 已知取舍
- 620px 下侧栏与检查器 `display:none`（DOM 不变），换来窄窗可读；要三栏都在得做抽屉，属交互改动；
- 1180px 下右栏的次要统计数字隐藏；
- 消息气泡原来是「单角尾巴」圆角，按"单角特殊值统一掉"改成四角 14px —— 这是看得见的形状变化。

## 0.9.1 — 选工作区有目录索引了

用户原话：「设置工作区这种功能，应该出现索引，引导用户去确认工作区」。之前是盲输路径 + 提交，
你不知道那个路径长什么样、是不是空的、是不是你要的那个仓库。

### 新增
- `GET /api/browse?path=`：只列**目录**（不吐文件名），`entries` ≤200、正常目录按名排序、
  噪音目录（`.git`/`node_modules`/`__pycache__` 等）排最后并标 `noise`；每条带 `is_git`/`hint`；
  另给当前路径的体检 `selected{exists,is_git,git_branch,file_count,writable,has_tasks}`；
  不存在 404、给文件 400、没权限 403（不会 500）。
- 网页：工作区对话框 = 面包屑 + 上一级/Backspace + 盘符切换 + 目录行（点行进 / 「选它」只选中）
  + 底部「选中路径 · `✔ git 仓库 main · 3 个文件 · 可写`」+「确认使用这个工作区」；体检不过按钮置灰并写原因；
  `Esc`/`↑↓`/`Enter` 可用，全部走现有 CSS 变量，另加 620px 窄屏适配。
- 引导：未绑工作区时左侧、欢迎区、右栏检查器三处都有可点的「选择工作区」；对话框默认停在
  当前工作区 → 上次用过的路径 → 主目录，不再从盘符根开始。

### 验证
- 真实服务上 Lead 独立复核：`/api/browse?path=E:\scratch-chat-test` → `is_git=true, branch=master, files=3, writable=true, has_tasks=true`；
  `404/400/401/401`（不存在/给文件/没凭证/URL 令牌）四种错误码都对；页面里 `#picker`、「选择工作区」、「确认使用这个工作区」、
  「上一级」、「浏览…」都在，HTML 里令牌出现 0 次。
- 259 项测试全绿，`ruff` 干净。

### 已知边界
- 对话框是模态的（背景在弹窗期间不可点不可滚），和现有产物弹层一致；
- 选「协作台自己的目录」要等 `POST /api/workspace` 才被拒（对话框里不会提前禁用），错误显示在对话框底部；
- 窄屏只做了媒体查询，没做肉眼截图比对；macOS/Linux 的根/`/` 分支只按代码路径写过，没实机跑。

## 0.9.0 — 安全迁移：令牌进 Cookie，URL 不再带令牌

四件事同一轮做完（用户要求：任何一项单独上线都不算完成）。

### 1. Cookie 是主凭证
- `Set-Cookie: team_token=<32hex>; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000`；
- 下发只有一处（`_set_cookie`），302 引导和 `/api/unlock` 都走它，不会两处各写一套；
- 鉴权顺序：Cookie → `X-Team-Token` 请求头（CLI/测试/doctor 用）。

### 2. URL 令牌只活一次
- `GET /?token=<对>` → `302` + `Location` 去掉 token（其余查询参数保留）+ Cookie；
- 令牌错 → 401 解锁页，**不下发** Cookie；
- **`/api/*` 带 `?token=` 一律 401**，挡住浏览器历史/书签里的旧链接；
- 页面 HTML 不再注入令牌；前端删掉 `localStorage`、`withToken()`、`?token=` 拼接，全部改 `credentials: 'same-origin'`。

### 3. CSRF / Origin 校验
- 写操作（`/api/*` 的 POST/PUT/PATCH/DELETE）按顺序校验：
  `Sec-Fetch-Site` 只能是 `same-origin`/`none` → 否则看 `Origin`（必须 http(s)、回环地址、netloc 等于本次 `Host`）→ 都没有则放行（非浏览器客户端）；
- 再加 `Content-Type: application/json` 要求（否则 415），挡住简单表单 CSRF；
- 修了个真 bug：拒绝写请求时没读掉请求体会让 Windows 上直接 RST，客户端连 401/403 都收不到（新增 `_drain_body`）。

### 4. 解锁接口
- `POST /api/unlock`：JSON + CSRF 校验，令牌对则种 Cookie；按来源 IP 限流，1 分钟错 5 次 → 429（限流期间令牌对也 429，防枚举）。

### 验证
- 新增 31 项测试（`tests/test_web_control.py` 47 → 78），全项目 **249 项全绿**、`ruff` 干净；
- Lead 独立验收脚本 27 项全过（302/Location/Set-Cookie 四属性、错令牌无 Cookie、Cookie 鉴权、URL 令牌被拒、Origin 403、`Sec-Fetch-Site` 403、表单 415、unlock 限流 429、`/api/alive` 免鉴权）；
- 真实服务手工验证：`curl -i "/?token=..."` → `302 Location: /` + 完整 Cookie；`curl -b jar /api/state` → 200（chat-001）；headless 打开引导链接后 `body class="ready"`、HTML 里令牌出现 **0** 次。

### 已知边界
- 只支持 `http://127.0.0.1`；Cookie 无 `Secure`（http 下会被丢弃），没有 HTTPS；
- `SameSite=Strict` 意味着从外站（聊天软件/邮件）点进来的**第一次**导航不带 Cookie——但引导那一刻靠 `?token=` 种 Cookie，落地页的同源 `fetch` 会带上，实测正常；
- 反向代理会改 `Host`/`Origin`，上反代前必须重审 `_csrf_problem`；
- 升级前开着的旧标签页会 401，刷新一次即可（旧 JS 还在拼 `?token=`）；
- **在补 HTTPS 之前不要改绑 `0.0.0.0`。**

## 0.8.1 — 聊天记录不再丢一段

轮询用 `after=<倒数第 20 条>` 只取新消息，两次轮询之间消息如果超过一页，中间那段会永久缺失。
用户把这条排在了性能之前：聊天记录不完整比慢更伤。

### 新增
- **断层自愈**：客户端发现「服务端这页第一条 id 比本地最后一条大 1 以上」，就循环
  `GET /api/messages?before=<缺口上界+1>&limit=120` 把缺口补回来并插回正确位置，最多 5 页。
  补不齐时在流顶部显示「历史消息有断层（缺 N 条）」+「重试补齐」（放宽到 20 页），不静默失败。
- 翻页和自愈共用同一套合并逻辑（`mergeMessages`），不写两份。
- id 不是 `msg-数字` 格式时跳过自愈，不报错。

### 验证
- headless 探针独立复现（Lead 自己跑的，不只依赖开发者自述）：
  - 小缺口：140 条、1..140 连续，`msg-101..104` 正文都在，无提示；
  - 超 5 页上限：800 条、剩 `msg-101..200` 共 100 条，出现「缺 100 条」+ 重试按钮，页面正常渲染。
- Python 侧 6 项接口契约测试（突发 200 条一页补回、正常重叠不误报、超上限报剩余、重试补齐、翻页契约、脏 id 跳过）。
- 218 项测试全绿，`ruff check .` 干净。

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