# 硅基狼村 · Silicon Wolf Village

**十二个 AI 同桌玩狼人杀，你在上帝视角看它们推理、伪装、投票和翻盘。**

本发布版由访客填写自己的 API Key，可分别选择好人和狼人使用的模型。
规则由 Python 引擎裁决，模型负责发言、推理和行动选择。

## 网页部署

本项目需要 Python 后端，不能直接通过 GitHub Pages 运行。
可使用 [Streamlit Community Cloud](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app)：
选择本仓库、`main` 分支、入口 `app.py`，使用 Python 3.11 或 3.12。
依赖由 `requirements.txt` 安装，无须配置站长的 API Key。

访客打开网页，在侧边栏填写自己的 Key、服务地址和模型名，先测试连接，再开始对局。
Key 会发送到托管服务器，用于向所填模型服务发起请求；密码输入框不代表 Key 只留在浏览器。
Key 不写入存档。一局可能产生大量请求和 Token，请关注用量统计。

存档和复盘按浏览器会话分别存储。重新连接、服务重启或休眠后可能无法找回，
需要保留的复盘请及时下载。公开版不提供全站日志查看、下载和清空入口。
本版本不会将服务器环境变量中的 Key 自动填给访客。

## 本地运行与验证

```sh
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

启动后在侧边栏输入 Key。运行自动测试：

```sh
python -m unittest discover -s tests -v
```

历史对局、存档、日志、本机秘密配置和个人项目记忆不包含在上传文件中。

---

# 赛博狼人杀 · 斗蛐蛐大厅

12 个大模型自动进行狼人杀博弈的 Streamlit 观战应用。Python 引擎负责全部规则裁决，AI 只负责推理、发言和提交结构化行动。

## 当前能力

- 12 人动态板子：标准预女猎守、白狼王、守墓人、狼美骑士、丘比特。
- 完整面杀流程：夜间行动、警长竞选与 PK、退水、盲投、警长 1.5 票、警徽移交、自爆与双爆吞警徽。
- 角色插件：技能提示、解析、状态与死亡动作集中在 `roles.py`。
- 三层记忆：不可压缩真理层、滚动摘要层、近期工作台。
- 阵营双模型：好人和狼人可分别选择 OpenAI 兼容模型。
- 上帝视角：圆桌状态、公开台面、上帝读心、狼队夜间作战板、票型图、复盘导出、实时 Token 账本和上帝传音。
- 存档与日志：手动进度存档位于 `saves/`；每局自动档案位于 `replays/`；运行日志写入 `runtime.log` 并自动滚动。

## 运行架构

`GameEngine.run()` 是逐事件 `yield` 的生成器。玩家请求和按体量触发的记忆压缩都在后台 Future 中运行；Streamlit 使用单一整页刷新通道，等待 Future 时至少间隔 1.5 秒轮询，避免 fragment 与整页 rerun 竞态，同时仍可安全地跳过当前玩家。

同一会话的 `next(gen)` 由真正的 `threading.Lock` 互斥保护。跳过操作只会使当前 `request_id` 失效并写入兜底结果，不会强制重入生成器；迟到的 API 响应会被丢弃，不会污染后续记忆。

所有真实死亡统一经过 `GameEngine._mark_dead()` / `_mark_dead_batch()`，死亡落地后立即执行胜负判定。批量夜间死亡仍按同一批次结算，保留狼刀在先与连环死亡语义。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `config.py` | API、模型、超时、板子、UI 节奏与滚动日志配置 |
| `ai_agent.py` | 多模型路由、三层记忆、JSON 规整、后台请求、重试与错误分级 |
| `roles.py` | 角色插件、技能规则、行动提示与结果解析 |
| `game_engine.py` | 生成器状态机、竞选/投票、死亡链、胜负与复盘 |
| `replay_recorder.py` | 每局实时 JSONL 取证流、原子快照与断点续写 |
| `game_archive.py` | 局末摘要、样本质量评估、战术指标与跨局 CSV 索引 |
| `app.py` | Streamlit 三栏观战界面、控制台、存读档与日志查看 |
| `tests/test_runtime_regressions.py` | 不访问真实 API 的关键运行时回归测试 |

## 快速开始

```powershell
cd "F:\Agent项目\硅基狼村"
pip install -r requirements.txt
streamlit run app.py
```

启动后在侧边栏填写 API Key、Base URL 和双方模型。第三方 OpenAI 兼容服务需要填写其实际可用的模型名；不要仅根据默认名称假定服务端支持。

## 自动对局档案

新局发牌前就会创建 `replays/<时间>_<game_id>/`，过程数据逐条追加并立即 flush；即使浏览器断线，已经发生的事件仍在磁盘。每局目录包含：

- `events.jsonl`：状态机事件、公共发言、完整票型、死亡、广播和每次结构化行动。
- `api_calls.jsonl`：每次请求的完整消息、模型参数、耗时、原始/规整响应、错误、重试和 token usage。
- `memory.jsonl`：System Prompt、私人底牌、全局注入、摘要前后内容和 FIFO 兜底。
- `session.json` / `runtime.log`：开局配置、随机种子和本局人类可读运行记录；不会保存 API Key。
- `final_state.json` / `manifest.json` / `summary.md`：终局全量快照、代码与 Prompt 哈希、完整日票型、缓存/推理/摘要 Token 成本、样本质量和精简复盘。

根目录的 `replays/index.csv` 汇总新旧档案，便于按胜负、模型、兜底率、token、放逐命中率和狼人自爆数做跨局分析。V2 会把 API 大面积兜底或触发熔断的对局标为无效样本，避免把服务故障误判成 Prompt 战术问题。旧版根目录 `.json/.md` 保留不动，并自动纳入索引。

## 故障处理

- `HTTP 401/402/403`：不会重复请求，自动推演会暂停并显示真实原因；修复 Key、余额或权限后点击继续。
- 连续 3 个完整玩家请求失败：触发 API 熔断并暂停自动推演，防止断网时整局以“过/弃权”跑成无效样本。
- 模型超时：当前行动返回“过/弃权”，流程继续；超时上限可在侧边栏调整。
- 单个玩家卡住：点击“跳过当前玩家”，当前请求立即失效，迟到结果不会写入记忆。
- 控制台反复出现 `WebSocketClosedError`、`Cached ForwardMsg MISS` 或 fragment 不存在：先执行 `pip install -r requirements.txt` 升级到 Streamlit 1.57，再完全关闭旧服务后重启。项目已禁用 `fastReruns`、延长消息缓存寿命、取消高频计时 iframe，并默认只渲染频道尾部；完整记录始终保存在对局归档中。
- 排查长局：侧边栏查看或下载 `runtime.log`。重点搜索 `ASK`、`API→`、`API✓`、`API✗`、`SKIP`、`DEATH` 和 `STEP✗`。
- 存档只允许在没有后台任务时进行；读档会重建引擎并从下一夜的干净边界继续。

## 验证

```powershell
$env:PYTHONDONTWRITEBYTECODE = "1"
python -m unittest discover -s tests -v
```

测试覆盖取消后迟到结果隔离、402 不重试、连续失败熔断、结构化自爆判定、事实账本主客体不串位、实时 JSONL、记忆压缩非阻塞与按量跳过、生成器等待/跳过、统一死亡入口和即时胜负判定。

