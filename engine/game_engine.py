# -*- coding: utf-8 -*-
"""
game_engine.py —— 狼人杀状态机引擎（V3.0 高配面杀）
==================================================
核心理念：规则由 Python 绝对控制，AI 只是“演员”。
生成器架构：每个“可观看步骤”yield 一次，由 GameService 在后台推进，前端读取快照。

V3.0 新增（均不破坏 yield 状态机与硬超时防卡死）：
  · 警长竞选状态机：election_state 0未开始/1竞选中/2已顺延/3已有警长/4徽毁。
    上警 → 随机起始发言 → 退水 → 警下盲投 → 选出警长（票×1.5）。
  · 自爆中断：白天任何【顺序发言】里狼人可 is_explode=true 自爆 →
      - 竞选状态1自爆：顺延(→2)，中断白天入夜；
      - 复选状态2自爆：双爆吞警徽(→4)，中断白天入夜；
      - 常规白天自爆：中断发言、跳过放逐投票，入夜。
  · 警徽移交：警长出局触发 _transfer_badge，可移交或撕毁。
  · 女巫面杀规则：解药仅首夜可自救；解药/毒药当晚互斥。
  · 完整票型公开 + 警长1.5票加权计票，并广播注入全体 AI 记忆。
"""

import json
import random
import re
import uuid
from collections import Counter
from datetime import datetime

from engine import config
from storage.replay_recorder import GameRecorder
# ★V6.3 角色多态：解析工具与角色类下沉到 roles.py（roles 不反向 import 本模块 → 无循环依赖）
from engine.roles import create_role, parse_seat, validate_board_roles


# ============================================================
# 狼队“刀法心理博弈”战术警告（夜间讨论 & 落刀两处共用，防无脑刀明跳神）
# ============================================================
WOLF_KILL_TACTICS = (
    "【狼队落刀】比较关键技能威胁、守救概率、猎枪风险与屠边进度。"
    "明跳神职可能被保护，也可能是最高收益刀口；边缘位不自动更安全。"
    "【狼队分工】不仅定刀口，还要安排白天主悍跳/代跳、深水/倒钩、压身份/抗推与归票位；不要求每局全用，避免所有狼同线机械互保。"
    "【反向金水/做身份】给队友金水可以建立可信关系，也会留下关系链；给好人金水可借势或留后手。评估发金水者、时机、接收者后续站边和票型，不把金水直接当真或假。"
    "【警徽链】若狼自爆/出局后把警徽交给X，X可能是队友接力，也可能是被故意甩锅的好人；预判好人会怎样反盘，不能只凭交徽定狼或定好。"
    "【关系链执行】先检查事实账本中实际存在的金水/查验声明和警徽流转；若涉及机械确认狼，必须选一条最相关关系，比较至少两种解释并指出下一轮可核验的发言、站边或票型。没有相关证据就明确不适用，不得编造。"
    "【代跳与传话执行】讨论时明确是否需要主悍跳、代跳接力、深水倒钩、压身份/抗推与归票；可决定不采用。传话应通过自洽的公开站边、质疑和归票完成，不用明显暗号，也不要求所有狼机械同边。"
    "【风险控制】每次只选最符合当前局势的1—2个策略，明确目标、收益、暴露风险和备选方案。"
    "只根据本局实际存在角色与已知信息决策，不把未公开的底牌当作公开共识。"
)


