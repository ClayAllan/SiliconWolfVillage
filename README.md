# 🐺 硅基狼村 · Silicon Wolf Village

<p align="center">
  <a href="https://siliconwolfvillage.streamlit.app/"><img src="https://img.shields.io/badge/Streamlit%20Cloud-Live%20Demo-FF4B4B?style=for-the-badge&logo=streamlit&logoColor=white" alt="Streamlit Cloud"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.11%20%7C%203.12-3776AB?style=for-the-badge&logo=python&logoColor=white" alt="Python Version"></a>
  <a href="https://platform.openai.com/"><img src="https://img.shields.io/badge/LLM-DeepSeek%20%7C%20OpenAI-8A2BE2?style=for-the-badge&logo=openai&logoColor=white" alt="LLM Engine"></a>
  <img src="https://img.shields.io/badge/Tests-159%20Passed-2ea44f?style=for-the-badge&logo=githubactions&logoColor=white" alt="Tests">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Players-12%20Full%20Roster-orange?style=flat-square" alt="Players">
  <img src="https://img.shields.io/badge/Factions-Wolf%20%7C%20Good%20%7C%20Third%20Party-critical?style=flat-square" alt="Factions">
  <img src="https://img.shields.io/badge/Reasoning-Full%20Chain--of--Thought-blue?style=flat-square" alt="Reasoning">
  <img src="https://img.shields.io/badge/Replay-Deterministic%20Audit-informational?style=flat-square" alt="Replay">
  <img src="https://img.shields.io/badge/UI-Adaptive%20Light%20%2F%20Dark-9cf?style=flat-square" alt="UI Theme">
</p>

> 本地启动说明（2026-10-25）：本目录为新版，直接运行 `python -m streamlit run app.py` 默认使用 http://127.0.0.1:8503/。 两个批处理均采用ASCII正文和Windows换行，并检查8503占用；不会停止其他项目。DeepEcho继续使用8501。批处理附加 `--check` 只检查路径和端口，不启动网页。

**十二个 AI 同桌玩狼人杀，你在上帝视角看它们推理、伪装、投票和翻盘。**

规则由 Python 引擎裁决，模型负责发言、推理和行动选择。访客填写自己的 API Key，好人和狼人可分别选择模型。

## 运行与部署

需要 Python 后端，不能直接通过 GitHub Pages 运行。建议 Python 3.11 或 3.12：

