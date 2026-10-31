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