# ============================================================
# 游戏引擎主体
# ============================================================
class GameEngine:
    def __init__(self, manager):
        self.manager = manager
        self.game_id = uuid.uuid4().hex[:12]
        self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.board_name = ""
        self.archive_basename = None
        self.archive_complete = False
        self.random_seed = random.SystemRandom().randrange(0, 2**63)
        self.rng = random.Random(self.random_seed)
        self.recorder = None

        # ---- 可渲染状态（前端只读这些）----
        self.players: dict[int, dict] = {}
        self.public_log: list[dict] = []
        self.published_alive = {}
        self.public_speaker = None
        self.war_board_recipients = []
        self.reasoning_log = {i: [] for i in range(1, 13)}
        self.current_speaker = None
        self.phase = "准备中"
        self.day = 0
        self.winner = None
        self.sheriff = None              # 当前警长座位（None=无）

        # ---- 规则内部状态 ----
        self.wolves: list[int] = []
        self.role_seats: dict[str, int] = {}
        self.roles: dict[int, object] = {}   # ★V6.3：座位→角色实例(技能数据下沉到实例)
        self.night = {}
        self._last_resp = None
        self.election_state = 0          # 0未开始 1竞选中 2已顺延 3已结束(可能有警长) 4徽毁
        self._day_interrupted = False    # 自爆等导致本白天提前结束
        self.war_board = None            # 狼队夜间作战板（最近一夜的分工与刀法），供上帝视角展示
        self.election_speeches = []      # 本日警长竞选的“警上发言”，供白天发言阶段继承(治警上发言失忆症)
        self.replay_logs = []            # 上帝复盘档案：只记真身与关键动作，可导出 Markdown
        self.objective_events = []        # 全场公开客观事件；进入每个AI的不可压缩事实账本
        self.public_claims = []           # 公开身份/查验声明（只记录“谁声称什么”，不判真伪）
        self.confirmed_roles = {}         # 由公开技能机械确认的身份，如发动猎枪者
        self.badge_history = []           # 公开警徽流转链；用于关系审计，不等同于身份认证
        self.explosion_events = []        # 真实执行过的自爆事件；复盘统计不得从自然语言猜测
        self._resume = False             # ★读档恢复标记：True 时 run() 跳过开场、从下一夜的干净边界续跑
        # ---- V6.5/6.6 变体角色 & 连环死亡状态 ----
        self.last_exiled_seat = None     # 守墓人：昨日被公投放逐者座位
        self.last_exiled_camp = None     # 守墓人：其真实阵营(好人/狼人)
        self.charm_target = None         # 狼美人：当前魅惑链目标(狼美人一死则它殉情)
        self._charm_sprung = False       # 狼美人：殉情是否已触发(幂等)
        self.lovers = None               # 丘比特：情侣对 (a, b)，一方死另一方殉情(纯双死)
        self._resolving_chain = False    # 连环死亡结算重入哨兵(防 _run_death_action↔_resolve_chain_deaths 互递归)
        self.pending_night_deaths = {}   # ★V6.7 死人上警：竞选日昨夜死者{座位:死因}，竞选期间暂以活人参选(死不知情)，竞选后才真正处死+公布
        self._react_left = 0             # ★V6.7 反应式抢爆/抢决斗的当日剩余预算(每天重置, 硬封顶防全场轮询)
        self._react_cursor = 0           # 每个威胁窗口只询问一只狼，按存活狼轮转，避免逐狼重复请求
        self.react_budget = int(getattr(config, "EXPLODE_REACT_BUDGET", 0))  # 每日预算(可被前端滑块实时改写)
        # 非阻塞 AI 行动状态（不进存档；读档从干净夜晚边界重建）
        self._active_request_id = None
        self._active_compression_id = None
        self._skip_current = False
        self.fatal_error = ""
        # 真人模式：固定一个真人座位；其余座位仍由 AI 驱动。
        self.human_seat = None
        self.pending_human_action = None
        self._pending_human_context = None
        self.human_private_messages = []
        # 当前夜晚已完成的狼队讨论；只通过狼人视角投影给狼队成员。
        self.current_wolf_talk = []
        # 白天自爆的狼人可按本项目规则参与下一夜最后一次讨论与落刀。
        # pending 可存档；active 仅表示狼队阶段仍可见狼队私密信息，不序列化。
        self.pending_wolf_night_seats: list[int] = []
        self.pending_wolf_night_day: int | None = None
        self.active_last_stand_wolves: list[int] = []

    # ========================================================
    # 发牌与初始化
    # ========================================================
    def setup(self, board_roles=None):
        # ★V6.4：板子可切换。board_roles 为身份列表(12个)；不传则用默认板子。
        src = validate_board_roles(
            config.GAME_BOARDS[config.DEFAULT_BOARD] if board_roles is None else board_roles
        )
        # ★V6.6.2：按【本局真实板子】生成身份名单，注入 System Prompt(治变体板被告知"白狼王不存在"的降智)。
        self._roster = "、".join(f"{r}×{src.count(r)}" for r in dict.fromkeys(src))
        roles = list(src)
        self.rng.shuffle(roles)
        styles = list(config.STYLE_POOL)
        self.rng.shuffle(styles)

        for seat in range(1, 13):
            self.players[seat] = {
                "role": roles[seat - 1], "alive": True,
                "style": styles[(seat - 1) % len(styles)], "memory": "",
            }
        # ★V6.3/6.4：先实例化角色对象(含白狼王)，再【按 camp】算狼队与神职座位——
        #   白狼王 camp=狼人，故算进 wolves、且【不进 role_seats】(它没有单座位夜间技能，夜杀走狼队集体)。
        self.roles = {s: create_role(self.players[s]["role"], s) for s in range(1, 13)}
        self.wolves = [s for s in range(1, 13) if self.roles[s].camp == "狼人"]
        self.role_seats = {}
        for s in range(1, 13):
            if self.roles[s].camp != "狼人":
                self.role_seats[self.players[s]["role"]] = s

        self.published_alive = {s: True for s in self.players}
        # 先彻底清空所有 AI 记忆，确保全新对局
        self.manager.clear()
        for seat in range(1, 13):
            role = self.players[seat]["role"]
            pm = self._private_memory(seat, role)
            self.players[seat]["memory"] = pm
            self.manager.init_agent(seat, role, self.players[seat]["style"], pm, roster=self._roster)

        self.sheriff = None
        self.election_state = 0
        self._day_interrupted = False
        self.war_board = None
        self.election_speeches = []
        # V6.5/6.6 变体状态复位
        self.last_exiled_seat = None
        self.last_exiled_camp = None
        self.charm_target = None
        self._charm_sprung = False
        self.lovers = None
        self._resolving_chain = False
        self.pending_wolf_night_seats = []
        self.pending_wolf_night_day = None
        self.active_last_stand_wolves = []
        # —— 复盘档案：开篇 + 真身配置 ——
        self.replay_logs = []
        self.objective_events = []
        self.public_claims = []
        self.confirmed_roles = {}
        self.badge_history = []
        self.explosion_events = []
        self._add_replay("# ⚔️ 赛博狼人杀 · 上帝复盘档案\n")
        self._add_replay("## 🎭 身份配置")
        for s in range(1, 13):
            self._add_replay(f"- {s}号：{config.ROLE_EMOJI.get(self.players[s]['role'], '')} {self.players[s]['role']}")
        self._log_judge("🧹 已清空全部 AI 记忆，本局为全新对局。")
        self._log_judge("🎲 发牌完毕！12 名硅基玩家已就坐，身份已私密分发。")
        self._record_event(
            "game_setup",
            board_name=self.board_name or getattr(self, "_roster", ""),
            roster=getattr(self, "_roster", ""),
            random_seed=self.random_seed,
            players={
                str(s): {
                    "role": self.players[s]["role"],
                    "camp": self.roles[s].camp,
                    "style": self.players[s]["style"],
                }
                for s in range(1, 13)
            },
            wolves=list(self.wolves),
        )

    def _private_memory(self, seat: int, role: str) -> str:
        # ★V6.3：身份私密信息委托给角色实例(get_private_memory)；role 形参保留以兼容调用签名。
        return self.roles[seat].get_private_memory(self)

    # ★V6.3 兼容视图：技能数据已下沉到各角色实例，这里现拼成老的 skills 结构，
    #   让 app.py（内心监控室技能行）与复盘按原结构读取，零改动、零外部表现差异。
    @property
    def skills(self):
        def rb(name):
            seat = self.role_seats.get(name)
            return self.roles.get(seat) if seat else None
        w, g, se, h = rb("女巫"), rb("守卫"), rb("预言家"), rb("猎人")
        return {
            "witch": {"antidote": getattr(w, "antidote_available", False),
                      "poison": getattr(w, "poison_available", False)},
            "guard": {"last": getattr(g, "last_guarded", None)},
            "seer": {"checks": getattr(se, "checks", [])},
            "hunter": {"can_shoot": getattr(h, "can_shoot", False),
                       "shot": getattr(h, "shot", None)},
        }

    def _role_by_name(self, name):
        """按身份名取角色实例（守卫/女巫/预言家/猎人等单一神职，走 role_seats）。"""
        seat = self.role_seats.get(name)
        return self.roles.get(seat) if seat else None

    def _seat_with_role(self, role_name):
        """在所有角色实例里按 role_name 找座位(含狼阵营，如狼美人/白狼王)；找不到返回 None。"""
        for s, r in self.roles.items():
            if r.role_name == role_name:
                return s
        return None

    def _solo(self, role_name):
        """取该身份的角色实例(供夜晚 solo 驱动)；不存在(本板子没这身份/座位空)则 None。"""
        return self.roles.get(self._seat_with_role(role_name))

    def _wolfbeauty_seat(self):
        return self._seat_with_role("狼美人")

    def _third_party_set(self):
        """★V6.6.1 情侣第三方阵营(上帝视角，按真实 camp 判)：仅当情侣【跨阵营(一好一狼)】才成立。
        - 同阵营情侣(人人/狼狼/丘比特自连好人) → 无第三方(丘比特好人向，走标准屠边)。
        - 跨阵营情侣(丘比特自连狼 / 非自连人狼恋) → 第三方 = 两情侣 ∪ 丘比特(自连时丘比特本就是情侣之一)。
        - 情侣双死保证两人同生共死；两人皆亡则第三方瓦解、返回空集(丘比特回归好人向)。"""
        if not self.lovers:
            return set()
        a, b = self.lovers
        if self.roles[a].camp == self.roles[b].camp:
            return set()
        if not (self.players[a]["alive"] or self.players[b]["alive"]):
            return set()
        tp = {a, b}
        cupid = self._seat_with_role("丘比特")
        if cupid is not None:
            tp.add(cupid)
        return tp

    # ========================================================
    # 小工具
    # ========================================================
    def alive_seats(self) -> list[int]:
        return [s for s in range(1, 13) if self.players[s]["alive"]]

    def alive_str(self) -> str:
        return "、".join(f"{s}号" for s in self.alive_seats())

    def _recording_context(self) -> dict:
        return {
            "day": self.day,
            "phase": self.phase,
            "current_speaker": self.current_speaker,
        }

    def _compact_state(self) -> dict:
        if not self.players:
            alive, dead = [], []
        else:
            alive = self.alive_seats()
            dead = [s for s in range(1, 13) if not self.players[s]["alive"]]
        return {
            "day": self.day,
            "phase": self.phase,
            "alive": alive,
            "dead": dead,
            "sheriff": self.sheriff,
            "election_state": self.election_state,
            "winner": self.winner,
            "current_speaker": self.current_speaker,
        }

    def enable_recording(self, directory) -> GameRecorder:
        """开启实时逐事件归档。重复调用幂等，读档后会继续写入同一局目录。"""
        if self.recorder is None:
            self.recorder = GameRecorder(
                root=directory,
                game_id=self.game_id,
                started_at=self.started_at,
                basename=self.archive_basename,
            )
            self.archive_basename = self.recorder.basename
            self.manager.set_recorder(self.recorder, self._recording_context)
            self.recorder.record_event(
                "recording_started",
                resumed=bool(self._resume),
                board_name=self.board_name,
                roster=getattr(self, "_roster", ""),
                random_seed=self.random_seed,
                state=self._compact_state(),
            )
            self.recorder.write_json("session.json", {
                "schema_version": 2,
                "game_id": self.game_id,
                "started_at": self.started_at,
                "board_name": self.board_name,
                "random_seed": self.random_seed,
                "resumed": bool(self._resume),
                "models": {
                    "good": getattr(self.manager, "good_model", ""),
                    "wolf": getattr(self.manager, "wolf_model", ""),
                    "fallback": getattr(self.manager, "fallback_model", ""),
                },
                "temperature": getattr(self.manager, "temperature", None),
                "agent_timeout": getattr(self.manager, "agent_timeout", None),
                "max_retries": getattr(self.manager, "max_retries", None),
            })
            self.recorder.log(
                "INFO", f"RECORDER started resumed={self._resume} seed={self.random_seed}"
            )
        return self.recorder

    def _record_event(self, event: str, **payload) -> None:
        if self.recorder is None:
            return
        try:
            self.recorder.record_event(event, state=self._compact_state(), **payload)
        except Exception as exc:
            config.get_logger().exception(
                "RECORDER event write failed event=%s error=%s", event, exc
            )

    def _mark_dead(self, seat: int, cause: str = "", check: bool = True) -> bool:
        """唯一的真实死亡写入口；返回该座位是否由存活变为死亡。"""
        player = self.players.get(seat)
        if not player or not player.get("alive"):
            return False
        player["alive"] = False
        if "夜" not in cause and "夜" not in self.phase and "读档" not in cause:
            self.published_alive[seat] = False
        config.get_logger().info(f"DEATH seat={seat} role={player.get('role')} cause={cause or '未标注'}")
        self._record_event(
            "player_died",
            seat=seat,
            role=player.get("role"),
            camp=getattr(self.roles.get(seat), "camp", ""),
            cause=cause or "未标注",
        )
        if check:
            self.winner = self.check_winner()
        return True

    def _mark_dead_batch(self, seats, cause: str = "") -> list[int]:
        """同时落地一批死亡，整批完成后只判定一次胜负，保留夜间/连锁的同时结算语义。"""
        changed = []
        for seat in dict.fromkeys(seats):
            if self._mark_dead(seat, cause, check=False):
                changed.append(seat)
        self.winner = self.check_winner()
        return changed

    def _ev(self, etype: str, status: str = "") -> dict:
        event = {"type": etype, "status": status, "phase": self.phase, "day": self.day}
        if etype not in {"waiting", "compress_waiting"}:
            self._record_event("engine_yield", yield_event=event)
        return event

    def _append_public_item(self, item):
        # 同局递增序号用于跨局隔离、滚动游标和归档定位。
        self._public_event_seq = getattr(self, "_public_event_seq", len(self.public_log)) + 1
        item.update(game_id=self.game_id, event_seq=self._public_event_seq)
        self.public_log.append(item)

    def _log_judge(self, text):
        item = {"kind": "judge", "text": text, "phase": self.phase}
        self._append_public_item(item)
        self._record_event("public_log", entry=item)

    def _log_god(self, text):
        item = {"kind": "god", "text": text, "phase": self.phase}
        self._append_public_item(item)
        self._record_event("public_log", entry=item)

    def _log_player(self, seat, text):
        item = {"kind": "player", "seat": seat, "role": self.players[seat]["role"],
                "text": text, "phase": self.phase}
        self._append_public_item(item)
        self._capture_public_claims(seat, text)
        self._record_event("public_log", entry=item)

    def _log_win(self, text):
        item = {"kind": "win", "text": text, "phase": self.phase}
        self._append_public_item(item)
        self._record_event("public_log", entry=item)

    def _log_votechart(self, title: str, totals: dict, votes: dict, winner, decision_sources=None):
        """把一轮票型存成【结构化记录】并按顺序内嵌进 public_log，供前端画横向柱状图。
        JSON 友好：键全部转 int(读档后是 str，前端渲染时会再 coerce 一次，双保险)。"""
        item = {
            "kind": "votechart", "phase": self.phase, "title": title,
            "totals": {int(k): float(v) for k, v in (totals or {}).items()},
            "votes": {int(k): int(v) for k, v in (votes or {}).items()},
            "winner": (int(winner) if winner else None),
            "sheriff": self.sheriff,
            "decision_sources": dict(decision_sources or {}),
        }
        self._append_public_item(item)
        self._record_event("vote_result", entry=item)

    def _record_reasoning(self, seat, label, reasoning, speech, response=None, request_id=None):
        response = response or {}
        is_human_action = bool(response.get("_human_action") or response.get("decision_source") == "human")
        # 安全不变量：真人座位绝不能把模型响应记成自己的行动。
        # 即使将来有新行动路径绕过 _ask 的真人分支，也必须 fail closed。
        if seat == self.human_seat and not is_human_action:
            raise RuntimeError(f"安全保护：真人席位 {seat:02d} 的行动不是由真人提交，已阻止结算（{label}）。")
        item = {"label": label, "reasoning": reasoning, "speech": speech,
                "actor_source": "human" if is_human_action else "ai"}
        if response:
            item["response"] = response
        if request_id:
            item["request_id"] = request_id
        self.reasoning_log[seat].append(item)
        self._record_event(
            "agent_action",
            seat=seat,
            role=self.players.get(seat, {}).get("role", ""),
            label=label,
            request_id=request_id,
            response=response or {"reasoning": reasoning, "speech": speech},
        )

    def _combined_text(self) -> str:
        r = self._last_resp or {}
        return f"{r.get('speech', '')} {r.get('reasoning', '')}"

    def _truth_state(self) -> str:
        """全局‘绝对真理层’文本：当前轮次/存活名单/警长——交给 manager 注入，永不被压缩。"""
        sheriff = f"{self.sheriff}号" if self.sheriff else "无"
        return f"当前进行到第{self.day}轮。存活玩家：{self.alive_str()}。当前警长：{sheriff}。"

    def _capture_public_claims(self, seat: int, text: str) -> None:
        """把关键公开声明结构化，防止摘要把“A给B金水”改写成别的关系。"""
        speech = str(text or "")
        role_match = re.search(
            r"(?:我是\s*(?:\d{1,2}\s*号)?\s*[，,、 ]*|我(?:这里)?(?:跳|自称)|跳明)"
            r"(预言家|女巫|猎人|守卫|守墓人|骑士|丘比特|摄梦人|恶灵骑士|魔术师|平民)",
            speech,
        )
        if role_match:
            self._add_public_claim({
                "kind": "role", "day": self.day, "seat": seat,
                "claimed_role": role_match.group(1),
            })

        check_pattern = re.compile(
            r"(?:我\s*(?:昨晚|今晚)?\s*(?:查验|验了?|查了?|查的是|验的是)|"
            r"昨晚\s*(?:我)?\s*(?:查验|验了?|查了?|查的是|验的是))"
            r"\s*(\d{1,2})\s*号?[^。；\n]{0,16}?(金水|好人|查杀|狼人)"
        )
        for match in check_pattern.finditer(speech):
            target = int(match.group(1))
            if not 1 <= target <= 12:
                continue
            result = "好人" if match.group(2) in {"金水", "好人"} else "狼人"
            self._add_public_claim({
                "kind": "seer_check", "day": self.day, "seat": seat,
                "target": target, "result": result,
            })

    def _add_public_claim(self, claim: dict) -> None:
        key = tuple((k, claim.get(k)) for k in ("kind", "seat", "claimed_role", "target", "result"))
        for existing in self.public_claims:
            old = tuple((k, existing.get(k)) for k in ("kind", "seat", "claimed_role", "target", "result"))
            if old == key:
                existing["last_day"] = self.day
                return
        claim["last_day"] = self.day
        self.public_claims.append(claim)
        self.public_claims = self.public_claims[-40:]
        self._record_event("public_claim", claim=claim)

    def _confirm_role(self, seat: int, role_name: str, reason: str) -> None:
        """登记所有玩家都能机械确认的身份，不泄露普通死者真身。"""
        self.confirmed_roles[int(seat)] = {"role": role_name, "reason": reason, "day": self.day}
        self._record_event(
            "role_mechanically_confirmed", seat=seat, role=role_name, reason=reason,
        )

    def _hard_facts_for(self, seat: int) -> str:
        """按该玩家合法视角生成不可压缩账本，不把上帝身份泄露给其他座位。"""
        player = self.players[seat]
        role = self.roles[seat]
        alive = "、".join(f"{s}号" for s in self.alive_seats()) or "无"
        dead = "、".join(f"{s}号" for s in range(1, 13) if not self.players[s]["alive"]) or "无"
        lines = [
            f"- 你的真实底牌：{seat}号={player['role']}，阵营={role.camp}。",
            f"- 当前轮次：第{self.day}轮；存活={alive}；已出局={dead}；警长={self.sheriff or '无'}。",
            f"- 你的初始私密信息：{player.get('memory', '')}",
            "- 你的真实技能状态(JSON，由引擎维护)："
            + json.dumps(role.skill_state(), ensure_ascii=False, sort_keys=True),
        ]
        if role.camp == "狼人":
            lines.append("- 你的真实狼队友：" + "、".join(f"{s}号" for s in self.wolves) + "。")
        if self.lovers and (seat in self.lovers or role.role_name == "丘比特"):
            lines.append(f"- 你合法知晓的情侣关系：{self.lovers[0]}号与{self.lovers[1]}号；这不揭示对方底牌。")
        if self.confirmed_roles:
            confirmed = "；".join(
                f"{s}号={info['role']}（{info['reason']}）"
                for s, info in sorted(self.confirmed_roles.items())
            )
            lines.append("- 全场机械确认身份：" + confirmed + "。")
        confirmed_wolves = {
            int(s) for s, info in self.confirmed_roles.items()
            if getattr(config.ROLE_CLASSES.get(info.get("role")), "camp", "") == "狼人"
        }
        if self.badge_history:
            transfers = "；".join(
                f"D{item.get('day', '?')}：{item.get('from')}号→{item.get('to')}号"
                for item in self.badge_history
            )
            lines.append("- 公开警徽流转（仅代表交徽者的选择，不认证身份）：" + transfers + "。")
            lines.append(
                "- 警徽关系链推理：若交徽者已机械确认狼人，接徽对象是高价值审查对象；"
                "但也可能是交徽者故意甩锅。结合接徽者后续站边、归票、投票、查验链和狼队收益判断，"
                "不能仅凭交徽定狼或定好。"
            )
            alerts = []
            for item in self.badge_history:
                source, target = int(item.get("from", 0)), int(item.get("to", 0))
                if target in confirmed_wolves:
                    alerts.append(f"{source}号曾把警徽交给后来机械确认的狼人{target}号")
                if source in confirmed_wolves:
                    alerts.append(
                        f"机械确认的狼人{source}号曾把警徽交给{target}号"
                        "（接徽者需比较队友接力与故意甩锅给好人两种解释）"
                    )
            if alerts:
                lines.append(
                    "- 关系审计警报（高风险关联，但不是自动定狼）："
                    + "；".join(dict.fromkeys(alerts))
                    + "。必须重新审查整条警徽链及其收益，不能把警徽当金水。"
                )
        role_claims = [c for c in self.public_claims if c.get("kind") == "role"]
        if role_claims:
            lines.append("- 公开身份声明（只是声明，不保证为真）：" + "；".join(
                f"{c['seat']}号声称自己是{c['claimed_role']}" for c in role_claims
            ) + "。")
        check_claims = [c for c in self.public_claims if c.get("kind") == "seer_check"]
        if check_claims:
            lines.append("- 公开查验声明（主语和宾语绝不可改写；声明者未必是真预言家）：" + "；".join(
                f"{c['seat']}号声称查验{c['target']}号为{c['result']}" for c in check_claims
            ) + "。")
            claim_pairs = []
            for claim in check_claims:
                try:
                    issuer = int(claim.get("seat"))
                    target = int(claim.get("target"))
                except (TypeError, ValueError):
                    continue
                claim_pairs.append(
                    f"{issuer}号→{target}号（声明查验{claim.get('result', '未知')}）"
                )
            if claim_pairs:
                lines.append(
                    "- 金水/查验关系链（全部是公开声明，不是身份认证）：" + "；".join(claim_pairs) + "。"
                )
                lines.append(
                    "- 对每条‘好人/金水’声明，先判断发言者是否真预言家，再比较至少两种解释："
                    "狼给队友做身份、狼给好人金水以便利用/甩锅、或真预言家报出真实结果。"
                    "核对接收者后续站边、投票、对跳与谁获益；不要把金水当硬身份，也不要要求每名玩家机械重复。"
                )
            lines.append(
                "- 反向金水分析：若发金水者可能是狼，接收者可能是队友被做身份，也可能是被利用或甩锅的好人；"
                "被金水者反水也可能是独立判断或狼人切割。结合对跳、发言时间线、警徽流、票型和后续行为比较解释，"
                "不能自动认好或认狼。"
            )
            relation_alerts = []
            for claim in check_claims:
                if claim.get("result") != "好人":
                    continue
                try:
                    issuer = int(claim.get("seat"))
                    target = int(claim.get("target"))
                except (TypeError, ValueError):
                    continue
                if issuer in confirmed_wolves:
                    relation_alerts.append(
                        f"机械确认狼人{issuer}号曾公开给{target}号发金水/好人查验声明；"
                        "审查目标是否队友被做身份，也要考虑该狼故意给好人发金水以便后续甩锅"
                    )
                if target in confirmed_wolves:
                    relation_alerts.append(
                        f"{issuer}号曾公开给后来机械确认狼人{target}号发金水/好人查验声明；"
                        "需审查声明者的身份、发言时机与后续票型，但不能仅凭这条声明认定双方同阵营"
                    )
            if relation_alerts:
                lines.append(
                    "- 反向金水关系审计（基于公开声明与机械确认事实，不自动定性）："
                    + "；".join(dict.fromkeys(relation_alerts))
                    + "。至少比较两种解释，并用后续站边、投票或公开声明验证"
                )
        if self.objective_events:
            lines.append("- 最近公开客观事件：" + "｜".join(self.objective_events[-24:]))
        lines.append(
            "- 自爆只机械确认自爆者本人属于狼人阵营；绝不能据此反向坐实其前任警长、支持对象、"
            "对跳方或反对者是好人。"
        )
        lines.append("- 事实账本没有写明的身份关系一律不是硬信息；多数票、警徽移交和他人态度都不等于身份认证。")
        return "\n".join(lines)

    def _refresh_hard_facts(self, seats=None) -> None:
        for seat in (list(seats) if seats is not None else range(1, 13)):
            if seat in self.players:
                self.manager.set_hard_facts(seat, self._hard_facts_for(seat))

    def _broadcast_event(self, msg: str):
        """把一条客观事实硬注入【全场12人】的私人记忆（含已死者）——
        死者(如待开枪的猎人/待留遗言者)也必须知道自己的死讯，否则会对已死玩家乱开枪。"""
        for seat in range(1, 13):
            self.manager.inject(seat, msg)
        if msg not in self.objective_events:
            self.objective_events.append(msg)
            self.objective_events = self.objective_events[-40:]
        self._record_event("global_broadcast", message=msg, recipients=list(range(1, 13)))
        self._log_god(f"📡 已向全场12名AI同步客观事实：{msg}")

    # ========================================================
    # 上帝复盘档案：只记真身与关键动作，可一键导出 Markdown
    # ========================================================
    def _add_replay(self, text: str):
        self.replay_logs.append(text)
        self._record_event("replay_line", text=text)

    def export_replay_text(self) -> str:
        if not self.replay_logs:
            return "（暂无复盘内容，开始游戏后自动生成。）"
        return "\n".join(self.replay_logs)

    # ========================================================
    # 存档 / 读档（V6.0）：序列化【生死/天数/警长/技能/日志等核心状态】。
    #   ⚠️ 生成器无法原位恢复 → 读档侧会重建引擎+本方法注入状态，再让 run() 从下一夜续跑。
    #   字典 int 键转 str 以兼容 JSON；读档时转回 int。
    # ========================================================
    def export_state(self) -> dict:
        return {
            "game_id": self.game_id,
            "started_at": self.started_at,
            "board_name": self.board_name,
            "archive_basename": self.archive_basename,
            "archive_complete": self.archive_complete,
            "random_seed": self.random_seed,
            "players": {str(s): {"role": p["role"], "alive": bool(p["alive"]),
                                 "style": p.get("style", ""), "memory": p.get("memory", "")}
                        for s, p in self.players.items()},
            "public_log": self.public_log,
            "published_alive": self.published_alive,
            "war_board_recipients": self.war_board_recipients,
            "reasoning_log": {str(s): v for s, v in self.reasoning_log.items()},
            "phase": self.phase,
            "day": self.day,
            "winner": self.winner,
            "sheriff": self.sheriff,
            "wolves": list(self.wolves),
            "role_seats": dict(self.role_seats),
            # ★V6.3：技能状态由各角色实例提供(替代老的 skills/guard_last 平铺结构)
            "role_skills": {str(s): self.roles[s].skill_state() for s in self.roles},
            "election_state": self.election_state,
            "war_board": self.war_board,
            "election_speeches": self.election_speeches,
            "replay_logs": self.replay_logs,
            "objective_events": self.objective_events,
            "public_claims": self.public_claims,
            "confirmed_roles": {str(k): v for k, v in self.confirmed_roles.items()},
            "badge_history": self.badge_history,
            "explosion_events": self.explosion_events,
            # ★V6.5/6.6 变体引擎态(角色技能态已在 role_skills 里)
            "last_exiled_seat": self.last_exiled_seat,
            "last_exiled_camp": self.last_exiled_camp,
            "charm_target": self.charm_target,
            "charm_sprung": self._charm_sprung,
            "lovers": list(self.lovers) if self.lovers else None,
            "pending_night_deaths": {str(k): v for k, v in self.pending_night_deaths.items()},
            "pending_wolf_night_seats": list(self.pending_wolf_night_seats),
            "pending_wolf_night_day": self.pending_wolf_night_day,
        }

    def load_state(self, data: dict):
        data = data or {}
        self.published_alive = {int(k): v for k, v in data.get("published_alive", {}).items()}
        self.war_board_recipients = list(data.get("war_board_recipients", []))
        self.game_id = data.get("game_id") or self.game_id
        self.started_at = data.get("started_at") or self.started_at
        self.board_name = data.get("board_name", "") or ""
        self.archive_basename = data.get("archive_basename")
        self.archive_complete = bool(data.get("archive_complete", False))
        self.random_seed = int(data.get("random_seed", self.random_seed))
        self.rng = random.Random(self.random_seed)
        self.players = {}
        for s, p in (data.get("players") or {}).items():
            p = p or {}
            self.players[int(s)] = {"role": p.get("role", "未知"), "alive": bool(p.get("alive", True)),
                                    "style": p.get("style", ""), "memory": p.get("memory", "")}
        self.public_log = list(data.get("public_log", []) or [])
        # 只有明确的整局读档入口可以为旧日志绑定归属；已带外局ID的行保留供投影层隔离。
        self._public_event_seq = 0
        for index, original in enumerate(self.public_log):
            row = dict(original)
            row.setdefault("game_id", self.game_id)
            sequence = row.get("event_seq")
            if type(sequence) is not int or sequence <= self._public_event_seq:
                sequence = self._public_event_seq + 1
            row["event_seq"] = sequence
            self._public_event_seq = sequence
            self.public_log[index] = row
        self.reasoning_log = {int(s): list(v or []) for s, v in (data.get("reasoning_log") or {}).items()}
        for i in range(1, 13):
            self.reasoning_log.setdefault(i, [])
        self.current_speaker = None
        self.phase = data.get("phase", "准备中")
        self.day = int(data.get("day", 0))
        self.winner = data.get("winner")
        self.sheriff = data.get("sheriff")
        self.wolves = [int(x) for x in (data.get("wolves") or [])]
        self.role_seats = {k: int(v) for k, v in (data.get("role_seats") or {}).items()}
        # ★V6.3：按 players 身份重建角色实例，再灌入技能状态（技能数据已下沉到实例）
        self.roles = {s: create_role(self.players[s]["role"], s) for s in self.players}
        rs = data.get("role_skills")
        if rs is not None:
            for s_str, st in rs.items():
                s = int(s_str)
                if s in self.roles:
                    self.roles[s].load_skill_state(st)
        else:
            # 兼容 V6.x 旧存档：从老的 skills/guard_last 平铺结构回填到角色实例
            old = data.get("skills") or {}
            ow, og, ose, oh = (old.get("witch") or {}), (old.get("guard") or {}), \
                              (old.get("seer") or {}), (old.get("hunter") or {})
            for s, role in self.roles.items():
                if role.role_name == "女巫":
                    role.load_skill_state({"antidote": ow.get("antidote", True), "poison": ow.get("poison", True)})
                elif role.role_name == "守卫":
                    role.load_skill_state({"last": og.get("last", data.get("guard_last"))})
                elif role.role_name == "预言家":
                    role.load_skill_state({"checks": ose.get("checks", [])})
                elif role.role_name == "猎人":
                    role.load_skill_state({"can_shoot": oh.get("can_shoot", True), "shot": oh.get("shot")})
        self.night = {}
        self._last_resp = None
        self.election_state = int(data.get("election_state", 0))
        self._day_interrupted = False
        self.war_board = data.get("war_board")
        self.election_speeches = data.get("election_speeches") or []
        self.replay_logs = list(data.get("replay_logs", []) or [])
        self.objective_events = list(data.get("objective_events", []) or [])[-40:]
        self.public_claims = list(data.get("public_claims", []) or [])[-40:]
        self.confirmed_roles = {
            int(k): dict(v or {}) for k, v in (data.get("confirmed_roles") or {}).items()
        }
        self.badge_history = [dict(item) for item in (data.get("badge_history") or []) if isinstance(item, dict)]
        self.explosion_events = [
            dict(item) for item in (data.get("explosion_events") or []) if isinstance(item, dict)
        ]
        self.pending_wolf_night_seats = sorted({
            int(seat) for seat in (data.get("pending_wolf_night_seats") or [])
            if str(seat).isdigit() and int(seat) in self.players
        })
        pending_wolf_day = data.get("pending_wolf_night_day")
        self.pending_wolf_night_day = int(pending_wolf_day) if str(pending_wolf_day or "").isdigit() else None
        # 活跃阶段不跨存档恢复；生成器从下一夜边界重建，避免重复发放已开始的最后行动。
        self.active_last_stand_wolves = []
        # ★V6.5/6.6 变体引擎态恢复
        self.last_exiled_seat = data.get("last_exiled_seat")
        self.last_exiled_camp = data.get("last_exiled_camp")
        self.charm_target = data.get("charm_target")
        self._charm_sprung = bool(data.get("charm_sprung", False))
        _lv = data.get("lovers")
        self.lovers = (int(_lv[0]), int(_lv[1])) if _lv else None
        self._resolving_chain = False
        # A save made during the sheriff election cannot resume the Python generator
        # in-place. Settle pending night deaths before continuing at the next-night boundary,
        # and publish that settlement so public liveness cannot remain stale.
        _pend = data.get("pending_night_deaths") or {}
        pending_seats = sorted({int(k) for k in _pend if str(k).isdigit() and int(k) in self.players})
        if pending_seats:
            self._mark_dead_batch(pending_seats, "读档恢复待结算夜间死亡")
            for sk in pending_seats:
                self.published_alive[sk] = False
            dead_list = "、".join(f"{seat}号" for seat in pending_seats)
            self._log_judge(f"💾 读档恢复：存档发生在警长竞选期间，已结算并公布此前待结算的夜间死亡：{dead_list}。")
            self._broadcast_event(f"【读档恢复】因从存档的下一夜继续，系统已结算此前待公布的夜间死亡：{dead_list}。")
            if self.winner:
                self.phase = "游戏结束"
                if self.winner == "好人":
                    self._log_win("🏆 读档结算后，所有狼人已出局，【好人阵营】获胜！")
                elif self.winner == "情侣":
                    self._log_win("🏆 读档结算后，【情侣第三方】获胜！")
                else:
                    self._log_win("🏆 读档结算后，【狼人阵营】获胜！")
                self._add_replay(f"\n## 🏁 结果\n**🏆 {self.winner}阵营获胜！（读档恢复结算）**")
        self.pending_night_deaths = {}
        self._active_request_id = None
        self._active_compression_id = None
        self._skip_current = False
        self.fatal_error = ""
        self._resume = True   # ★关键：标记读档，run() 据此跳过开场、从下一夜续跑(详见 run())

    # ========================================================
    # 通用“问一个 AI”的子生成器：两次 yield 实现思考高亮
    def request_skip_current(self) -> bool:
        """请求跳过当前正在等待的 AI。真正的取消/兜底在 _ask 下一次轮询时完成。"""
        if not self._active_request_id:
            return False
        self._skip_current = True
        config.get_logger().warning(
            f"SKIP→ 请求跳过座位{self.current_speaker} request={self._active_request_id[:8]}"
        )
        self._record_event(
            "manual_skip_requested",
            seat=self.current_speaker,
            request_id=self._active_request_id,
        )
        return True

    def has_pending_action(self) -> bool:
        return bool(self._active_request_id)

    def submit_human_action(self, action_id, payload):
        """提交真人当前行动；只接受与当前待处理请求完全匹配的动作。"""
        from agent.model_protocol import parse_action

        pending = self.pending_human_action
        if pending is None:
            raise RuntimeError("当前没有等待中的真人行动")
        if action_id != pending["action_id"]:
            raise ValueError("行动请求已过期，请刷新页面后重试")
        if pending["seat"] != self.human_seat:
            raise ValueError("真人行动席位不匹配")
        if not isinstance(payload, dict):
            raise ValueError("行动内容必须是对象")
        response = parse_action(json.dumps(payload, ensure_ascii=False), pending["kind"], self._pending_human_context)
        response.setdefault("reasoning", "真人玩家提交")
        response["_human_action"] = True
        response["decision_source"] = "human"
        self._last_resp = response
        # 一旦成功提交立即消费请求令牌，阻止重复点击覆盖已提交动作。
        self.pending_human_action = None
        self._pending_human_context = None
        return True

    # ========================================================
    # 通用“问一个 AI”的子生成器：Future 轮询 + 多次 yield。
    # 每次 next() 都会快速返回；API 在后台执行，Streamlit 主线程不再长时间阻塞。
    # ========================================================
    def _action_context(self, seat, kind, allowed_targets=None):
        alive = set(self.alive_seats())
        role = self.roles[seat]
        context = {"allowed_flags": [], "skill_targets": sorted(alive - {seat}),
                   "explosion_requires_target": role.role_name == "白狼王"}
        daytime = "夜" not in self.phase and (self.day > 0)
        if daytime and seat in alive:
            if role.camp == "狼人" and role.role_name not in {"恶灵骑士", "狼美人"}:
                context["allowed_flags"].append("is_explode")
            if role.role_name == "骑士" and not getattr(role, "has_dueled", False):
                context["allowed_flags"].append("is_duel")
        targets = None
        if kind in {"check", "poison", "hunt", "charm", "dream", "badge_target"}:
            targets = alive - {seat}
        if kind in {"poison", "hunt", "badge_target"}:
            targets.add(0)
        if kind == "charm":
            targets.discard(getattr(role, "charm_target", None))
        if kind == "guard":
            targets = (alive - {getattr(role, "last_guarded", None)}) | {0}
        if kind in {"link", "swap"}:
            targets = alive
        if kind in {"vote", "consolidation"}:
            targets = alive if "夜" in self.phase else alive - {seat}
            targets = targets | {0}
        if allowed_targets is not None:
            targets = set(allowed_targets)
        if targets is not None:
            context["allowed_targets"] = sorted(targets)
        return context

    def _public_evidence(self):
        # 公共日志中 role 是全知字段，严禁传入玩家记忆。
        return [{"id": i + 1, "phase": row.get("phase", ""),
                 "seat": row["seat"], "text": row.get("text", "")}
                for i, row in enumerate(self.public_log) if row.get("kind") == "player"]

    @staticmethod
    def _decision_source(resp, target=None, legal=None):
        source = resp.get("decision_source")
        if resp.get("_is_fallback"):
            return source if source == "manual_skip" else "technical_fallback"
        if legal is not None and target not in legal:
            return "invalid_target"
        return source or "model"

    @staticmethod
    def _vote_detail(detail, sources):
        for seat, source in sources.items():
            if source != "model":
                reason = {"technical_fallback": "技术性未提交", "manual_skip": "人工跳过",
                          "invalid_target": "无效目标", "human": "真人玩家主动弃票"}.get(source, "未提交")
                detail = re.sub(r"(?<!\d)" + re.escape(f"{seat}号->弃票"),
                                f"{seat}号->{reason}（不代表主动弃票）", detail)
        return detail

    @staticmethod
    def _action_display(kind, response):
        if response.get("_is_fallback"):
            return "【人工跳过】" if response.get("decision_source") == "manual_skip" else "【技术性未提交】"
        if kind in {"check", "guard", "poison", "hunt", "charm", "dream"}:
            target = response.get("vote_target", 0)
            verb = {"check": "查验", "guard": "守护", "poison": "毒", "hunt": "开枪", "charm": "魅惑", "dream": "摄梦"}[kind]
            return f"{verb}{target}号" if target else f"不执行{verb}"
        if kind == "antidote":
            return "使用解药" if response.get("use_antidote") else "不使用解药"
        if kind in {"link", "swap"}:
            return ("连线" if kind == "link" else "交换") + "、".join(f"{s}号" for s in response.get("targets", []))
        return response.get("speech", "")

    def _ask(self, seat: int, prompt: str, label: str, public: bool = False, response_kind: str = "speech", allowed_targets=None):
        self.current_speaker = seat
        self.public_speaker = seat if public else None
        self._skip_current = False
        action_context = self._action_context(seat, response_kind, allowed_targets)
        if seat == self.human_seat:
            if self.pending_human_action is not None:
                raise RuntimeError("引擎已存在未完成的真人行动")
            request_id = f"human:{uuid.uuid4().hex}"
            self._last_resp = None
            self.pending_human_action = {
                "action_id": request_id, "seat": seat, "kind": response_kind,
                "label": label, "phase": self.phase, "day": self.day, "prompt": prompt,
                "public": bool(public), "allowed_targets": action_context.get("allowed_targets"),
                "allowed_flags": action_context.get("allowed_flags", []),
            }
            self._pending_human_context = action_context
            yield self._ev("human_input_required", f"等待真人玩家：{label}")
            while self._last_resp is None:
                yield self._ev("human_waiting", f"等待真人玩家：{label}")
            resp = self._last_resp
            self._last_resp = None
            self.pending_human_action = None
            self._pending_human_context = None
        else:
            # 第二道防线：真人座位不能进入任何 AI 请求路径。
            if self.human_seat is not None and seat == self.human_seat:
                raise RuntimeError(f"安全保护：拒绝为真人席位 {seat:02d} 调用 AI。")
            self.manager.set_hard_facts(seat, self._hard_facts_for(seat))
            if hasattr(self.manager, "set_action_context"):
                self.manager.set_action_context(seat, action_context)
            if hasattr(self.manager, "set_public_evidence"):
                self.manager.set_public_evidence(seat, self._public_evidence())
            config.get_logger().info(f"ASK [{label}] 座位{seat}({self.players[seat]['role']}) kind={response_kind}")
            request_id = self.manager.submit_ai_response(seat, prompt, response_kind=response_kind)
            self._active_request_id = request_id
            yield self._ev("turn", f"{seat}号正在思考…")

            while True:
                if self._skip_current:
                    resp = self.manager.cancel_ai_response(request_id, "上帝跳过当前玩家")
                    break
                done, resp = self.manager.poll_ai_response(request_id)
                if done:
                    break
                yield self._ev("waiting", f"{seat}号正在思考…（可点击跳过当前玩家）")

            self._active_request_id = None
            self._skip_current = False
            self._last_resp = resp
        self._last_resp = resp
        if resp.get("_is_fallback") and public:
            resp["speech"] = "【技术性未提交发言】" if resp.get("decision_source") != "manual_skip" else "【人工跳过发言】"
        fatal = resp.get("_fatal_error", "")
        if fatal:
            self.fatal_error = fatal
            self._log_judge(f"⛔ API 已暂停：{fatal}。处理后点击继续可从当前流程恢复。")

        if response_kind == "vote":
            vt = resp.get("vote_target", 0)
            display = "（暗投：弃票/空刀）" if not vt else f"（暗投：{vt}号）"
            if resp.get("_is_fallback"):
                display = "（暗投：" + self._action_display(response_kind, resp) + "）"
            self._record_reasoning(
                seat, label, resp.get("reasoning", ""), display,
                response=resp, request_id=request_id,
            )
        else:
            self._record_reasoning(
                seat, label, resp.get("reasoning", ""), self._action_display(response_kind, resp),
                response=resp, request_id=request_id,
            )
            if public:
                if resp.get("_is_fallback"):
                    self._broadcast_event(f"【行动状态】{seat}号{resp['speech']}，不能作为其主动沉默或阵营证据。")
                else:
                    self._log_player(seat, resp["speech"])
        self.public_speaker = None
        yield self._ev("done", f"{seat}号行动完毕")

    # ========================================================
    def check_winner(self):
        alive = self.alive_seats()
        # ★V6.6.1 情侣第三方：跨阵营情侣在场时，三方“剩者为王”——只有情侣方独活才算情侣胜，
        #   否则未定(None)；好人/狼人需先把这对不可分割的情侣清掉(致其双死、第三方瓦解)才能按屠边分胜负。
        tp = self._third_party_set()
        if tp:
            return "情侣" if all(s in tp for s in alive) else None
        # 无第三方：标准屠边。按 camp 计狼(白狼王/狼美人 camp=狼人)；神/民按身份计。
        wolves = sum(1 for s in alive if self.roles[s].camp == "狼人")
        gods = sum(1 for s in alive if self.roles[s].is_god)
        villagers = sum(1 for s in alive if self.players[s]["role"] == "平民")
        if wolves == 0:
            return "好人"
        if gods == 0 or villagers == 0:
            return "狼人"
        return None

    # ========================================================
    # 夜晚结算
    # ========================================================
    def settle_night(self) -> dict:
        deaths = {}
        kill = self.night.get("wolf_kill")
        guard = self.night.get("guard_target")
        save = self.night.get("witch_save")
        poison = self.night.get("witch_poison")
        dream_target = self.night.get("dream_target")
        seer_check = self.night.get("seer_check")

        # —— 魔术师号码牌移位 ——
        swap = self.night.get("magician_swap")
        if swap:
            def _map(t):
                if t == swap[0]: return swap[1]
                if t == swap[1]: return swap[0]
                return t
            kill = _map(kill) if kill else kill
            guard = _map(guard) if guard else guard
            save = _map(save) if save else save
            poison = _map(poison) if poison else poison
            dream_target = _map(dream_target) if dream_target else dream_target

        # —— 恶灵骑士反伤与免疫夜死 ——
        gr_seat = self._seat_with_role("恶灵骑士")
        gr_role = self.roles.get(gr_seat) if gr_seat else None
        if gr_seat and gr_role:
            # 恶灵骑士免于被狼刀杀死
            if kill == gr_seat:
                kill = None
            # 预言家查验恶灵骑士 → 反伤预言家
            if seer_check and seer_check[0] == gr_seat and not gr_role.has_reflected:
                seer_seat = self._seat_with_role("预言家")
                if seer_seat and self.players.get(seer_seat, {}).get("alive"):
                    deaths[seer_seat] = "reflect"
                    gr_role.has_reflected = True
                    self._log_god(f"💀 恶灵骑士（{gr_seat}号）反伤预言家（{seer_seat}号）！预言家今夜暴毙！")
                    self._add_replay(f"- 💀 恶灵骑士反伤：预言家({seer_seat}号)查验恶灵骑士遭反伤暴毙")
            # 女巫毒杀恶灵骑士 → 恶灵骑士免疫毒杀，反伤女巫
            if poison == gr_seat:
                poison = None
                if not gr_role.has_reflected:
                    witch_seat = self._seat_with_role("女巫")
                    if witch_seat and self.players.get(witch_seat, {}).get("alive"):
                        deaths[witch_seat] = "reflect"
                        gr_role.has_reflected = True
                        self._log_god(f"💀 恶灵骑士（{gr_seat}号）反伤女巫（{witch_seat}号）！女巫今夜暴毙！")
                        self._add_replay(f"- 💀 恶灵骑士反伤：女巫({witch_seat}号)毒杀恶灵骑士遭反伤暴毙")

        # —— 狼刀结算 (同守同救 / 守卫 / 女巫解药) ——
        if kill is not None:
            guarded = (guard == kill)
            saved = (save == kill)
            if guarded and saved:
                deaths[kill] = "wolf"     # 同守同救 → 死
            elif guarded or saved:
                pass
            else:
                deaths[kill] = "wolf"

        # —— 女巫毒杀结算 ——
        if poison is not None:
            deaths[poison] = "poison"

        # —— 摄梦人守护、连摄暴毙与殉情结算 ——
        dreamer_seat = self._seat_with_role("摄梦人")
        dreamer_role = self.roles.get(dreamer_seat) if dreamer_seat else None
        if dreamer_seat and dreamer_role and dream_target:
            # 摄梦人守护：免疫当晚狼刀
            if kill == dream_target and dream_target in deaths and deaths[dream_target] == "wolf":
                del deaths[dream_target]
            # 连续两晚摄同一人 → 目标暴毙
            if len(dreamer_role.dream_history) >= 2:
                if (dreamer_role.dream_history[-1] == dream_target
                        and dreamer_role.dream_history[-2] == dream_target):
                    deaths[dream_target] = "dream_overdose"
                    self._log_god(f"🌙 摄梦人连续两晚摄梦 {dream_target}号，{dream_target}号梦中暴毙！")
                    self._add_replay(f"- 🌙 摄梦暴毙：连续两晚摄梦 {dream_target}号，目标死亡")
            # 摄梦人夜间死亡 → 梦游者随之殉情
            if dreamer_seat in deaths and dream_target in self.alive_seats() and dream_target not in deaths:
                deaths[dream_target] = "dream_link"
                self._log_god(f"🌙 摄梦人（{dreamer_seat}号）死亡，梦游者 {dream_target}号 连带出局！")
                self._add_replay(f"- 🌙 摄梦连带：摄梦人({dreamer_seat}号)夜死，梦游者({dream_target}号)一同出局")

        return deaths

    # ========================================================
    # 狼队落刀裁决：0=空刀参与竞争；最高票，平票随机；最高是0→无人被刀
    # ========================================================
    def _decide_kill_votes(self, kill_votes: dict):
        if not kill_votes:
            return None
        counter = Counter(kill_votes.values())
        top = counter.most_common()
        leaders = [t for t, c in top if c == top[0][1]]
        choice = self.rng.choice(leaders)
        return None if choice == 0 else choice

    # ========================================================
    # 加权计票：警长票×1.5；返回 (出局者or None, 完整票型文字, 各目标权重和)
    # ========================================================
    @staticmethod
    def _fmt_w(w) -> str:
        return f"{w:g}"

    def _weighted_tally(self, votes: dict, win_word="被放逐", none_word="无人被放逐"):
        """加权计票。win_word/none_word 用于区分场景：放逐(被放逐) vs 警长竞选(当选警长)。"""
        totals = {}
        parts = []
        for voter, target in votes.items():
            w = 1.5 if (self.sheriff is not None and voter == self.sheriff) else 1.0
            if target:
                totals[target] = totals.get(target, 0.0) + w
                parts.append(f"{voter}号投->{target}号({self._fmt_w(w)}票)")
            else:
                parts.append(f"{voter}号->弃票")
        body = "，".join(parts) if parts else "（无人投票）"
        if not totals:
            return None, body + f"。{none_word}。", totals
        maxw = max(totals.values())
        leaders = [t for t, wt in totals.items() if wt == maxw]
        if len(leaders) == 1:
            out = leaders[0]
            return out, body + f"。最终{out}号以 {self._fmt_w(maxw)} 票{win_word}。", totals
        tie = "、".join(f"{x}号" for x in leaders)
        return None, body + f"。平票（{tie} 各{self._fmt_w(maxw)}票），{none_word}。", totals

    # ========================================================
    # 猎人开枪（子生成器）
    # ========================================================
    # ========================================================
    # ★V6.3 角色行动驱动器（所有 yield 都在引擎；角色方法纯净无 yield）
    # ========================================================
    def _run_solo_night(self, role):
        """单座位夜间行动通用驱动：反复 pull 角色子步骤(女巫最多2步/守卫·预言家1步)，每步引擎执行 _ask。"""
        if role is None:
            return
        seat = role.seat
        if not (self.players[seat]["alive"] and role.has_night_action(self)):
            return
        role.reset_night()
        while True:
            step = role.get_night_prompt(self)
            if step is None:
                return
            prompt, kind, label = step
            yield from self._ask(seat, prompt, label, response_kind=kind)
            role.parse_night_response(self, self._last_resp)
            if seat == self.human_seat:
                if kind == "check" and self.night.get("seer_check"):
                    target, result = self.night["seer_check"]
                    self.human_private_messages.append(f"预言家查验结果：{target}号是【{result}】。")
                elif kind == "guard":
                    target = self.night.get("guard_target")
                    self.human_private_messages.append(f"守卫行动已记录：{'守护' + str(target) + '号' if target else '本夜空守'}。")
                elif kind == "antidote":
                    self.human_private_messages.append("女巫解药选择已记录；请以当前技能状态为准。")
                elif kind == "poison":
                    target = self.night.get("witch_poison")
                    self.human_private_messages.append(f"女巫毒药选择已记录：{'毒杀' + str(target) + '号' if target else '本夜不使用毒药'}。")

    def _run_death_action(self, role):
        """临终技能事件回调(猎人开枪)：★V6.7+ 私下询问——【不再公开预告身份】(原"X号猎人倒下"会白送身份)，
        仅上帝视角可见；只有【真的发动技能(开枪)】时才由角色自己公开。引擎问→角色结算→统一熔断判胜负。"""
        self._log_god(f"🔫 {role.seat}号({role.role_name})倒下，正在【私下】决定是否发动临终技能……")
        prompt, kind, label = role.get_death_prompt(self)
        yield from self._ask(role.seat, prompt, label, public=False, response_kind=kind)
        role.parse_death_response(self, self._last_resp)
        self.winner = self.check_winner()   # ★熔断：临终技能可能带走人，动作后立刻判胜负
        if not self.winner and not self._resolving_chain:
            # 刚开枪带走的人可能是【情侣】或【狼美人】→ 触发连环死亡结算(重入哨兵防互递归)
            yield from self._resolve_chain_deaths()

    # ========================================================
    # ★V6.6 连环死亡总结算：统一处理【丘比特·情侣双死】+【狼美人·殉情】，支持级联、幂等收敛。
    #   _link_propagate() 纯数据补齐连带死亡；_resolve_chain_deaths() 循环结算+开枪/移交；
    #   _resolving_chain 哨兵防止它与 _run_death_action 互递归。每处 alive=False 紧跟 check_winner()。
    # ========================================================
    def _link_propagate(self):
        """把【已死者】触发的连带死亡(情侣/魅惑)一次性 set alive=False，返回新死 [(座位,死因)]。幂等。"""
        newly = []
        if self.lovers:
            a, b = self.lovers
            if not self.players[a]["alive"] and self.players[b]["alive"]:
                self._mark_dead(b, "情侣殉情", check=False)
                newly.append((b, "情侣殉情"))
            elif not self.players[b]["alive"] and self.players[a]["alive"]:
                self._mark_dead(a, "情侣殉情", check=False)
                newly.append((a, "情侣殉情"))
        wb = self._wolfbeauty_seat()
        if wb is not None and not self.players[wb]["alive"] and not self._charm_sprung:
            self._charm_sprung = True
            t = self.charm_target
            if t and self.players[t]["alive"]:
                self._mark_dead(t, "狼美人殉情", check=False)
                newly.append((t, "狼美人殉情"))
        return newly

    def _resolve_chain_deaths(self):
        if self._resolving_chain:
            return                                  # 重入哨兵：连环里的猎人开枪不再嵌套触发本方法
        self._resolving_chain = True
        try:
            guard = 0
            while not self.winner and guard < 24:   # 死亡数≤12，24 步必收敛；防御性上限
                guard += 1
                newly = self._link_propagate()
                if not newly:
                    break
                self.winner = self.check_winner()   # ★熔断：本批连带死后立刻判胜负
                for seat, why in newly:
                    if why == "情侣殉情":
                        self._log_judge(f"💔 情侣 {seat}号 殉情，随爱人而去！")
                    else:
                        self._log_judge(f"💔 狼美人死亡，触发魅惑，{seat}号殉情陪葬！")
                    self._broadcast_event(f"【连环死亡】{seat}号 {why} 随之出局。")
                    self._add_replay(f"- 💔 连环死亡：{seat}号({self.players[seat]['role']}) {why}")
                if self.winner:
                    return
                for seat, _why in newly:             # 新死者里的猎人 → 临终开枪(其后循环再补传播)
                    r = self.roles[seat]
                    if r.has_death_action(self, "chain"):
                        yield from self._run_death_action(r)
                        if self.winner:
                            return
                yield from self._resolve_sheriff_death()   # 新死者里若有警长 → 移交
        finally:
            self._resolving_chain = False

    def _augment_chain_deaths(self, deaths):
        """夜间结算钩子：把夜死者触发的连带死亡(情侣/魅惑)并入 deaths(夜间殉情)，让晨间播报与
        ‘猎人夜死开枪’走原有流程；循环到稳定以处理级联。幂等。"""
        changed = True
        while changed:
            changed = False
            if self.lovers:
                a, b = self.lovers
                if a in deaths and b not in deaths and self.players[b]["alive"]:
                    deaths[b] = "lover"; changed = True
                    self._add_replay(f"- 💔 连环死亡(夜间)：{b}号 情侣殉情")
                if b in deaths and a not in deaths and self.players[a]["alive"]:
                    deaths[a] = "lover"; changed = True
                    self._add_replay(f"- 💔 连环死亡(夜间)：{a}号 情侣殉情")
            wb = self._wolfbeauty_seat()
            if wb is not None and wb in deaths and not self._charm_sprung:
                self._charm_sprung = True
                t = self.charm_target
                if t and self.players[t]["alive"] and t not in deaths:
                    deaths[t] = "charm"; changed = True
                    self._add_replay(f"- 💔 连环死亡(夜间)：{t}号 狼美人殉情")
        return deaths

    def _run_cupid_link(self):
        """★V6.6 开局连线(第0夜)：丘比特连定一对情侣并私下告知双方。仅一次、仅在有丘比特时。
        (yield 仍在引擎；丘比特角色只造 prompt/解析，不含 yield。)"""
        cupid = self._solo("丘比特")
        if cupid is None or self.lovers is not None:
            return
        self._log_judge("💘 丘比特睁眼，将连定一对情侣……")
        yield self._ev("cupid", "丘比特连线")
        prompt = cupid.get_link_prompt(self)
        yield from self._ask(cupid.seat, prompt, "D0 丘比特连线", response_kind="link")   # public=False：连线静默进行
        cupid.parse_link_response(self, self._last_resp)            # 置 self.lovers + 私下告知双方 + 复盘

    def _run_wolf_pack_night(self, label):
        """狼队集体夜杀；白天自爆者可按本项目规则参加下一夜最后一次讨论与落刀。"""
        living_wolves = [w for w in self.wolves if self.players[w]["alive"]]
        last_stand_wolves = []
        if self.pending_wolf_night_day == self.day:
            last_stand_wolves = [
                w for w in self.pending_wolf_night_seats
                if w in self.wolves and w in self.players and not self.players[w]["alive"]
            ]
        # 进入狼队阶段即消费“下一夜”令牌；存档恢复从下一夜开始，不会重复发放这次最后行动。
        if self.pending_wolf_night_day is not None and self.pending_wolf_night_day <= self.day:
            self.pending_wolf_night_seats = []
            self.pending_wolf_night_day = None
        self.active_last_stand_wolves = list(last_stand_wolves)
        wolf_participants = sorted(set(living_wolves + last_stand_wolves))
        wolf_talk = []
        self.current_wolf_talk = []
        # 不让上一夜的作战板在本夜讨论期间被误认为当前讨论。
        self.war_board = None
        for w in wolf_participants:
            mates = [x for x in living_wolves if x != w]
            last_stand_mates = [x for x in last_stand_wolves if x != w]
            prev_text = "\n".join(f"{x}号狼：{t}" for x, t in wolf_talk) or "（你是第一个发言的狼）"
            if w in last_stand_wolves:
                status_note = ("你已因白天自爆出局；按本局规则，你可以参加本夜最后一次狼人讨论与落刀。"
                               "本次行动结束后你彻底退出，不计入存活人数，也不再参与后续夜晚。")
            else:
                status_note = (f"本夜可参与最后行动的已自爆狼队友：{last_stand_mates}。"
                               "这些队友已出局，只能参与本夜讨论与落刀，不计入存活人数。") if last_stand_mates else ""
            prompt = (f"【{self.phase}·狼人战术讨论】{status_note}你的存活狼队友：{mates}。当前存活：{self.alive_str()}。\n"
                      f"狼队此前发言：\n{prev_text}\n"
                      "不要只重复战术名。先检查事实账本中的金水/查验声明和警徽链；若存在与机械确认狼有关的关系，挑最相关一条，"
                      "比较至少两种解释，并说明下一轮如何通过公开发言、站边或票型验证；没有相关事实时不要硬编。"
                      "再明确明天是否安排主悍跳/代跳、深水倒钩、压身份/抗推与归票/传话分工；可以选择不使用某项并说明理由。"
                      "是否考虑自刀需比较实际收益与失去一名狼的代价，不能为了观赏性机械自刀。"
                      f"原则：稳健、以胜利为目标、队友不必机械同边。\n{WOLF_KILL_TACTICS}\n"
                      f"speech输出简短可执行的战术建议(60-140字)。")
            yield from self._ask(w, prompt, f"{label} 狼人讨论")
            wolf_talk.append((w, self._last_resp["speech"]))
            self.current_wolf_talk = list(wolf_talk)
            self._log_god(f"🐺 狼人（{w}号）战术：{self._last_resp['speech']}")
        if wolf_talk:
            discuss = "；".join(f"{x}号狼:{t}" for x, t in wolf_talk)
            for w in wolf_participants:
                self.manager.inject(w, f"【狼队第{self.day}夜讨论纪要】{discuss}")
        else:
            discuss = "（无）"
        kill_votes = {}
        for w in wolf_participants:
            final_note = ("这是你最后一次落刀决策；你已出局，不能把自己视为存活目标。" if w in last_stand_wolves else "")
            prompt = (f"【{self.phase}·狼人落刀·盲投】讨论结束，请暗中决定最终刀人目标。{final_note}\n狼队讨论：\n{discuss}\n"
                      f"{WOLF_KILL_TACTICS}\n"
                      f"当前存活：{self.alive_str()}。只输出严格JSON："
                      f'{{"reasoning":"落刀理由","vote_target":X}}，X为要刀的座位号，空刀填0；'
                      f"如执行【自刀】策略可填某个狼队友的座位号(会损失一名狼)。"
                      f"💡偶尔大胆【自刀】(刀掉自己队友)能制造‘悲情金水/真神’假象、骗取好人深度信任，"
                      f"是高风险但极出彩、提升观赏性的操作，局势合适时不妨一试。")
            yield from self._ask(w, prompt, f"{label} 狼人落刀", response_kind="vote")
            # 自爆者提交最后一次落刀后立即失去狼队私密视角；身份此时对其开放。
            if w in last_stand_wolves and w in self.active_last_stand_wolves:
                self.active_last_stand_wolves.remove(w)
            vt = self._last_resp.get("vote_target", 0)
            kill_votes[w] = vt if vt in self.alive_seats() else 0   # 最后行动狼同样可贡献一票，但只能刀当前存活目标
            self._log_god(f"🐺 狼人（{w}号）落刀 → {'空刀' if not kill_votes[w] else str(kill_votes[w]) + '号'}")
        wolf_kill = self._decide_kill_votes(kill_votes)
        self.night["wolf_kill"] = wolf_kill
        if wolf_kill is None:
            note = "空刀（今夜无人被刀）"
            self._log_god("🐺 狼队本晚【空刀】，无人被刀。")
        elif wolf_kill in self.wolves:
            note = f"自刀 {wolf_kill}号（骗取好人信任）"
            for w in wolf_participants:
                self.manager.inject(w, f"【狼队决议】今晚狼队执行【自刀】，刀了狼队友 {wolf_kill}号。")
            self._log_god(f"🐺 狼队最终目标：【自刀】{wolf_kill}号！")
        else:
            note = f"刀 {wolf_kill}号"
            for w in wolf_participants:
                self.manager.inject(w, f"【狼队决议】今晚狼队最终对 {wolf_kill}号 出刀。")
            self._log_god(f"🐺 狼队最终目标：{wolf_kill}号")
        # 写入“夜间作战板”，供上帝视角展示狼队分工与决定
        self.war_board_recipients = list(wolf_participants)
        self.war_board = {"day": self.day, "discussion": list(wolf_talk), "kill": wolf_kill, "note": note}
        # 最后行动已完成：自爆者不再拥有狼人私密视角，死后身份可正常开放。
        self.active_last_stand_wolves = []

    # ========================================================
    # 遗言（子生成器）
    # ========================================================
    def _last_words(self, seat: int):
        badge_warning = ""
        if self.sheriff != seat:
            badge_warning = ("⚠️【最高警告】：你目前【没有警徽】！绝不能在遗言中编造“撕警徽”、“交警徽”等词汇"
                             "给自己加戏，必须绝对尊重客观事实！\n")
        prompt = (self._time_anchor()
                  + f"【遗言】你（{seat}号，身份：{self.players[seat]['role']}）已经出局。\n"
                  + badge_warning
                  + f"请【仅基于本局真实发生过的事】发表遗言（至今只过了 {self.day} 个夜晚，不要编造多夜虚假行动）。"
                  + "可公布身份、传递真实信息、指认狼人或留遗嘱。在speech输出遗言(100-200字)。")
        yield from self._ask(seat, prompt, f"D{self.day} 遗言", public=True)
        # —— 复盘：记录遗言摘要(speech 前 40 字)——
        _sp = (self._last_resp.get("speech") or "").strip()
        self._add_replay(f"- 💬 {seat}号({self.players[seat]['role']})遗言：{_sp[:40]}{'…' if len(_sp) > 40 else ''}")

    # ========================================================
    # 时间锚点：强力抑制“凭空编造夜晚/技能记录”幻觉
    # ========================================================
    def _time_anchor(self) -> str:
        sheriff_line = f"当前警长：{self.sheriff}号(票×1.5)。" if self.sheriff else "当前无警长。"
        return (f"【事实锚点·最高优先】现在是「{self.phase}」，本局至今【总共只经历了 {self.day} 个夜晚】"
                f"（第1夜~第{self.day}夜）。{sheriff_line}严禁编造尚未发生的夜晚、不存在的查验/守护/用药记录或未来剧情。\n")

    # ========================================================
    # 发言起始与方向：模拟“死左/死右”，避免每天都 1→12
    # ========================================================
    def _decide_speech_order(self, deaths):
        """如果警长存活，由警长【私下】极简选择发言顺序（死左/死右 或 警左/警右）；否则由系统自动计算。"""
        sheriff_alive = self.sheriff is not None and self.players.get(self.sheriff, {}).get("alive")
        dead_sorted = sorted(deaths)
        if not sheriff_alive:
            return self._compute_speech_order(deaths)

        sh = self.sheriff
        if not dead_sorted:
            opts = "【警左】(从你左手边顺时针开始，你压轴) 或 【警右】(从你右手边逆时针开始，你压轴)"
            prompt = (
                self._time_anchor()
                + f"【D{self.day}·警长定序】你是现任警长（{sh}号）。昨夜是平安夜。\n"
                + f"请决定今日发言顺序起点与方向：{opts}。\n"
                + "只输出严格JSON：{\"reasoning\":\"战术考量(想先听谁发言)\",\"speech\":\"警左或警右\"}。"
            )
        else:
            first_dead = dead_sorted[0]
            opts = f"【死左】(从首位死者{first_dead}号左侧顺时针开始) 或 【死右】(从{first_dead}号右侧逆时针开始) 或 【警左】/【警右】"
            prompt = (
                self._time_anchor()
                + f"【D{self.day}·警长定序】你是现任警长（{sh}号）。昨夜倒牌：{', '.join(f'{s}号' for s in dead_sorted)}。\n"
                + f"请决定今日发言顺序起点与方向：{opts}。\n"
                + "只输出严格JSON：{\"reasoning\":\"战术考量\",\"speech\":\"死左或死右或警左或警右\"}。"
            )

        yield from self._ask(sh, prompt, f"D{self.day} 警长定序", public=False, response_kind="order")
        resp_speech = self._last_resp.get("speech", "")

        direction = 1  # 默认顺时针(左)
        if "右" in resp_speech:
            direction = -1

        is_police_based = ("警" in resp_speech) or (not dead_sorted)
        if is_police_based:
            dir_cn = "警右(逆时针)" if direction == -1 else "警左(顺时针)"
            seq = [((sh - 1 + direction * k) % 12) + 1 for k in range(1, 13)]
            order = [s for s in seq if self.players[s]["alive"]]
            reason = f"由警长 {sh}号 指定从【{dir_cn}】起轮流发言，警长压轴收尾"
        else:
            anchor = dead_sorted[0]
            dir_cn = "死右(逆时针)" if direction == -1 else "死左(顺时针)"
            seq = [((anchor - 1 + direction * k) % 12) + 1 for k in range(1, 13)]
            order = [s for s in seq if self.players[s]["alive"]]
            reason = f"由警长 {sh}号 指定从首位死者{anchor}号【{dir_cn}】开始发言"

        self._log_god(f"🎖️ 警长（{sh}号）定序：{reason}")
        return order, reason

    def _compute_speech_order(self, deaths):
        alive = self.alive_seats()
        if not alive:
            return [], "无人存活"
        dead_sorted = sorted(deaths)
        sheriff_alive = self.sheriff is not None and self.players.get(self.sheriff, {}).get("alive")

        # ★ 平安夜(昨夜无人死亡) + 有警长存活：由警长定序——随机【警左/警右】开局，且警长【压轴最后发言】。
        #   引擎层直接写死(随机选方向)，不额外调 API 问警长，省时省请求。
        #   原理：从警长出发沿某方向绕满 12 步，第 12 步正好回到警长本人 → 警长天然排在序列末尾。
        if len(dead_sorted) == 0 and sheriff_alive:
            direction = self.rng.choice([1, -1])
            dir_cn = "警右(座位号增大方向)" if direction == 1 else "警左(座位号减小方向)"
            seq = [((self.sheriff - 1 + direction * k) % 12) + 1 for k in range(1, 13)]  # k=12→警长本人，压轴
            order = [s for s in seq if self.players[s]["alive"]]
            reason = f"平安夜，由警长 {self.sheriff}号 定序：从{dir_cn}起轮流发言，警长压轴收尾"
            return order, reason

        direction = self.rng.choice([1, -1])
        dir_cn = "死右(向座位号增大方向)" if direction == 1 else "死左(向座位号减小方向)"
        if len(dead_sorted) == 1:
            anchor = dead_sorted[0]
            reason = f"{anchor}号倒牌，{dir_cn}"
        elif len(dead_sorted) == 0:
            anchor = self.rng.choice(alive)
            reason = f"平安夜无人倒牌，随机从{anchor}号的{'右' if direction == 1 else '左'}侧、{dir_cn}"
        else:
            anchor = dead_sorted[0]
            reason = f"多人倒牌，从首位{anchor}号、{dir_cn}"
        seq = [((anchor - 1 + direction * k) % 12) + 1 for k in range(1, 13)]
        order = [s for s in seq if self.players[s]["alive"]]
        return order, reason

    def _position_context(self, seat, order):
        """按当天真实发言顺序解释位置术语；位置不是固定座位号属性。"""
        order = list(order or [])
        if seat not in order:
            return "【位置视角】你不在本轮常规发言序列中。"
        idx = order.index(seat)
        total = len(order)
        if idx == 0:
            label = "首置位"
        elif idx == total - 1:
            label = "末置位"
        elif idx < max(2, total // 3):
            label = "前置位"
        elif idx >= max(2, (total * 2) // 3):
            label = "后置位"
        else:
            label = "中置位"
        heard = "、".join(f"{s}号" for s in order[:idx]) or "无"
        pending = "、".join(f"{s}号" for s in order[idx + 1:]) or "无"
        sequence = "→".join(f"{s}号" for s in order)
        return (
            f"【位置视角】今日完整发言顺序：{sequence}。你是第{idx + 1}/{total}位，属于【{label}】；"
            f"你之前已发言：{heard}；你之后尚未发言：{pending}。"
            "‘前置位/后置位’只按今日顺序判断；‘外置位’指当前预言家对跳、PK或主要矛盾之外的玩家，"
            "与座位号远近无关，也不代表天然好人或狼人。"
        )

    # ========================================================
    # 自爆判定 & 处理
    # ========================================================
    def _explode_requested(self, seat) -> bool:
        """自爆是规则动作，只接受结构化字段，绝不从自然语言台词猜测。狼美人无自爆权。"""
        role_name = getattr(self.roles[seat], "role_name", "")
        return (
            self.roles[seat].camp == "狼人"
            and role_name not in {"狼美人", "恶灵骑士"}
            and bool((self._last_resp or {}).get("is_explode", False))
        )

    def _record_explosion(self, seat: int, context: str, target: int = 0) -> None:
        """记录真实执行的自爆。幂等键防止生成器恢复或重复归档造成重复计数。"""
        event = {
            "day": int(self.day),
            "seat": int(seat),
            "role": self.players[seat]["role"],
            "context": str(context),
            "target": int(target or 0),
        }
        key = (event["day"], event["seat"], event["context"], event["target"])
        if any(
            (int(old.get("day", 0)), int(old.get("seat", 0)), str(old.get("context", "")),
             int(old.get("target", 0) or 0)) == key
            for old in self.explosion_events
        ):
            return
        self.explosion_events.append(event)

    def _schedule_last_stand_wolf(self, seat: int) -> None:
        """安排白天自爆狼在下一夜参与最后一次狼队讨论与落刀。"""
        if self.winner or seat not in self.players or self.roles[seat].camp != "狼人":
            return
        target_day = self.day + 1
        if self.pending_wolf_night_day != target_day:
            self.pending_wolf_night_seats = []
            self.pending_wolf_night_day = target_day
        if seat not in self.pending_wolf_night_seats:
            self.pending_wolf_night_seats.append(seat)

    def _handle_election_explode(self, seat, entry_resumed):
        """竞选期间自爆（子生成器）：顺延 或 双爆吞警徽；若自爆者是狼美人则触发殉情连环。"""
        self._mark_dead(seat, "警长竞选自爆")
        if not self.winner:
            self._schedule_last_stand_wolf(seat)
        self._confirm_role(seat, self.players[seat]["role"], "公开自爆")
        self._record_explosion(
            seat,
            "election_resumed" if entry_resumed else "election",
        )
        self._record_event(
            "player_exploded",
            seat=seat,
            role=self.players[seat]["role"],
            context="election_resumed" if entry_resumed else "election",
        )
        self._day_interrupted = True
        if entry_resumed:
            self.election_state = 4
            self._log_judge(f"💥 {seat}号狼人自爆！双爆吞警徽！本局不再产生警长！")
            self._broadcast_event(f"【双爆吞警徽】{seat}号狼人在复选中自爆，警徽销毁、本局不再有警长，白天结束直接入夜。")
            self._add_replay(f"- 💥 狼人自爆：{seat}号在复选中自爆，双爆吞警徽，白天强制结束")
        else:
            self.election_state = 2
            self._log_judge(f"💥 {seat}号狼人自爆！警长竞选顺延至明日！")
            self._broadcast_event(f"【自爆顺延】{seat}号狼人自爆，警长竞选顺延至明日，白天结束直接入夜。")
            self._add_replay(f"- 💥 狼人自爆：{seat}号自爆，警长竞选顺延，白天强制结束")
        if not self.winner:
            yield from self._resolve_chain_deaths()   # ★自爆者是狼美人 → 殉情连环

    # ========================================================
    # 警徽移交（子生成器）：警长出局时触发
    # ========================================================
    def _transfer_badge(self, seat):
        self._log_judge(f"🎖️ {seat}号警长出局，正在【静默】移交或撕毁警徽……")
        prompt = (self._time_anchor()
                  + f"【警徽移交·绝对静默】你（{seat}号）是警长且刚出局，只做一个【无声的动作】：把警徽移交他人或当场撕毁"
                  + f"（1.5票特权将转给接徽者）。当前存活：{self.alive_str()}。\n"
                  + "⚠️本环节【绝对不发言】：不得有任何公开陈述、喊话、交代或解释，speech 必须为空字符串。"
                  + "只输出严格JSON：{\"reasoning\":\"你的真实考量(上帝视角，不公开)\",\"speech\":\"\",\"badge_target\":X}，"
                  + "X为你要把警徽交给的存活座位号；若当场撕毁则填 0。")
        # public=False：移交是静默动作，绝不把 speech 播到公屏；只在结算后由法官播报【结果】。
        yield from self._ask(seat, prompt, f"D{self.day} 警徽移交(静默)", public=False, response_kind="badge_target")
        target = self._last_resp.get("badge_target", 0)
        if target in self.alive_seats() and target != seat:
            self.badge_history.append({"day": int(self.day), "from": int(seat), "to": int(target)})
            self.sheriff = target
            self._log_judge(f"🎖️ 警徽移交给 {target}号，{target}号成为新警长。")
            self._broadcast_event(f"【警徽移交】{seat}号警长把警徽交给 {target}号，{target}号成为新警长（投票算1.5票）。")
            self._add_replay(f"- 🎖️ 警徽移交：{seat}号 → {target}号")
        else:
            self.sheriff = None
            self._log_judge("🎖️ 警徽被当场撕毁，本局不再有警长。")
            self._broadcast_event(f"【警徽撕毁】{seat}号警长当场撕毁警徽，本局不再有警长。")
            self._add_replay(f"- 🎖️ 警徽撕毁：{seat}号 当场撕毁，本局无警长")

    def _resolve_sheriff_death(self):
        """若现任警长已死亡且尚未交接，触发警徽移交。"""
        if self.sheriff is not None and not self.players[self.sheriff]["alive"]:
            yield from self._transfer_badge(self.sheriff)

    # ========================================================
    # 公布昨夜死讯（子生成器）：死讯播报 + 猎人夜死开枪 + 警长夜死移交 + 首夜遗言
    # （在“竞选日”里于竞选完成后才调用；若竞选被自爆打断则整段被吞、不调用）
    # ========================================================
    def _finalize_pending_deaths_silently(self):
        """★V6.7：自爆吞死讯 / 竞选内终局时，把暂时复活的昨夜死者【真正处死】但不公布、无遗言(信息被吞)。
        暂存死者按同一批次落地，并在整批完成后立即判定胜负。"""
        seats = [s for s in self.pending_night_deaths if self.players.get(s, {}).get("alive")]
        self._mark_dead_batch(seats, "吞死讯后的夜间死亡")
        self.pending_night_deaths = {}

    def _announce_morning(self, deaths, peace_first):
        for seat in deaths:
            self.published_alive[seat] = False
        if deaths:
            dead_list = "、".join(f"{s}号" for s in sorted(deaths))
            self._log_judge(f"📢 昨夜倒下的是：{dead_list}。（死因不公布）")
            self._broadcast_event(f"【法官播报】昨夜（第{self.day}夜）{dead_list}倒下，系夜间死亡，死因不公开。")
        elif peace_first:
            self._log_judge("📢 平安夜！昨晚无人死亡。第一天没有遗言环节，直接进入白天。")
            self._broadcast_event(f"【法官播报】第{self.day}夜是平安夜，无人死亡，没有遗言环节。")
        else:
            self._log_judge("📢 昨夜是平安夜，无人死亡。")
            self._broadcast_event(f"【法官播报】第{self.day}夜是平安夜，无人死亡。")
        yield self._ev("announce", "结算公布")

        # 夜里猎人被刀（非毒杀）→ 临终开枪（事件回调：角色自报 has_death_action）
        for s in sorted(deaths):
            role = self.roles.get(s)
            if role is not None and role.has_death_action(self, deaths[s]):
                yield from self._run_death_action(role)
                if self.winner:   # ★熔断：开枪即终局 → 跳过移交/遗言，直接结束
                    return

        # 第一天死者有遗言（★V6.7 死人上警：若夜死者当选警长，先发表遗言，再移交警徽）
        if self.day == 1 and deaths:
            self._log_judge("🕯️ 第一天，死者可发表遗言。")
            for s in sorted(deaths):
                yield from self._last_words(s)

        # 警长若夜间死亡（或死者当选警长后倒牌）→ 遗言后移交/撕毁警徽
        yield from self._resolve_sheriff_death()

    # ========================================================
    # ★ 警长竞选状态机（子生成器）
    # ========================================================
    def _run_election(self):
        entry_resumed = (self.election_state == 2)   # 是否顺延后的复选
        self.election_state = 1
        if entry_resumed:
            self._log_judge("📣 警长竞选【顺延复选】开始：请存活玩家重新决定是否上警。")
            self._broadcast_event("【警长竞选·复选】顺延后的复选开始，请重新决定是否上警。")
        else:
            self._log_judge("📣 警长竞选开始：请存活玩家决定是否上警。")
            self._broadcast_event("【警长竞选】开始，请决定是否上警竞选警长（警长票×1.5）。")
        yield self._ev("election", "警长竞选")

        # 1) 自主报名（盲报：互不知晓，全部报名后才统一公布名单）
        self._log_judge("🙋 请各位【独立、互不知晓】地决定是否上警，报名结束后统一公布名单。")
        candidates = []
        for s in self.alive_seats():
            # public=False：报名静默进行，不在公屏逐个剧透（阴间仍可见其盘算）
            yield from self._ask(s, self._police_join_prompt(s), f"D{self.day} 上警报名", public=False, response_kind="join_police")
            if self._explode_requested(s):
                yield from self._handle_election_explode(s, entry_resumed)
                return
            if self._parse_join(self._last_resp):
                candidates.append(s)
        if not candidates:
            self._log_judge("🙅 无人上警，本局不设警长。")
            self._broadcast_event("【警长竞选】无人上警，本局不设警长。")
            self.election_state = 3
            return
        self._log_judge(f"🙋 上警名单（统一公布）：{'、'.join(f'{c}号' for c in candidates)}。")
        self._broadcast_event(f"【上警名单】本轮上警竞选警长的有：{'、'.join(f'{c}号' for c in candidates)}。")

        # 2) 上警发言（随机起点 + 方向）
        start = self.rng.choice(candidates)
        direction = self.rng.choice([1, -1])
        seq = [((start - 1 + direction * k) % 12) + 1 for k in range(0, 12)]
        speak_order = [s for s in seq if s in candidates]
        self._log_judge(f"🗣️ 上警发言（随机起点 {start}号，{'顺时针' if direction == 1 else '逆时针'}）："
                        + "→".join(f"{s}号" for s in speak_order) + "。")
        pol_speeches = []
        for s in speak_order:
            yield from self._ask(s, self._police_speech_prompt(s, pol_speeches), f"D{self.day} 上警发言", public=True)
            if self._explode_requested(s):
                self.election_speeches = pol_speeches   # 即便中断也把已有警上发言留给白天继承
                yield from self._handle_election_explode(s, entry_resumed)
                return
            pol_speeches.append((s, self._last_resp["speech"]))
        # ★ 把警上发言存给引擎，供当日白天发言阶段继承(治“警上发言失忆症”)
        self.election_speeches = pol_speeches

        # 3) 退水（只做决定，不发言；静默进行，阴间可见考量）
        self._log_judge("💧 退水环节：上警玩家只决定【退水/不退水/自爆】，不再发言。")
        withdrawers = []
        for s in speak_order:
            yield from self._ask(s, self._withdraw_prompt(s), f"D{self.day} 退水", public=False, response_kind="withdraw")
            if self._explode_requested(s):
                yield from self._handle_election_explode(s, entry_resumed)
                return
            if self._parse_withdraw(self._last_resp):
                withdrawers.append(s)
        remaining = [c for c in candidates if c not in withdrawers]
        if withdrawers:
            self._log_judge(f"💧 退水：{'、'.join(f'{w}号' for w in withdrawers)}；仍在警上：{'、'.join(f'{r}号' for r in remaining) or '无'}。")
            self._broadcast_event(f"【退水】{'、'.join(f'{w}号' for w in withdrawers)} 退水，退出竞选且失去警下投票权。")

        if not remaining:
            self._log_judge("🙅 上警玩家全部退水，本局不设警长。")
            self._broadcast_event("【警长竞选】全部退水，本局不设警长。")
            self.election_state = 3
            return
        if len(remaining) == 1:
            self.sheriff = remaining[0]
            self.election_state = 3
            self._log_judge(f"🎖️ 仅 {self.sheriff}号 留在警上，自动当选警长！")
            self._broadcast_event(f"【警长诞生】{self.sheriff}号 当选警长（投票算1.5票）。")
            return

        # 4) 警下盲投（上警者含退水者均无投票权）
        #   铁律：voters = 存活 − candidates。candidates 是【报名上警的全集】，退水者(withdrawers)是其子集，
        #   故任何曾上警的人(无论是否退水)都【绝不会】出现在 voters 里，自然丧失警下投票权。
        voters = [s for s in self.alive_seats() if s not in candidates]
        if not voters:
            self.sheriff = self.rng.choice(remaining)
            self.election_state = 3
            self._log_judge(f"🎖️ 无警下投票人，随机指定 {self.sheriff}号 为警长。")
            self._broadcast_event(f"【警长诞生】{self.sheriff}号 成为警长（投票算1.5票）。")
            return
        self._log_judge(f"🗳️ 警下盲投：{'、'.join(f'{v}号' for v in voters)} 在 {'、'.join(f'{r}号' for r in remaining)} 中选警长。")
        yield self._ev("police_vote", "警下盲投")
        votes, decision_sources = {}, {}
        for v in voters:
            yield from self._ask(v, self._police_vote_prompt(v, remaining), f"D{self.day} 警下投票", response_kind="vote", allowed_targets=[0, *remaining])
            vt = self._last_resp.get("vote_target", 0)
            votes[v] = vt if vt in remaining else 0
            decision_sources[v] = self._decision_source(self._last_resp, vt, {0, *remaining})
        # 竞选阶段尚无警长(全1票)；用“当选警长”而非“被放逐”的措辞
        out, detail, totals = self._weighted_tally(votes, win_word="当选警长", none_word="本轮无人达成多数")
        detail = self._vote_detail(detail, decision_sources)
        self._log_judge(f"【警长竞选票型】{detail}")
        self._broadcast_event(f"【警长竞选票型】{detail}")
        self._log_votechart(f"🎖️ 第{self.day}天 · 警长竞选票型", totals, votes, out, decision_sources)   # ★柱状图
        self._add_replay(f"- 🗳️ 警长竞选票型：{detail}")

        # ★ 平票【绝不随机】，进入 PK 环节
        if out is None and totals:
            maxw = max(totals.values())
            tied = [t for t, w in totals.items() if w == maxw]
            if len(tied) >= 2:
                out = yield from self._run_election_pk(tied, voters, entry_resumed)
                if self._day_interrupted:   # PK 中有人自爆 → 顺延/吞警徽，交由主循环处理
                    return
                if out is None:             # PK 再次平票 → 已在内部设无警长并广播
                    return

        # 收尾：产生了警长 / 全场弃票无警长
        if out is not None:
            self.sheriff = out
            self.election_state = 3
            self._log_judge(f"🎖️ {self.sheriff}号 当选警长！（投票算1.5票）")
            self._broadcast_event(f"【警长诞生】{self.sheriff}号 当选警长（投票算1.5票）。")
        else:
            self.sheriff = None
            self.election_state = 3
            self._log_judge("【警长竞选】全场弃票，本局不设警长。")
            self._broadcast_event("【警长竞选】全场弃票，本局不设警长。")

    # ========================================================
    # ★ 警长竞选·PK 状态机：平票候选人 PK 发言 → 警下二次盲投 → 再平票则警徽流失
    #   返回 PK 胜者座位号；若 PK 再平票则返回 None(并已设无警长)；若 PK 中自爆则置 _day_interrupted。
    # ========================================================
    def _run_election_pk(self, tied, voters, entry_resumed):
        self._log_judge("【警长竞选】出现平票，平票候选人进入 PK 发言！")
        self._broadcast_event(f"【警长竞选·PK】{'、'.join(f'{c}号' for c in tied)} 平票，进入 PK 环节。")
        yield self._ev("police_pk", "警长PK")
        # PK 发言（允许狼人自爆）
        pk_speeches = []
        for s in tied:
            yield from self._ask(s, self._police_pk_prompt(s, pk_speeches, tied), f"D{self.day} PK发言", public=True)
            if self._explode_requested(s):
                yield from self._handle_election_explode(s, entry_resumed)
                return None
            pk_speeches.append((s, self._last_resp["speech"]))
        # 把 PK 发言也并入“警上发言”上下文，供白天继承
        self.election_speeches = (self.election_speeches or []) + [(seat, f"【PK发言】{sp}") for seat, sp in pk_speeches]
        # PK 盲投（警下玩家在平票候选人里二选一）
        self._log_judge(f"🗳️ PK 盲投：{'、'.join(f'{v}号' for v in voters)} 在 {'、'.join(f'{c}号' for c in tied)} 中再选一次。")
        yield self._ev("police_pk_vote", "PK盲投")
        pk_votes, decision_sources = {}, {}
        for v in voters:
            yield from self._ask(v, self._police_vote_prompt(v, tied), f"D{self.day} PK投票", response_kind="vote", allowed_targets=[0, *tied])
            vt = self._last_resp.get("vote_target", 0)
            pk_votes[v] = vt if vt in tied else 0
            decision_sources[v] = self._decision_source(self._last_resp, vt, {0, *tied})
        out2, detail2, totals2 = self._weighted_tally(pk_votes, win_word="当选警长", none_word="PK仍无人达成多数")
        detail2 = self._vote_detail(detail2, decision_sources)
        self._log_judge(f"【PK票型】{detail2}")
        self._broadcast_event(f"【PK票型】{detail2}")
        self._log_votechart(f"⚔️ 第{self.day}天 · 警长PK票型", totals2, pk_votes, out2, decision_sources)   # ★柱状图
        self._add_replay(f"- 🗳️ 警长PK票型：{detail2}")
        if out2 is not None:
            return out2
        # PK 再次平票 / 全弃票 → 警徽流失，本局无警长
        self.sheriff = None
        self.election_state = 3
        self._log_judge("【警长竞选】PK环节再次平票，警徽流失，本局无警长。")
        self._broadcast_event("【警长竞选】PK环节再次平票，警徽流失，本局无警长。")
        return None

    # ========================================================
    # ★ 常规白天：发言 + 尾发言归票 + 加权放逐投票（子生成器）
    #   顺序发言里检测狼人自爆 → 中断、跳过投票、入夜
    # ========================================================
    def _handle_regular_explode(self, s):
        """常规白天自爆：自爆者出局、中断白天、跳过放逐、入夜。
        白狼王(camp=狼人, role=白狼王)额外【带走 explode_target】；被带走者若是猎人则触发临终开枪。
        ★熔断：每一处 alive=False 都紧跟 check_winner()，屠边即 return；连环死亡逐环判胜负。"""
        role = self.roles[s]
        is_wwk = (role.role_name == "白狼王")
        target = 0
        if is_wwk:
            t = (self._last_resp or {}).get("explode_target", 0)
            if t in self.alive_seats() and t != s:
                target = t   # 仅在带人目标合法(存活且非自己)时生效；否则退化为普通自爆

        # (1) 自爆者出局
        self._mark_dead(s, "白天自爆")   # ★熔断①
        if not self.winner:
            self._schedule_last_stand_wolf(s)
        self._confirm_role(s, self.players[s]["role"], "公开自爆")
        self._record_explosion(s, "regular_day", target)
        self._record_event(
            "player_exploded",
            seat=s,
            role=self.players[s]["role"],
            context="regular_day",
            explode_target=target,
        )
        self._day_interrupted = True
        if is_wwk and target:
            self._log_judge(f"💥 {s}号【白狼王】发动毁灭自爆，强行带走 {target}号！白天中断、跳过放逐，直接入夜。")
            self._broadcast_event(f"【白狼王自爆】{s}号白狼王自爆并强行带走 {target}号，白天提前结束、跳过放逐投票，直接入夜。")
            self._add_replay(f"💥 白狼王 {s}号 发动毁灭自爆，强行带走了 {target}号！")
        else:
            self._log_judge(f"💥 {s}号狼人自爆出局！白天中断、跳过放逐投票，直接入夜。")
            self._broadcast_event(f"【自爆】{s}号狼人自爆出局，白天提前结束、跳过放逐投票，直接入夜。")
            self._add_replay(f"- 💥 狼人自爆：{s}号自爆，白天强制结束")
        if self.winner:
            return   # 自爆即终局(如最后一狼自爆) → 跳过带人/移交，直接结束

        # (2) 白狼王带走目标（与自爆同时发生）
        if is_wwk and target and self.players[target]["alive"]:
            victim_role = self.roles[target]
            self._mark_dead(target, "白狼王自爆带走")   # ★熔断②
            self._broadcast_event(f"【法官播报】{target}号被白狼王 {s}号 强行带走出局。")
            self._add_replay(f"- 💀 白狼王带走：{target}号({self.players[target]['role']})")
            if self.winner:
                return
            # (3) 连环：被带走者若是猎人 → 临终开枪（非毒杀可开；_run_death_action 内部已熔断）
            if victim_role.has_death_action(self, "explode"):
                yield from self._run_death_action(victim_role)   # ★熔断③
                if self.winner:
                    return

        # (3.5) 若自爆者本身是狼美人、或被白狼王带走的目标是狼美人 → 触发殉情连环
        yield from self._resolve_chain_deaths()
        if self.winner:
            return
        # (4) 自爆者/被带走者/被开枪者中若有现任警长出局 → 统一移交警徽
        yield from self._resolve_sheriff_death()

    def _apply_consolidation(self, s):
        """解析归票目标：拦截“归票自己”，合法则播报+广播，返回归票目标(或 None)。"""
        cands = set(self.alive_seats()) - {s}
        if "vote_target" in self._last_resp:
            target = self._last_resp["vote_target"]
            t = target if target in cands and not self._last_resp.get("_is_fallback") else None
        else:
            t = parse_seat(self._last_resp["speech"], cands, fallback=False)
        if t:
            who = "警长" if self.sheriff == s else "末位发言者"
            self._log_judge(f"📌 {who} {s}号 归票：建议大家投 {t}号。")
            self._broadcast_event(
                f"【归票建议】{who}({s}号)公开建议放逐 {t}号。"
                "这只是待核验的公开意见，不是身份认证；所有玩家仍须独立核验证据后投票。"
            )
            self._add_replay(f"- 📌 归票建议：{s}号 建议放逐 {t}号（非强制）")
        return t

    # ========================================================
    # ★V6.5 骑士决斗（白天发言阶段拦截）：返回 True=白天就此结束(决斗成功或终局)，False=白天继续
    # ========================================================
    def _duel_requested(self, seat) -> bool:
        r = self.roles[seat]
        if r.role_name != "骑士" or getattr(r, "has_dueled", False):
            return False
        return bool((self._last_resp or {}).get("is_duel"))

    def _handle_knight_duel(self, s):
        role = self.roles[s]
        role.has_dueled = True   # 一次性技能，无论成败用掉
        target = (self._last_resp or {}).get("duel_target", 0)
        if target not in self.alive_seats() or target == s:
            self._log_judge(f"⚔️ {s}号骑士发起决斗，但目标无效，决斗作废，白天继续。")
            self._broadcast_event(f"【骑士决斗】{s}号骑士决斗目标无效，本次决斗作废。")
            self._add_replay(f"- ⚔️ 骑士决斗：{s}号 目标无效，作废")
            return False
        if self.roles[target].camp == "狼人":
            # 决斗成功：目标是狼 → 当场处决，白天立即结束入夜
            self._confirm_role(s, "骑士", "公开发动并成功完成决斗")
            self._mark_dead(target, "骑士决斗处决")   # ★熔断
            self._day_interrupted = True
            self._log_judge(f"⚔️ 骑士决斗成功，{target}号 是狼人！当场处决，白天结束直接入夜。")
            self._broadcast_event(f"【骑士决斗】{s}号骑士查杀 {target}号是狼人并当场处决，白天直接结束入夜。")
            self._add_replay(f"- ⚔️ 骑士决斗：{s}号 决斗 {target}号【成功·是狼】→ {target}号出局，白天提前结束")
            if self.winner:
                return True
            yield from self._resolve_chain_deaths()    # 目标可能是狼美人 → 殉情连环
            if self.winner:
                return True
            yield from self._resolve_sheriff_death()  # 目标若是警长 → 移交
            return True
        else:
            # 决斗失败：目标是好人 → 骑士撞死谢罪，白天继续
            self._confirm_role(s, "骑士", "公开发动决斗")
            self._mark_dead(s, "骑士决斗失败")   # ★熔断
            self._log_judge(f"⚔️ 骑士决斗失败，{target}号 是好人，{s}号骑士以死谢罪！白天继续。")
            self._broadcast_event(f"【骑士决斗】{s}号骑士决斗 {target}号失败(好人)，骑士以死谢罪出局，白天继续。")
            self._add_replay(f"- ⚔️ 骑士决斗：{s}号 决斗 {target}号【失败·是好人】→ 骑士{s}号 以死谢罪")
            if self.winner:
                return True   # 骑士之死导致终局(极罕见) → 结束
            yield from self._resolve_sheriff_death()  # 骑士若是警长 → 移交
            return False      # 白天继续

    # ========================================================
    # ★V6.7 反应式抢爆/抢决斗窗口（吞发言）：受【每日硬预算】封顶，绝不退化成无界全场轮询。
    #   仅在出现“查杀/归票”等威胁发言后才开窗，让自爆能真正吞掉【后续】发言与投票。
    # ========================================================
    @staticmethod
    def _speech_is_threat(text) -> bool:
        """纯文本启发式：判断刚才那句发言是否像‘查杀/归票/抗推/点票’等威胁(决定要不要开抢爆窗)。不使用上帝视角。"""
        return bool(re.search(r"查杀|归票|是狼|的狼|认定.{0,4}狼|抗推|出掉|带走|"
                              r"投他|投.{0,3}\d{1,2}号|票.{0,3}\d{1,2}号|怀疑.{0,3}\d{1,2}号|建议.{0,4}投",
                              str(text or "")))

    def _react_explode_prompt(self, seat, today_speeches):
        spoken = "\n".join(f"{s}号：{sp}" for s, sp in today_speeches[-6:]) or "（无）"
        wwk = " 你是【白狼王】，自爆可【同时带走一人】：再加 \"explode_target\": X。" \
            if self.roles[seat].role_name == "白狼王" else ""
        return (self._time_anchor()
                + f"【{self.phase}·抢爆窗口·私下询问】刚出现了对你/狼队不利的查杀或归票。你（{seat}号·狼）"
                + "现在可以【抢先自爆】，立刻【吞掉后续所有发言与本轮放逐投票】(打断信息、保护队友、掩盖女巫用药)。\n"
                + f"最近发言：\n{spoken}\n当前存活：{self.alive_str()}。\n"
                + "要自爆就输出 JSON \"is_explode\": true" + wwk + "；不爆则 \"is_explode\": false。"
                + "⚠️只有当后续信息真的会重创狼队、自爆收益＞暴露代价时才爆，【优势局/无实质威胁别乱爆白送一狼】。speech 一句话即可。")

    def _react_duel_prompt(self, seat, today_speeches):
        spoken = "\n".join(f"{s}号：{sp}" for s, sp in today_speeches[-6:]) or "（无）"
        return (self._time_anchor()
                + f"【{self.phase}·抢决斗窗口·私下询问】你（{seat}号·骑士）听完刚才的发言，现在可以【立刻发起决斗】一锤定音"
                + "(决斗中狼→当场处决、白天直接结束；决斗错杀好人→你自爆谢罪)。\n"
                + f"最近发言：\n{spoken}\n当前存活：{self.alive_str()}。\n"
                + "要决斗就输出 JSON \"is_duel\": true 与 \"duel_target\": X(你高度怀疑是狼的人)；否则 \"is_duel\": false。"
                + "⚠️赌错会以死谢罪，务必基于扎实逻辑、别乱赌。speech 一句话即可。")

    def _maybe_react_window(self, today_speeches, exclude=None):
        """抢爆/抢决斗窗口子生成器：受 self._react_left 预算硬约束(耗尽即整窗 no-op)。
        返回 True 表示白天已被中断(有人自爆/决斗成功)，调用方应立即 return。"""
        if self._react_left <= 0:
            return False
        # 1) 每个威胁窗口只询问一只存活狼，并在狼队间轮转。
        # 原逻辑会对所有狼逐个发请求，一句查杀可能额外触发 3~4 次深度推理，
        # 既重复决策也让后期上下文和 WebSocket 压力成倍增长。
        wolves = [s for s in self.alive_seats()
                  if s != exclude and self.roles[s].camp == "狼人"]
        if wolves and self._react_left > 0:
            w = wolves[self._react_cursor % len(wolves)]
            self._react_cursor += 1
            self._react_left -= 1
            yield from self._ask(w, self._react_explode_prompt(w, today_speeches),
                                 f"D{self.day} 抢爆判断", public=False, response_kind="react_explode")
            if self._explode_requested(w):
                yield from self._handle_regular_explode(w)
                return True
        # 2) 存活的骑士：是否抢决斗(私下询问)
        k = self._seat_with_role("骑士")
        if (k is not None and k != exclude and self._react_left > 0
                and self.players[k]["alive"] and not getattr(self.roles[k], "has_dueled", False)):
            self._react_left -= 1
            yield from self._ask(k, self._react_duel_prompt(k, today_speeches),
                                 f"D{self.day} 抢决斗判断", public=False, response_kind="react_duel")
            if self._duel_requested(k):
                day_over = yield from self._handle_knight_duel(k)
                if day_over:
                    return True
        return False

    def _run_day_discussion_and_vote(self, deaths, peace_first):
        order, reason = yield from self._decide_speech_order(deaths)
        # 归票权：有警徽 → 警长归票带队；无警徽 → 末位发言者归票
        sheriff_alive = self.sheriff is not None and self.players[self.sheriff]["alive"]
        gui = f"（归票由警长 {self.sheriff}号 负责）" if sheriff_alive else "（无警徽，由末位发言者归票）"
        self._log_judge(f"💬 进入【发言阶段】：{reason}。发言顺序："
                        + "→".join(f"{s}号" for s in order) + "。" + gui)
        yield self._ev("speech_phase", "发言阶段")
        # ★ 修复“警上发言失忆症”：本日若进行过警长竞选，把警上发言并入今日发言上下文。
        today_speeches = []
        if self.election_speeches:
            today_speeches.extend([(seat, f"【警上发言】{sp}") for seat, sp in self.election_speeches])
        consolidation_target = None
        # ★每日重置抢爆/抢决斗预算(硬封顶)；读 self.react_budget(前端滑块可实时改写)，回退到 config 默认
        self._react_left = int(getattr(self, "react_budget", getattr(config, "EXPLODE_REACT_BUDGET", 0)))
        self._react_cursor = 0

        for idx, s in enumerate(order):
            # 无警徽时，末位发言者顺便归票；有警徽则所有人正常发言、由警长最后单独归票
            do_consolidate = (not sheriff_alive) and idx == len(order) - 1 and len(order) >= 2
            if do_consolidate:
                yield from self._ask(s, self._consolidation_prompt(s, today_speeches, order), f"D{self.day} 归票", public=True, response_kind="consolidation")
            else:
                yield from self._ask(s, self._day_speech_prompt(s, today_speeches, deaths, peace_first, order),
                                     f"D{self.day} 发言", public=True)
            today_speeches.append((s, self._last_resp["speech"]))   # 先记录发言(自爆/决斗也已发过言)
            if self._explode_requested(s):
                yield from self._handle_regular_explode(s)
                return
            if self._duel_requested(s):                      # ★骑士决斗(白天技能截断)
                day_over = yield from self._handle_knight_duel(s)
                if day_over:
                    return                                   # 决斗成功(目标狼)→白天结束入夜；或终局
                continue                                     # 决斗失败(骑士已死)→不再归票，继续下一位
            if do_consolidate:
                consolidation_target = self._apply_consolidation(s)
            # ★V6.7 反应式抢爆/抢决斗窗口：刚才发言若像【查杀/归票】(威胁)，给狼/骑士一次【立刻反应、吞掉后续】的机会
            #   (受 _react_left 每日预算硬封顶；非威胁发言不开窗，避免无界轮询)
            if self._speech_is_threat(today_speeches[-1][1]):
                if (yield from self._maybe_react_window(today_speeches, exclude=s)):
                    return

        # 有警徽：警长在所有人发言后提出归票建议（票×1.5）；其他好人仍须独立核验证据。
        if sheriff_alive:
            sh = self.sheriff
            self._log_judge(f"🎖️ 由警长 {sh}号 归票带队。")
            yield from self._ask(sh, self._consolidation_prompt(sh, today_speeches, order), f"D{self.day} 警长归票", public=True, response_kind="consolidation")
            today_speeches.append((sh, self._last_resp["speech"]))
            if self._explode_requested(sh):
                yield from self._handle_regular_explode(sh)
                return
            if self._duel_requested(sh):                     # ★警长是骑士也可决斗
                day_over = yield from self._handle_knight_duel(sh)
                if day_over:
                    return
                # 警长骑士决斗失败身亡(警徽已在 duel 内移交) → 跳过归票，直接进入投票
            else:
                consolidation_target = self._apply_consolidation(sh)
                # ★V6.7 警长归票直接导向投票 → 再给一次抢爆窗口(狼可抢爆吞掉本轮投票)
                if self._speech_is_threat(today_speeches[-1][1]):
                    if (yield from self._maybe_react_window(today_speeches, exclude=sh)):
                        return

        # 放逐投票（警长票×1.5），完整票型公开 + 广播
        self._log_judge("🗳️ 进入【投票阶段】，全体同时暗投（警长票×1.5）。")
        yield self._ev("vote_phase", "投票阶段")
        voters = [s for s in order if self.players[s]["alive"]]
        votes, decision_sources = {}, {}
        for s in voters:
            yield from self._ask(s, self._vote_prompt(s, today_speeches, consolidation_target, order),
                                 f"D{self.day} 暗投", response_kind="vote")
            vt = self._last_resp.get("vote_target", 0)
            legal = (set(voters) - {s}) | {0}
            votes[s] = vt if vt in legal else 0
            decision_sources[s] = self._decision_source(self._last_resp, vt, legal)
        out, detail, totals = self._weighted_tally(votes)
        detail = self._vote_detail(detail, decision_sources)
        self._log_judge(f"【放逐投票结果】{detail}")
        self._broadcast_event(f"【放逐票型】{detail}")
        self._log_votechart(f"🗳️ 第{self.day}天 · 放逐投票票型", totals, votes, out, decision_sources)   # ★柱状图
        self._add_replay(f"- 🗳️ 放逐票型：{detail}")   # 复盘：完整票型
        if out is not None:
            self._mark_dead(out, "白天公投放逐")
            # ★V6.5 守墓人：记录【昨日被公投放逐者】及其真实阵营(camp)，供今夜守墓人验墓
            self.last_exiled_seat = out
            self.last_exiled_camp = "狼人" if self.roles[out].camp == "狼人" else "好人"
            self._broadcast_event(f"【法官播报】{out}号被公投放逐出局。")
            # ★★★ 放逐后已由 _mark_dead 立刻判胜负；终局则跳过遗言/开枪/殉情/移交。
            if self.winner:
                return
            yield from self._last_words(out)
            out_role = self.roles.get(out)
            if out_role is not None and out_role.has_death_action(self, "vote"):
                yield from self._run_death_action(out_role)
                if self.winner:   # ★熔断：放逐的猎人开枪即终局 → 跳过殉情/移交，直接结束
                    return
            # ★V6.5：被放逐者若是狼美人 → 殉情连环(目标随之死、其若猎人则开枪)
            yield from self._resolve_chain_deaths()
            if self.winner:
                return
            yield from self._resolve_sheriff_death()   # 警长被放逐/被枪 → 警徽移交

    def _guard_replay_line(self) -> str:
        """用互斥措辞记录守卫状态，避免把“空守”和“本局无守卫”混为一谈。"""
        guard_seat = self._seat_with_role("守卫")
        if guard_seat is None:
            return "- 🛡️ 守卫：本局无守卫"
        if not self.players[guard_seat]["alive"]:
            return f"- 🛡️ 守卫：{guard_seat}号守卫已出局，本夜无守护"
        target = self.night.get("guard_target")
        if target is None:
            return f"- 🛡️ 守卫：{guard_seat}号本夜空守"
        return f"- 🛡️ 守卫守护：{target}号"

    # ========================================================
    # ★★★ 主生成器 ★★★
    # ========================================================
    def run(self):
        if getattr(self, "_resume", False):
            # ★读档恢复：生成器无法原位续跑，故在【干净的夜晚边界】续：
            #   load_state 已注入 day/phase/生死/记忆等全部状态；这里跳过开场白，
            #   下面 while 的首个 self.day+=1 会把局面推进到【下一夜】重新开战。
            #   (因此存档语义=“从存档当天的下一夜继续”；建议在白天阶段存档以减少跳过。)
            self._resume = False
            self._log_judge(f"💾 已读档：自第 {self.day} 天的局面恢复，下面进入第 {self.day + 1} 夜继续推演……")
            yield self._ev("resume", f"读档恢复（第{self.day + 1}夜起）")
        else:
            self._log_judge("🌑 第一夜即将降临，请所有玩家闭眼……")
            yield self._ev("start", "游戏开始")
            yield from self._run_cupid_link()   # ★V6.6 丘比特开局连线(第0夜)；无丘比特则 no-op

        while not self.winner:
            self.day += 1

            # ==================== 夜晚 ====================
            self.phase = f"第{self.day}夜"
            self.current_speaker = None
            self.night = {}
            self._log_judge(f"🌙 天黑请闭眼 —— 第 {self.day} 个夜晚")
            yield self._ev("night", self.phase)
            label = f"D{self.day} 夜晚"

            # ★ 刷新‘绝对真理层’（当前局势）；并在第2夜起触发【记忆压缩】(滚动摘要)。
            self.manager.set_truth(self._truth_state())
            if self.day >= 2:
                compression_seats = [s for s in self.alive_seats() if s != self.human_seat]
                self._refresh_hard_facts(compression_seats)
                self._log_judge(f"🌙 第 {self.day} 夜降临，玩家们正在沉淀思绪（记忆整理中）……")
                compression_id = self.manager.submit_memory_compression(compression_seats)
                self._active_compression_id = compression_id
                yield self._ev("compress", "🧠 记忆整理中…")
                while True:
                    done, stats = self.manager.poll_memory_compression(compression_id)
                    if done:
                        break
                    yield self._ev("compress_waiting", "🧠 记忆整理中…")
                self._active_compression_id = None
                self._log_god(
                    f"🧠 记忆压缩完成：{stats['compressed']}人成功摘要 / "
                    f"{stats['fallback']}人兜底截断 / {stats.get('skipped', 0)}人上下文未达阈值跳过"
                )
                yield self._ev("compress_done", "记忆已整理，入夜")

            # ★V6.3/6.5 夜晚执行：守卫→守墓人→狼队(集体)→狼美人魅惑→女巫→预言家。
            #   solo 行动统一由 _run_solo_night 驱动(所有 yield 仍在引擎；角色纯净无 yield)；
            #   _solo() 按身份取实例(含狼阵营如狼美人)，本板子没有该身份则返回 None → 自动跳过。
            # 0) 魔术师（V7.0：夜间首位行动，交换两人号码牌）
            yield from self._run_solo_night(self._solo("魔术师"))

            # 1) 守卫
            yield from self._run_solo_night(self._solo("守卫"))

            # 1b) 摄梦人（V7.0：摄梦一人成为梦游者）
            yield from self._run_solo_night(self._solo("摄梦人"))

            # 1c) 守墓人（V6.5：法官私下告知昨日放逐者阵营）
            yield from self._run_solo_night(self._solo("守墓人"))

            # 2) 狼人：阶段一战术讨论 → 阶段二盲投落刀（集体行动，由引擎编排）
            yield from self._run_wolf_pack_night(label)

            # 2b) 狼美人（V6.5：狼队杀完后单独魅惑一人）
            yield from self._run_solo_night(self._solo("狼美人"))

            # 3) 女巫（面杀规则：解药仅首夜可自救；解药/毒药互斥）
            yield from self._run_solo_night(self._solo("女巫"))

            # 4) 预言家
            yield from self._run_solo_night(self._solo("预言家"))

            # —— 复盘：本夜行动（结算前记录真实动作）——
            self._add_replay(f"\n## 🌙 第{self.day}夜")
            self._add_replay(self._guard_replay_line())
            _wk = self.night.get("wolf_kill")
            self._add_replay("- 🐺 狼队刀：" + ("空刀" if _wk is None else f"{_wk}号" + ("(自刀)" if _wk in self.wolves else "")))
            _sv, _po = self.night.get("witch_save"), self.night.get("witch_poison")
            if _sv:
                self._add_replay(f"- 🧪 女巫：用【解药】救了 {_sv}号（本晚未用毒药）")
            elif _po:
                self._add_replay(f"- 🧪 女巫：未用解药；用【毒药】毒了 {_po}号")
            else:
                self._add_replay("- 🧪 女巫：未用解药、未用毒药（空过/空毒）")
            _sc = self.night.get("seer_check")
            self._add_replay(f"- 🔮 预言家查验：{_sc[0]}号 → {_sc[1]}" if _sc else "- 🔮 预言家：未验/无预言家")

            # ==================== 夜晚结算 ====================
            deaths = self.settle_night()
            deaths = self._augment_chain_deaths(deaths)   # ★V6.5/6.6：夜亡触发情侣/魅惑连带 → 并入夜间殉情
            self._mark_dead_batch(deaths.keys(), "夜间结算")
            self.current_speaker = None
            if deaths:
                self._add_replay("- 💀 结算死亡：" + "、".join(f"{s}号({self.players[s]['role']})" for s in sorted(deaths)))
            else:
                self._add_replay("- 💀 结算：平安夜，无人死亡")

            # ★★★ _mark_dead_batch 已即时判胜负。若终局，只播报死讯、不进白天(不开枪/不遗言)。
            if self.winner:
                for seat in deaths:
                    self.published_alive[seat] = False
                self.phase = f"第{self.day}天"
                self._log_judge(f"☀️ 天亮了 —— 第 {self.day} 天")
                if deaths:
                    self._log_judge("📢 昨夜倒下的是：" + "、".join(f"{s}号" for s in sorted(deaths)) + "。（死因不公布）")
                else:
                    self._log_judge("📢 昨夜平安夜，无人死亡。")
                yield self._ev("day", self.phase)
                break

            # ★V6.7 死不知情：本日若要竞选警长 且 有昨夜死者 → 把死者【暂时复活】以活人身份参选，
            #   竞选结束(或自爆中断)后再真正处死+公布(见下方竞选段)。非竞选日保持已死、照常公布。
            #   注意：此处的复活只在"狼刀未直接终局"时发生(终局已在上面 break)，故不会掩盖屠边。
            run_election_now = (self.election_state == 0 and self.day == 1) or (self.election_state == 2)
            self.pending_night_deaths = {}
            if run_election_now and deaths:
                self.pending_night_deaths = dict(deaths)
                for s in deaths:
                    self.players[s]["alive"] = True   # 系统暂时视作活人(死不知情)

            # ==================== 白天 ====================
            self.phase = f"第{self.day}天"
            self._day_interrupted = False
            self.election_speeches = []   # 每个白天先清空，竞选若进行会重新写入，避免旧警上发言串味
            peace_first = (self.day == 1 and not deaths)
            self.manager.set_truth(self._truth_state())   # 结算后刷新‘当前局势’真理层
            self._log_judge(f"☀️ 天亮了 —— 第 {self.day} 天")
            self._add_replay(f"\n## ☀️ 第{self.day}天")
            yield self._ev("day", self.phase)

            if run_election_now:
                # 面杀规则：天亮先竞选警长，昨夜死讯【暂不公布】——给狼人“自爆吞信息”空间。
                # ★V6.7 死人上警：昨夜死者已暂时复活(死不知情)，可报名/上警发言/退水/警下投票/当选警长。
                self._log_judge("📣 天亮后先进行警长竞选，昨夜死讯【暂不公布】"
                                + ("，昨夜死者亦以活人身份参与(死不知情)。" if self.pending_night_deaths else "。"))
                yield from self._run_election()
                # 复盘：警长竞选结果
                if self.election_state == 4:
                    self._add_replay("- 🎖️ 警长竞选：💥 双爆吞警徽，本局无警长")
                elif self.sheriff:
                    self._add_replay(f"- 🎖️ 警长竞选：{self.sheriff}号 当选警长")
                else:
                    self._add_replay("- 🎖️ 警长竞选：无人上警/流警，本局无警长")
                self.winner = self.check_winner()
                if self.winner:
                    self._finalize_pending_deaths_silently()   # 竞选内终局：仍复活态的昨夜死者静默处死(本就该死)
                    break
                if self._day_interrupted:
                    # 有人自爆 → 昨夜死讯被【吞】：死者静默处死、不公布、无猎人开枪、无遗言、无放逐，直接入夜
                    self._log_judge("🤐 因自爆，昨夜死讯被【吞掉】：本日不公布死讯、无遗言、无放逐，直接入夜。")
                    self._broadcast_event("【信息被吞】因有人自爆，昨夜死讯未公布、死者无遗言、本日无放逐投票。")
                    self._finalize_pending_deaths_silently()   # 昨夜死者真正处死，但不公布(信息被吞)
                    self.winner = self.check_winner()
                    if self.winner:
                        break
                    self._day_interrupted = False
                    continue
                # ★竞选正常结束 → 现在才【真正处死】昨夜死者 + 公布死讯 + 遗言 + (死者当选则)遗言后移交警徽
                if self.pending_night_deaths:
                    self._mark_dead_batch(self.pending_night_deaths.keys(), "竞选后公布夜间死亡")
                    self.manager.set_truth(self._truth_state())   # 死讯落定 → 刷新存活真理层
                self.pending_night_deaths = {}
                yield from self._announce_morning(deaths, peace_first)
                self.winner = self.check_winner()
                if self.winner:
                    break
            else:
                # 非竞选日：天亮即公布昨夜死讯
                yield from self._announce_morning(deaths, peace_first)
                self.winner = self.check_winner()
                if self.winner:
                    break

            # ===== 常规白天：发言 + 归票 + 放逐投票 =====
            yield from self._run_day_discussion_and_vote(deaths, peace_first)
            self.winner = self.check_winner()
            if self.winner:
                break
            self._day_interrupted = False

        # ==================== 游戏结束 ====================
        self.current_speaker = None
        self.phase = "游戏结束"
        if self.winner == "好人":
            self._log_win("🏆 游戏结束！所有狼人被处决，【好人阵营】获胜！")
        elif self.winner == "情侣":
            lv = self.lovers or ("?", "?")
            self._log_win(f"🏆 游戏结束！💘【情侣第三方】活到最后，{lv[0]}号 ❤️ {lv[1]}号 (及丘比特)通吃全场获胜！")
        else:
            self._log_win("🏆 游戏结束！屠边成功，【狼人阵营】获胜！")
        self._add_replay(f"\n## 🏁 结果\n**🏆 {self.winner}阵营获胜！（共 {self.day} 天）**")
        yield self._ev("end", f"{self.winner}胜利")

    # ========================================================
    # 解析：是否上警 / 是否退水
    # ========================================================
    def _parse_join(self, resp) -> bool:
        if resp.get("join_police") is True:
            return True
        sp = resp.get("speech", "")
        if re.search(r"不\s*上警|不\s*参选|不\s*竞选|警下|不\s*上", sp):
            return False
        return bool(re.search(r"上警|参选|竞选警长|我上", sp))

    def _parse_withdraw(self, resp) -> bool:
        if resp.get("withdraw") is True:
            return True
        sp = resp.get("speech", "")
        if re.search(r"不\s*退水|不退|坚持竞选|继续竞选", sp):
            return False
        return bool(re.search(r"退水|退出竞选|我退", sp))

    # ========================================================
    # 提示词构造
    # ========================================================
    def _wolf_explode_line(self, seat):
        if self.roles[seat].camp == "狼人":   # ★V6.4：含白狼王(它另有 explode_target 带人，见其 playbook)
            return ("\n（你是狼人阵营：若选择自爆，请在输出JSON里加 \"is_explode\": true —— "
                    "你将立即出局、本白天直接结束入夜，可用于打断警长竞选。）")
        return ""

    def _role_playbook(self, seat):
        # ★V6.3：阵营打法（含 V6.2 认知补丁）委托给角色实例(get_playbook_prompt)。
        return self.roles[seat].get_playbook_prompt(self)

    def _good_audit_brief(self, seat: int) -> str:
        """给好人分配稳定的独立审计视角，降低同模型玩家同步犯错的相关性。"""
        tasks = (
            "机制与时间线审计：逐项核对死讯、技能发动、自爆和警徽流转，优先指出与客观事实冲突的说法。",
            "预言家对跳审计：分别写出两边最强支持证据和最大漏洞，从狼队收益与查验链判断，不看谁更凶。",
            "票型与关系审计：检查谁组织冲票、谁持续互保、哪条警徽链让狼人获利；多数本身不是证据。",
            "反方假设审计：先假设当前主流站边是错的，寻找至少一条能推翻共识的证据，再决定是否维持原判断。",
        )
        return tasks[(int(seat) - 1) % len(tasks)]

    def _police_join_prompt(self, seat):
        return (self._time_anchor()
                + f"【{self.phase}·警长竞选-是否上警】当前存活：{self.alive_str()}。\n"
                + "警长每天投票算1.5票、可归票带队、出局时可移交/撕毁警徽——好人的指挥位，也是狼人想抢的位置。\n"
                + f"{self._role_playbook(seat)}\n请决定你（{seat}号）是否上警竞选警长。\n"
                + "输出严格JSON：{\"reasoning\":\"真实盘算\",\"speech\":\"简述是否上警及理由(50-120字)\",\"join_police\":true或false}。"
                + self._wolf_explode_line(seat))

    def _police_speech_prompt(self, seat, pol_speeches):
        spoken = "\n".join(f"{s}号：{sp}" for s, sp in pol_speeches) or "（你是第一个上警发言的人）"
        return (self._time_anchor()
                + f"【{self.phase}·警长竞选-上警发言】你（{seat}号）正在竞选警长。{self._role_playbook(seat)}\n"
                + f"当前存活：{self.alive_str()}。已上警发言：\n{spoken}\n"
                + "请发表竞选警长发言：展现视角与找狼逻辑，争取警下玩家把警徽投给你。speech输出竞选发言(100-200字)。"
                + self._wolf_explode_line(seat))

    def _withdraw_prompt(self, seat):
        return (self._time_anchor()
                + f"【{self.phase}·警长竞选-退水】你（{seat}号）在警上。本环节【不发言】，只做一个决定：是否退水。\n"
                + "退水=退出竞选且【失去警下投票权】；不退水=继续留在警上等候警下投票。\n"
                + "只输出严格JSON：{\"reasoning\":\"你退或不退的真实考量\",\"speech\":\"一句话表态(退水/不退水)\",\"withdraw\":true或false}。"
                + self._wolf_explode_line(seat))

    def _police_vote_prompt(self, seat, remaining):
        cand = "、".join(f"{c}号" for c in remaining)
        # ★防降智：必须把【上警/PK发言】喂给投票人，否则好人没有依据只能随机盲投。
        spoken = "\n".join(f"{s}号：{sp}" for s, sp in (self.election_speeches or [])) or "（暂无上警发言记录）"
        if self.roles[seat].camp == "狼人":
            camp_guidance = (
                "你是狼人阵营，可依据狼队计划争取让最有利于狼队的候选人当选；"
                "不要在 reasoning 中遗忘真实队友。"
            )
        else:
            camp_guidance = (
                f"【你的独立审计职责】{self._good_audit_brief(seat)}\n"
                "必须先分别写出你最支持候选人的一条证据和一条反证，再作独立决定。"
                "不得只因候选人发言强势、查杀力度大、警下多数或别人声援就投票。"
            )
        return (self._time_anchor()
                + f"【{self.phase}·警下盲投选警长】你（{seat}号）是警下投票人，请在仍在警上的候选人【{cand}】中选一位当警长。\n"
                + f"【上警发言回顾·务必据此判断，不要随机乱投】\n{spoken}\n"
                + "请根据【谁的发言更像真神、逻辑更扎实、找狼更准】把警徽投给最可信的好人候选人，"
                + "并警惕【悍跳狼往往比真神更凶】、靠表演骗警徽。\n"
                + "⚠️【别把警徽送给狼】：警惕【几个人在警下抱团硬顶/猛冲某个候选人(尤其是疑似真预言家)】——"
                + "那往往是狼队在帮悍跳狼抬轿；一旦让狼拿到警长，它就能用1.5票归票把【真预言家】冲出去。"
                + f"宁可投逻辑最稳的那个，也别盲从一边倒的风向。\n{camp_guidance}\n"
                + "匿名盲投，看不到别人投谁。只输出严格JSON：{\"reasoning\":\"事实核对、反方证据与独立结论\",\"vote_target\":X}，"
                + "X为你支持当警长的候选人座位号(必须是上面候选人之一)。")

    def _police_pk_prompt(self, seat, pk_speeches, tied):
        others = "、".join(f"{c}号" for c in tied if c != seat)
        spoken = "\n".join(f"{s}号：{sp}" for s, sp in pk_speeches) or "（你是第一个 PK 发言的人）"
        return (self._time_anchor()
                + f"【{self.phase}·警长竞选·PK发言】你（{seat}号）在警长竞选中与 {others} 平票，现在进入 PK 发言环节，请再次争取警徽。\n"
                + f"{self._role_playbook(seat)}\n当前存活：{self.alive_str()}。\n本轮 PK 已发言：\n{spoken}\n"
                + "请用更强的视角/找狼逻辑/票型分析说服警下玩家把警徽投给你。speech输出 PK 发言(100-200字)。"
                + self._wolf_explode_line(seat))

    def _consolidation_prompt(self, seat, today_speeches, order=None):
        spoken = "\n".join(f"{s}号：{sp}" for s, sp in today_speeches) or "（无）"
        position = self._position_context(seat, order or self.alive_seats())
        if self.sheriff == seat:
            who = "你是【警长】，由你提出归票建议（你的票算1.5票，但其他玩家仍会独立核验证据）"
        else:
            who = "你是今天最后一个发言的人（场上无警长），由你负责【归票】"
        return (self._time_anchor()
                + f"【{self.phase}·归票】{who}。\n"
                + f"{position}\n当前存活：{self.alive_str()}。\n今天全场发言：\n{spoken}\n"
                + "决策优先级：①公开发动的猎枪/骑士决斗等【机械铁证】及其造成的身份矛盾；"
                + "②经过票型与前后逻辑验证后的可信预言家查杀；③今天最像狼的人；"
                + "④警徽和多数意见只能在证据相当时辅助决胜，绝不是身份认证。"
                + "归票前必须写出一条可核验的证据，不能以‘大家都投他’作为理由；"
                + f"⚠️绝不能归票你自己({seat}号)。详细分析写进 reasoning。\n"
                + "⚠️【极简归票·硬性要求】speech 字段【最多一句话、不超过15字】，只报要打飞的座位号，"
                + "例如『归票7号』或『投7号』——【严禁】长篇分析、严禁复述发言，否则视为违规！"
                + self._wolf_explode_line(seat))

    def _day_speech_prompt(self, seat, today_speeches, deaths, peace_first, order=None):
        spoken = "\n".join(f"{s}号说：{sp}" for s, sp in today_speeches) or "（你是今天第一个发言的人）"
        position = self._position_context(seat, order or self.alive_seats())
        if deaths:
            dead = "、".join(f"{s}号" for s in sorted(deaths))
            death_line = f"昨夜死亡：{dead}（系夜间死亡，死因不公开，不要臆测死法）。"
        elif peace_first:
            death_line = ("【重要】昨晚是平安夜，没有任何人死亡，因此没有遗言环节，"
                          "现在直接自由发言。请勿臆想有人死亡或有人留过遗言。")
        else:
            death_line = "昨夜是平安夜，无人死亡。"
        guidance = (
            "依据本人合法技能结果、公开机制事实、公开声明、行为假说的顺序判断；声明未必为真。"
            "引用公开事件或原话，说明一条主要依据和一个可能反例，再表明倾向。"
            "抱团、孤立、改口和警徽都不能单独定狼；获得新证据可以纠错，也允许合理合作。"
            "公开发动猎枪者是真猎人，技能事实优先于口头身份；自爆只确认本人。"
            "机制核验：守卫守中狼刀/狼刀空刀，只代表狼刀没有造成死亡，不等于整夜无人死亡；女巫毒药可独立造成死亡。"
            "因此“守卫守4号、狼刀4号被挡、女巫毒3号”与公开死讯“3号夜死”完全自洽，不得称为机械矛盾或平安夜。"
            "只有规则或已公开事实确实互斥时，才能指称逻辑矛盾。"
            "技术性未提交不是主动弃权，不得用作躲表态或阵营证据。"
            "狼人只按伪装身份可知道的信息公开发言；私密刀口不可说成全场已知。"
        )
        audit = ""
        if self.roles[seat].camp != "狼人":
            audit = f"\n【你本轮的独立审计职责】{self._good_audit_brief(seat)}"
        return (self._time_anchor()
                + f"【{self.phase}·发言阶段】{self._role_playbook(seat)}\n"
                + f"{death_line}\n{position}\n当前存活：{self.alive_str()}。\n今天已发言记录：\n{spoken}\n"
                + f"{guidance}{audit}\n现在轮到你（{seat}号）发言，speech输出公开发言(100-200字)，切勿暴露reasoning。"
                + self._wolf_explode_line(seat))

    def _vote_prompt(self, seat, today_speeches, consolidation_target=None, order=None):
        spoken = "\n".join(f"{s}号：{sp}" for s, sp in today_speeches) or "（今日无发言记录）"
        position = self._position_context(seat, order or self.alive_seats())
        rec = (f"公开归票建议为【{consolidation_target}号】，这只是待验证意见，不是命令。"
               if consolidation_target else "本轮没有公开归票建议。")
        sheriff_note = f"当前警长 {self.sheriff}号 的票算1.5票。" if self.sheriff else ""
        if self.roles[seat].camp == "狼人":   # ★V6.4：按 camp，白狼王也走狼人投票口径
            camp_hint = "【狼人】与狼队默契把票投向同一个好人/神职制造冤杀，别太刻意暴露。"
        else:
            # 好人合作：先独立依据证据定案，再集中；警长/多数不能覆盖机械铁证或本人底牌。
            camp_hint = (
                f"【好人投票】{rec}\n"
                "先核对本人技能结果和公开结算事实，再评估公开声明，最后才用行为假说。"
                "多数票、警徽、抱团、孤立及改口不能单独定阵营；允许有依据的合作与纠错。"
                "机制核验：守卫挡住狼刀不代表平安夜；女巫毒杀可独立造成夜死。守4号挡刀、毒3号且3号夜死是自洽时间线，不是机械矛盾。"
                "选择最怀疑的合法活人，给出一条可核验依据和一个主要反例，依据不足可主动弃票。"
                "技术失票、人工跳过不代表玩家躲表态。自爆只确认本人，不认证任何相关人的身份。"
                "reasoning 用简短依据和结论，不必强制三段长文。"
            )
        return (self._time_anchor()
                + f"【{self.phase}·投票阶段】现在是投票阶段，不要发言。{sheriff_note}\n"
                + f"{position}\n当前存活：{self.alive_str()}。\n今天全场发言：\n{spoken}\n"
                + f"这是匿名同时投票，你看不到别人投给谁。{camp_hint}\n"
                + "只输出严格JSON：{\"reasoning\":\"判断依据\",\"vote_target\":X}，X为放逐目标座位号，弃票填0。")


from engine.runtime_version import register_source as _register_source
_register_source(__name__, __file__)