```sh
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

在侧边栏填写 Key、Base URL 和双方模型，先测试连接，再开始对局。第三方 OpenAI 兼容服务需要填写实际可用的模型名。

可部署在自己的云服务器，或通过 [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app) 选择仓库、分支及入口 `app.py`。在云服务器上向外提供服务时，由反向代理接入 Streamlit，并支持 WebSocket；目前每局只在创建它的 Python 进程中执行。

Key 会发送到托管服务器，用于向所填模型服务发起请求；密码输入框不代表 Key 只留在浏览器。Key 不写入存档，也不会把服务器环境变量中的 Key 自动填给访客。

**自动推演在后台独立执行：关闭网页后，对局仍会继续并消耗 API。需要停止推进时，请先点击暂停。** 暂停不会中断已发出的网络请求；其结果会在继续或单步时收集。进程重启或托管平台休眠会中断执行，当前没有自动恢复、账号、可重连房间或共享观战权限。

存档与复盘按浏览器会话隔离，实际目录为系统临时目录下的 `silicon_wolf_village/<会话 ID>/saves` 和 `replays`，并非仓库根目录。重新连接、清理临时目录、服务重启后可能无法找回，请及时下载需要保留的数据。公开界面不提供全站日志查看、下载和清空入口。

## 当前能力

- 七种 12 人板子，覆盖标准预女猎守、白狼王、守墓人、狼美骑士、丘比特、摄梦人、恶灵骑士与魔术师配置。
- 夜间行动、警长竞选与 PK、退水、盲投、警长 1.5 票、警徽移交、自爆与双爆吞警徽。
- 角色插件统一定义身份、阵营、神职标识、技能、提示与状态。发牌前检查板子，未知角色不再自动变成平民。
- 三层记忆：不可压缩真理层、滚动摘要层、近期工作台；结构化事实账本优先于主观摘要。
- 圆桌、公开台面、上帝读心、狼队作战板、票型图、实时 Token 账本、上帝传音及复盘导出。
- 暂停、继续、单步、跳过当前玩家、进度存档、读档与终局自动归档。
- 角色行动使用结构字段和合法目标校验；公开发言必须提供正文。技术失票、主动弃票、人工跳过分别标注，公共证词独立保留并记录来源。
- 可核验的放逐投票统计：好人投狼比例、好人票型集中度、好人弃票率、狼人投好人比例，均附分子、分母和定义。

神职名单从角色注册表派生，摄梦人或魔术师仍存活时会计入屠边判定。跨阵营情侣存活时优先按原有第三方规则判胜负。

统计按投票人数计数，不按警长权重加权；0 是有效观测，无分母显示 N/A。统计不会把好人投狼比例直接解释为识谎能力，也不输出缺乏测量依据的欺骗度、事实保真度或 Token 效能评分。新归档中 `analysis.metrics_schema_version=2`，`observed_metrics` 记录计数；旧字段 `tactical_radar` 保留为 `null`，旧归档不会被批量重写。

归档缺陷的修复、离线验证和重启说明见 [归档问题修复说明](docs/reports/归档问题修复说明.md)。升级动作协议后，请先暂停旧对局并停止旧服务，再启动新版，避免混用热更新前的对象。

## 后台与界面的分工

观战工作台采用圆桌与动态并列的布局，窄窗口自动改为纵向排列；顶部集中对局状态及控制，下方玩家观察室提供档案、双方对照与全席概览。模型参数折叠在侧栏。点击“查看示例 · 零 API”可浏览固定的示例快照，无须填写 Key，也不创建真实后台对局。示例并非模型运行结果。

对局动态默认开启“跟随最新”，新事件到达时自动滚到底部；想阅读历史时先关闭开关，界面会提示尚未查看的新动态数量。“沉底一次”只跳到当前底部，“顶部”会关闭跟随并回到开头。切换对局、代入身份或频道时隔离显示内容，避免保留上一局的终局播报。实现和离线验收结果见 [动态隔离与跟随修复说明](docs/reports/动态隔离与跟随修复说明.md)。

顶部“观战视角”支持全知与单一视角，可选择代入的座位。单一视角显示该席位的真实身份、技能和推理；代入狼人时，还可查看狼队友身份、狼队名单和夜间作战板，但不显示队友的私密推理或技能状态。其他身份、全知秘密记录、他人推理、全知统计与完整复盘导出隐藏。四类人数以进度条和数字卡片呈现；单一视角默认只显示存活/出局及开局配置，勾选“显示全知阵营人数”后才显示真实剩余阵营数。

外观由右上角 `⋮ → Theme` 原生菜单统一控制：`Light` 浅色、`Dark` 深色、`System` 跟随系统。原生输入框、问号说明、展开区和菜单使用同一主题；圆桌与人数条通过浏览器 `light-dark()` 跟随原生配色，即时切换，不重置对局。侧栏收起后不再保留展开宽度。详细实现和验收边界见 [主题与 API 稳定性修复](docs/reports/主题与API稳定性修复.md)。

本次参考五个项目的组件与交互组织方式，详见 [UI 调研与改版说明](docs/reports/UI调研与改版说明.md)。

`GameEngine.run()` 逐事件 yield，`GameService` 使用独立后台线程推进。玩家 API 和按体量触发的记忆压缩在 Future 中执行，推进器不会同步等待整次模型请求。

界面每 1.5 秒读取一份独立状态副本，页面刷新不再调用 `next(gen)`；关闭浏览器不影响正在执行的后台对局。暂停、单步、跳过、存档、上帝传音和生成器推进共用一把服务锁。工作线程不访问 Streamlit 会话状态，快照不包含 API 客户端或记录器。

跳过当前玩家会使其请求失效，迟到响应不会写入后续玩家记忆。重置或替换对局会取消待处理任务、关闭旧生成器、归档并释放任务池；已经执行的网络调用仍受原超时约束。

所有真实死亡统一经过 `GameEngine._mark_dead()` / `_mark_dead_batch()`。批量夜间死亡按同一批次结算，落地后检查胜负。

原工作台的视角切换仍是房主的显示模式。2026-10-26 新增可选的共享观战入口：创建新局前开启“共享观战与回放记录”，在“共享房间与独立回放”中生成公开、指定席位或全知邀请码。观众通过房间链接和邀请码进入独立页面，每次读取均由服务端校验权限；回看不会推进或暂停对局。当前仅支持本机同一服务进程，尚未开放远程访问、持久化调度或跨进程恢复。详见 [本地实现与验收说明](docs/reports/多人观战与回放本地实现说明.md)。

## 文件结构

仓库采用高内聚、低耦合的分层架构，核心业务划分为 5 大领域子包，根目录仅保留主程序入口与工程配置文件：

```text
SiliconWolfVillage/
├── engine/              # 🐺 规则引擎：状态机循环、角色与阵营、配置及版本元数据
│   ├── game_engine.py   # 生成器驱动主循环、投票、死亡链、胜负与复盘
│   ├── roles.py         # 角色注册表、技能执行器与板子合法性校验
│   ├── config.py        # 默认模型、Token 预算、超时及规则常量
│   └── runtime_version.py # 运行时源码快照与热更新哈希
├── agent/               # 🧠 智能体层：大模型推理、协议规整与记忆池
│   ├── ai_agent.py      # 多模型路由、Prompt 组装、记忆检索与后台执行
│   ├── model_protocol.py# OpenAI 协议适配、Token 预算策略与错误分类
│   └── public_memory.py # 公开记忆账本与多维证据检索
├── service/             # 🛰️ 服务层：多房间管理、观战会话与视角投影
│   ├── game_service.py  # 后台生命周期托管、串行锁控制、快照与自动归档
│   ├── room_registry.py # 共享房间注册表与观众权限校验
│   ├── room_view.py     # 观众视角定义与脱敏
│   ├── view_projection.py # 视角投影算法（狼人作战板/好人掩码）
│   └── live_feed.py     # 实时事件流发布/订阅与定时刷新
├── storage/             # 📦 持久化层：取证归档、复盘帧流与会话缓存
│   ├── game_archive.py  # 终局分析、样本质量评测、Markdown 摘要与索引
│   ├── replay_recorder.py# 原始 JSONL 事件流与断点续写
│   ├── replay_frames.py # 多视角回放展示帧管理
│   ├── replay_reader.py # 历史回放读取器
│   └── session_storage.py# 会话级临时目录隔离
├── ui/                  # 🎨 表现层：Streamlit 页面渲染与主题样式
│   ├── spectator_ui.py  # 共享观战房间与回放控制器界面
│   ├── feed_ui.py       # 动态流频道、定向滚动与消息卡片
│   ├── ui_design.py     # 圆桌布局、状态席位卡片与暗黑/明亮 CSS 样式
│   ├── ui_theme.py      # 主题色彩变量与 WCAG 对比度校验
│   ├── ui_preview.py    # 零 API 调用的离线 UI 预览演示
│   └── ui_runtime.py    # 前端事件轮询与平滑重绘节奏
├── docs/                # 📚 项目技术文档（手册 manuals/ 与专项报告 reports/）
├── tests/               # 🧪 全量自动化单元测试与回归套件
├── app.py               # 🎮 主应用入口（Streamlit 交互控制台）
├── 启动硅基狼村.bat      # Windows 专属一键启动脚本
├── pyproject.toml       # 项目构建与依赖声明
└── requirements.txt     # Python 核心依赖清单
```

## 存档与取证

新局发牌前创建本局档案目录，过程数据逐条追加并 flush。目录包含：

- `events.jsonl`：状态机事件、公共发言、完整票型、死亡、广播与结构化行动。
- `api_calls.jsonl`：请求消息、模型参数、耗时、原始及规整响应、错误、重试和 usage。
- `memory.jsonl`：System Prompt、私人底牌、注入、摘要前后内容与 FIFO 兜底。
- `session.json` / `runtime.log`：配置、随机种子及本局运行记录，不保存 Key。
- `final_state.json` / `manifest.json` / `summary.md`：全量状态、代码和 Prompt 哈希、票型计数、成本、样本质量与精简复盘。

启用共享记录后，本局目录额外保存 `replay_items.jsonl`、`replay_frames.jsonl` 和 `replay_manifest.json`。这些本地文件包含各授权视角的数据，不能直接作为公开下载；页面导出只包含当前获准画面。

会话的 `replays/index.csv` 汇总档案，可按胜负、模型、兜底率、Token 和投票表现分析。大面积兜底或熔断的对局标为无效样本，避免把服务故障当成模型能力差异。

手动存档仍使用 v6 格式：先暂停，若仍在等待玩家，则单步收集已完成的响应或跳过后保存。读档重建引擎，从存档当天的**下一夜**继续，不恢复原 Python 调用栈；读档后默认暂停。新存档使用原子写入和唯一文件名。

## 故障处理

- `HTTP 401/402/403`：不重复请求，自动暂停并显示原因。暂停且无后台任务时，修改配置后点击“应用模型设置（保持暂停）”，保留本局记忆、用量和棋盘，再测试或继续。
- 连续 3 个完整玩家行动失败：保留熔断并暂停，显示具体原因。每个行动最多调用两次，共用总时限；预算不足仅扩容一次，避免原样反复请求。超过剩余时限的限流等待会直接暂停。
- 超时或单个玩家卡住：超时后使用系统兜底；也可点击“跳过当前玩家”。继续会向后推进，不重做已记录的兜底响应。晚到响应仅补记用量，不能覆盖行动或清零失败计数。
- 归档失败：界面保留错误，不把失败的写盘显示为成功，也不丢失内存中的终局状态。
- 长局界面压力：默认仅渲染频道尾部；完整记录仍保存在取证流。保持使用 `requirements.txt` 指定的 Streamlit 版本及仓库提供的 `.streamlit/config.toml`。

## 验证

```sh
python -m unittest discover -s tests -v
```

测试不调用真实付费模型，覆盖思考预算耗尽、受控修复、协议校验、限流与总时限、迟到结果和用量隔离、熔断、暂停态应用设置、角色与屠边、统计、归档和 Streamlit 控件联调。真实模型长局需要另行限定预算验证。

2026-10-25 再次完成当前代码的 139 项离线回归，全部通过；测试进程禁止网络连接，未发生连接尝试。服务与归档核对、文档校准和剩余验证边界见 [接手核验与文档校准](docs/接手核验与文档校准.md)。

历史对局、日志、本机秘密配置和个人项目记忆不应提交到仓库。
