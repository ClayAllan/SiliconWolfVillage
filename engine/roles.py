# -*- coding: utf-8 -*-
"""
roles.py —— 角色多态 (Role Polymorphism) 层（V6.3 重构）
=======================================================
把原先 game_engine.py 里写死的 `if role == ...` 解耦成角色类，便于后续扩展变体板子
（白狼王 / 丘比特等）。

核心契约（铁律不破）：
  · Role 是【纯对象】：只负责「造 prompt / 解析模型 JSON / 改 engine.night 或自身技能属性」，
    【绝不含 yield、绝不调用 engine._ask】——所有 yield / 思考高亮仍由 GameEngine 持有。
  · 引擎在夜晚遍历角色：has_night_action() 为真则反复 pull get_night_prompt()（返回 None 即结束），
    每个子步骤由【引擎】执行 yield from self._ask(...)，拿到响应后回调 parse_night_response()。
  · 技能数据下沉到角色实例（女巫解药/毒药、守卫上次守护、预言家查验、猎人枪）；engine.skills 变兼容视图。
  · 临终技能（猎人开枪）走 has_death_action()/get_death_prompt()/parse_death_response() 事件回调，
    引擎在回调后【统一 check_winner()】，熔断铁律更集中、更稳。

依赖方向：roles.py 只依赖标准库（re/random），不 import game_engine → 无循环依赖；
engine 以参数注入（传 self）供角色读取局势（alive_str/phase/day/night/players/manager 等）。
"""
import random
import re


# ============================================================
# 文本解析工具（从 game_engine 平移；解析模型自然语言响应）
# ============================================================
def _all_numbers(text):
    return [int(n) for n in re.findall(r"(?<!\d)\d{1,2}(?!\d)", text)]


def parse_seat(text, candidates, fallback=True):
    cands = set(candidates)
    for n in _all_numbers(text):
        if n in cands:
            return n
    if fallback and cands:
        return random.choice(sorted(cands))
    return None


def parse_yes_save(text):
    text = re.split(r"[。！!；;\n]", str(text).strip(), maxsplit=1)[0]
    if re.search(r"不\s*(救|用|使用|施救)", text):
        return False
    return bool(re.search(r"(救|用解药|使用解药|解药|施救)", text))


def parse_poison(text, candidates):
    if re.search(r"不\s*(毒|用毒|使用毒)", text):
        return None
    cands = set(candidates)
    m = re.search(r"毒[^\d]{0,4}(\d{1,2})", text)
    if m and int(m.group(1)) in cands:
        return int(m.group(1))
    return None


def parse_hunter(text, candidates):
    if re.search(r"不\s*开枪|放弃开枪|不开|弃枪", text):
        return None
    cands = set(candidates)
    m = re.search(r"(开枪|带走|枪杀|射杀|射|崩)[^\d]{0,4}(\d{1,2})", text)
    if m and int(m.group(2)) in cands:
        return int(m.group(2))
    return None


def _combined(resp):
    """复刻引擎 _combined_text：speech + reasoning，供需要从自然语言抽座位的角色解析。"""
    resp = resp or {}
    return f"{resp.get('speech', '')} {resp.get('reasoning', '')}"


def _parse_action_seat(resp, candidates):
    """新协议只使用结构目标；兼容旧存档时仅从首句取号，绝不猜推理中的号码。"""
    resp = resp or {}
    if resp.get("_is_fallback"):
        return None
    if "vote_target" in resp:
        target = resp["vote_target"]
        return target if target in candidates else None
    first = re.split(r"[。！!；;\n]", resp.get("speech", ""), maxsplit=1)[0]
    return parse_seat(first, candidates, fallback=False)


# ============================================================
# 角色基类
# ============================================================
class BaseRole:
    role_name = "未知"
    camp = "好人"
    emoji = "❓"
    is_god = True

    def __init__(self, seat):
        self.seat = seat

    # —— 身份文本（替代 _private_memory / _role_playbook）——
    def get_private_memory(self, engine):
        return "你是平民(好人)。没有技能但有一票，靠独立核验发言、事实和票型找狼；确认可信带队者后再协同投票，发现错误共识要及时纠正。"

    def get_playbook_prompt(self, engine):
        return ""

    # —— 夜晚“单座位” solo 行动（狼队集体夜杀不走这里）——
    def has_night_action(self, engine):
        return False

    def reset_night(self):
        pass

    def get_night_prompt(self, engine):
        """返回 (prompt, response_kind, label) 或 None（本夜已无更多子步骤）。"""
        return None

    def parse_night_response(self, engine, resp):
        pass

    # —— 临终技能事件回调（猎人开枪等）——
    def has_death_action(self, engine, cause):
        """cause: 'wolf'（狼刀）/ 'poison'（毒杀）/ 'vote'（放逐）。"""
        return False

    def get_death_prompt(self, engine):
        return None

    def parse_death_response(self, engine, resp):
        pass

    # —— 技能状态快照（engine.skills 兼容视图 & 存档）——
    def skill_state(self):
        return {}

    def load_skill_state(self, data):
        pass


# ============================================================
# 平民 / 狼人（无 solo 夜间行动；狼队夜杀由引擎集体编排）
# ============================================================
class Villager(BaseRole):
    role_name = "平民"
    camp = "好人"
    emoji = "👤"
    is_god = False

    def get_private_memory(self, engine):
        return "你是平民(好人)。没有技能但有一票，靠独立核验发言、事实和票型找狼；确认可信带队者后再协同投票，发现错误共识要及时纠正。"

    def get_playbook_prompt(self, engine):
        return ("你属于【好人阵营·平民】：没技能但有一票，是好人阵营的中坚。\n"
                "【高配操作·代跳扛刀】神职不一定非要自己跳——你(平民)可以主动【穿衣服】(假跳预言家/女巫等)把狼的夜刀引到自己身上、"
                "替真神【挡刀】争取时间，关键轮再【脱衣服退水】并盘清逻辑；这往往比真神硬跳更保人。\n"
                "盲投时要【先独立锁狼、再在证据一致时集中】；绝不能为了不散票就盲从未经验证的警长、金水或多数风向。")


class Werewolf(BaseRole):
    role_name = "狼人"
    camp = "狼人"
    emoji = "🐺"
    is_god = False

    def get_private_memory(self, engine):
        mates = [w for w in engine.wolves if w != self.seat]
        return (f"你是狼人。你的狼队友座位号是：{mates}（他们和你是一伙的）。"
                f"狼队目标：隐藏身份、误导好人、逐个淘汰神职与平民。必要时可自爆(is_explode)打乱节奏或吞警徽。")

    # 自爆纪律(所有狼共用)：别被怀疑就炸、要算实际收益、别坑队友
    _EXPLODE_DISCIPLINE = (
        "\n💥【自爆纪律·切勿盲目自爆】自爆=主动送走一名狼，是把双刃剑，绝不能因为‘被怀疑/被票/上头’就炸！"
        "爆之前必须想清三件事：①【有没有实际作用】——能否吞掉关键查杀/归票与投票、强行换掉真神、打断对狼队致命的信息？"
        "②【会不会反坑队友】——你一爆是坐实了队友清白、还是反而暴露狼坑、让队友地位更糟？"
        "③【对获胜有没有贡献】——爆完狼队是更接近屠边，还是白少一张嘴、优势变劣势？"
        "只有‘收益＞少一狼的代价’时才爆；优势局、无实质威胁、单纯被怀疑→【坚决不爆】，靠发言和倒钩扛过去。")

    def get_playbook_prompt(self, engine):
        shared = (
            "【狼队策略分工】主动判断谁主悍跳/代跳、谁深水倒钩、谁负责压身份/抗推与归票；"
            "不必每局全用，也不要所有狼机械站同一边。"
            "【反向金水】给队友金水可以做身份，但会留下关系链；给好人金水也可借势或为后续甩锅留空间。"
            "判断被金水者时，既盘发金水者的真假，也盘接收者后续是否反水、如何投票及谁获益，不能自动认好。"
            "【警徽链】若自爆/出局狼把警徽交给某人，提前考虑对方是队友接力还是被故意甩锅；"
            "队友不要因接徽就过度保狼，也不要机械接受警徽，应以连贯的公开逻辑解释。"
            "【代跳与传话】规划可自洽的查验时间线、警徽流与退水时机；通过合理站边、质疑、归票传递战术，"
            "但不得捏造已发生的引擎事实，且要预判好人如何反盘。\n"
        )
        if engine.day <= 2:
            return ("你属于【狼人阵营】：以【阵营收益】为唯一目标。可悍跳预言家与真预言家对跳搅浑水；"
                    "若局势需要，也完全可以【悍跳女巫/守卫/猎人】来骗神位、找真神或帮队友躲放逐——一切看收益。"
                    "前后口径务必一致；【处于巨大优势时绝不要随意自爆】白送轮次。\n"
                    "📜【悍跳要像·每天报警徽流】若你悍跳了预言家，就要装得彻底：和真预言家一样【每天发言复述+更新警徽流】、"
                    "稳定给金水/查杀，口径前后一致，别露出‘跳了却不报警徽流’的马脚。"
                    + shared + self._EXPLODE_DISCIPLINE)
        return ("你属于【狼人阵营】，进入中后期、局势渐明。穿衣服(冒充神职)风险变高、易被识破，"
                "请审慎：可深水【倒钩】装平民、抓好人发言漏洞【抗推】、或继续维持已跳神职身份硬刚；"
                "【优势局绝不自爆】，只有在必死或能换取关键收益时才果断自爆。一切以阵营胜利为先、口径一致。"
                + shared + self._EXPLODE_DISCIPLINE)

class WhiteWolfKing(Werewolf):
    """白狼王(V6.4)：狼阵营特权角色，继承狼人全部夜间/阵营行为，额外拥有【白天自爆带人】。
       夜杀仍走狼队集体编排(_run_wolf_pack_night)；带人结算在引擎 _handle_regular_explode(读 explode_target)。"""
    role_name = "白狼王"
    camp = "狼人"
    emoji = "👑"

    def get_private_memory(self, engine):
        return (super().get_private_memory(engine)
                + " 你还是【白狼王】：白天发言阶段可发动自爆并【强行带走一名玩家】"
                  "(在 JSON 输出 is_explode=true 且 explode_target=目标座位号)。")

    def get_playbook_prompt(self, engine):
        return (super().get_playbook_prompt(engine)
                + "\n👑【白狼王专属】你拥有白天自爆带人的恐怖特权。如果在白天你的发言阶段觉得局势不利，"
                "或者想强行换掉神职（特别是真预言家/女巫），你可以在 JSON 中输出 \"is_explode\": true，"
                "并且必须同时输出 \"explode_target\": X（X为你想要带走的玩家座号）。"
                "善用它在关键时刻强行带走真神、打乱好人节奏！")


# ============================================================
# 预言家（每夜查验一人）
# ============================================================
class Seer(BaseRole):
    role_name = "预言家"
    camp = "好人"
    emoji = "🔮"

    def __init__(self, seat):
        super().__init__(seat)
        self.checks = []          # [(target, "狼人"/"好人"), ...]
        self._asked = False

    def get_private_memory(self, engine):
        return "你是预言家(神职/好人核心)。每晚可查验一名玩家真实阵营(好人/狼人)，白天可跳预言家报查验结果带队。"

    def get_playbook_prompt(self, engine):
        return '你是预言家。自己的真实查验是私密硬信息；他人眼中你的公开查验只是声明。出现对跳时说明查验链与可核验矛盾，结合局势选择最有收益的放逐目标；可优先处理查杀狼。跳身份后每日清楚说明累计查验及警徽流，区分实际交徽与未来计划。'

    def has_night_action(self, engine):
        return True

    def reset_night(self):
        self._asked = False

    def get_night_prompt(self, engine):
        if self._asked:
            return None
        self._asked = True
        prompt = (f"【{engine.phase}·预言家行动】当前存活：{engine.alive_str()}。"
                  f"请选择今晚查验阵营的对象，speech明确写‘查验X号’。")
        return (prompt, "check", f"D{engine.day} 夜晚")

    def parse_night_response(self, engine, resp):
        cands = set(engine.alive_seats()) - {self.seat}
        target = _parse_action_seat(resp, cands)   # ★speech 优先，避免 reasoning 噪声号
        # ★V6.4：查验返回【阵营】，按 camp 判定——白狼王(camp=狼人)必须验出"狼人"。
        if target is None:
            return
        result = "狼人" if engine.roles[target].camp == "狼人" else "好人"
        self.checks.append((target, result))
        engine.night["seer_check"] = (target, result)
        engine.manager.inject(self.seat, f"【查验结果】你昨晚查验了 {target}号，真实身份是【{result}】，请牢记用于白天发言。")
        engine._log_god(f"🔮 预言家（{self.seat}号）查验 {target}号 → 【{result}】")

    def skill_state(self):
        return {"checks": [list(c) for c in self.checks]}

    def load_skill_state(self, data):
        self.checks = [tuple(c) for c in ((data or {}).get("checks") or [])]


# ============================================================
# 女巫（解药→毒药 两子步骤；双药互斥；解药仅首夜可自救）
# ============================================================
class Witch(BaseRole):
    role_name = "女巫"
    camp = "好人"
    emoji = "🧪"

    def __init__(self, seat):
        super().__init__(seat)
        self.antidote_available = True      # ★技能数据下沉(原 skills["witch"]["antidote"])
        self.poison_available = True        # ★(原 skills["witch"]["poison"])
        self._used_tonight = False          # 本夜是否已用药(解毒互斥)
        self._asked_anti = False            # 本夜子步骤游标
        self._asked_poison = False
        self._pending = None                # 当前刚问的是哪一步(解析时用)
        self.medicine_history = []          # [(day, action, target)]，不可压缩真实技能记录

    def get_private_memory(self, engine):
        return "你是女巫(神职)。1瓶解药(救被刀者,仅第一夜可自救)+1瓶毒药(毒死一人)，每晚最多用一种(双药互斥)。"

    def get_playbook_prompt(self, engine):
        return '你是女巫。依据自己的药路、合法刀口与公开证词决策；别把药路声明当作他人已验证的事实。毒药可用也可保留，比较误毒风险、剩余轮次与被刀风险，不能因为日期靠后就强制盲毒。已结算技能事实优先；预言家口头查杀仍须判断真假。'

    def has_night_action(self, engine):
        return self.antidote_available or self.poison_available

    def reset_night(self):
        self._used_tonight = False
        self._asked_anti = False
        self._asked_poison = False
        self._pending = None

    def get_night_prompt(self, engine):
        kill = engine.night.get("wolf_kill")
        # —— 子步骤①：解药（有解药 且 今晚有人被刀 且 非“首夜后被刀自救”禁用）——
        if not self._asked_anti:
            self._asked_anti = True
            if self.antidote_available and kill is not None:
                if kill == self.seat and engine.day >= 2:
                    engine._log_god("🧪 女巫被刀，非第一夜无法自救（跳过解药）")
                else:
                    self._pending = "antidote"
                    selfnote = "（正是你自己，首夜可自救）" if kill == self.seat else ""
                    prompt = (f"【{engine.phase}·女巫-解药】今晚 {kill}号 被狼人刀了{selfnote}。你还有一瓶解药。"
                              f"是否使用解药救他？speech明确写‘救’或‘不救’，reasoning写理由。")
                    return (prompt, "antidote", f"D{engine.day} 夜晚")
        # —— 子步骤②：毒药（本晚未用解药 且 有毒药）——
        if not self._asked_poison:
            self._asked_poison = True
            if (not self._used_tonight) and self.poison_available:
                self._pending = "poison"
                warn = ("【警告】今天是第一夜，盲目毒人极易毒死好人神职！除非你有百分百把握，"
                        "否则强烈建议【不使用毒药】！" if engine.day == 1 else "")
                prompt = (f"【{engine.phase}·女巫-毒药】当前存活：{engine.alive_str()}。你还有一瓶毒药"
                          f"（本晚未用解药，可用毒；双药一晚只能用一种）。{warn}是否毒人？speech明确写‘毒X号’或‘不毒’。")
                return (prompt, "poison", f"D{engine.day} 夜晚")
        self._pending = None
        return None

    def parse_night_response(self, engine, resp):
        sp = (resp or {}).get("speech", "")
        if self._pending == "antidote":
            kill = engine.night.get("wolf_kill")
            if (not resp.get("_is_fallback") and self.antidote_available and not self._used_tonight
                    and kill in engine.alive_seats() and (kill != self.seat or engine.day == 1)
                    and (resp["use_antidote"] if "use_antidote" in resp else parse_yes_save(sp))):
                engine.night["witch_save"] = kill
                self.antidote_available = False
                self._used_tonight = True
                self.medicine_history.append((engine.day, "解药", kill))
                engine._log_god(f"🧪 女巫（{self.seat}号）使用【解药】救了 {kill}号")
            else:
                engine._log_god(f"🧪 女巫（{self.seat}号）放弃使用解药")
        elif self._pending == "poison":
            target = (_parse_action_seat(resp, set(engine.alive_seats()) - {self.seat})
                      if "vote_target" in resp else parse_poison(sp, engine.alive_seats()))
            if target is not None and target != self.seat and self.poison_available and not self._used_tonight and not resp.get("_is_fallback"):
                engine.night["witch_poison"] = target
                self.poison_available = False
                self.medicine_history.append((engine.day, "毒药", target))
                engine._log_god(f"🧪 女巫（{self.seat}号）使用【毒药】毒了 {target}号")
            else:
                engine._log_god(f"🧪 女巫（{self.seat}号）放弃使用毒药")

    def skill_state(self):
        return {
            "antidote": self.antidote_available,
            "poison": self.poison_available,
            "history": [list(item) for item in self.medicine_history],
        }

    def load_skill_state(self, data):
        data = data or {}
        self.antidote_available = bool(data.get("antidote", True))
        self.poison_available = bool(data.get("poison", True))
        self.medicine_history = [tuple(item) for item in (data.get("history") or [])]


# ============================================================
# 猎人（无夜间主动行动；被刀/被放逐时临终开枪，毒杀不可开枪）
# ============================================================
class Hunter(BaseRole):
    role_name = "猎人"
    camp = "好人"
    emoji = "🏹"

    def __init__(self, seat):
        super().__init__(seat)
        self.can_shoot = True
        self.shot = None

    def get_private_memory(self, engine):
        return "你是猎人(神职)。被狼刀或被放逐时可开枪带走一人；被女巫毒死时无法开枪。"

    def get_playbook_prompt(self, engine):
        return '你是猎人。被刀或放逐可开枪，被毒不能开枪。仅依据当时合法信息比较目标狼面和误枪代价，证据不足可以不开枪；带头投你或担任警长不等于狼人，不能为了报复开枪。'

    def has_death_action(self, engine, cause):
        return self.can_shoot and cause != "poison"

    def get_death_prompt(self, engine):
        prompt = (engine._time_anchor()
                  + f"【猎人开枪】你（{self.seat}号·猎人）已出局，临死可开枪带走一名【当前存活】的玩家。"
                  + f"当前存活：{engine.alive_str()}。\n"
                  + "⚠️ 警告：绝不可对【已死亡/已出局】的玩家开枪！只能从上面的存活名单里选目标。"
                  + "在speech中明确写出‘开枪X号’，若放弃写‘不开枪’。")
        return (prompt, "hunt", f"D{engine.day} 猎人开枪")

    def parse_death_response(self, engine, resp):
        self.can_shoot = False
        target = _parse_action_seat(resp, set(engine.alive_seats()) - {self.seat})
        if target is not None:
            engine._mark_dead(target, "猎人开枪", check=False)
            self.shot = target
            engine._confirm_role(self.seat, "猎人", "公开发动猎枪")
            # 公开确认的是猎人身份与开枪事实；被带走者的真实身份必须保密到终局。
            engine._log_judge(f"💥 猎人开枪带走了 {target}号！")
            engine._broadcast_event(f"【法官播报】{self.seat}号是猎人，开枪带走了{target}号！")
            engine._add_replay(f"- 🏹 猎人({self.seat}号)开枪带走 {target}号（身份暂不公开）")
        else:
            # ★V6.7+：不开枪 → 【不公开身份】(只上帝视角可见)，避免白白把猎人身份送给好人/狼人
            engine._log_god(f"🕊️ 猎人（{self.seat}号）私下决定【不开枪】，未暴露猎人身份。")

    def skill_state(self):
        return {"can_shoot": self.can_shoot, "shot": self.shot}

    def load_skill_state(self, data):
        data = data or {}
        self.can_shoot = bool(data.get("can_shoot", True))
        self.shot = data.get("shot")


# ============================================================
# 守卫（每夜守一人，可空守；不连守；同守同救会死）
# ============================================================
class Guard(BaseRole):
    role_name = "守卫"
    camp = "好人"
    emoji = "🛡️"

    def __init__(self, seat):
        super().__init__(seat)
        self.last_guarded = None      # ★技能数据下沉(原 skills["guard"]["last"] / guard_last)
        self.guard_history = []       # [(day, target_or_None)]，供不可压缩事实账本使用
        self._asked = False

    def get_private_memory(self, engine):
        return ("你是守卫(神职)。每晚守护一人(可守自己)，不能连续两晚守同一人；"
                "所守之人当晚又被女巫解药救则‘同守同救’会死。")

    def get_playbook_prompt(self, engine):
        return '你是守卫。不能连守同一人，可守自己或空守。比较关键好人被刀风险、女巫可能用药导致同守同救的风险与守护收益；警徽或跳预言家本身不认证身份，也不表示一定不能保护。'

    def has_night_action(self, engine):
        return True

    def reset_night(self):
        self._asked = False

    def get_night_prompt(self, engine):
        if self._asked:
            return None
        self._asked = True
        last = self.last_guarded
        prompt = (f"【{engine.phase}·守卫行动】当前存活：{engine.alive_str()}。"
                  f"你上一晚守护的是：{(str(last) + '号') if last else '无'}"
                  f"（不能连续两晚守同一人）。请选择今晚守护对象（可守自己），speech明确写‘守护X号’。\n"
                  f"⚠️你也可以选择 vote_target: 0 进行【空守】（谁都不守），以保留实力、或规避‘同守同救’导致目标死亡——"
                  f"若要空守，请在 speech 明确写‘空守’，不要写任何座位号。")
        return (prompt, "guard", f"D{engine.day} 夜晚")

    def parse_night_response(self, engine, resp):
        cands = set(engine.alive_seats()) - {self.last_guarded}
        target = _parse_action_seat(resp, cands)
        engine.night["guard_target"] = target
        self.last_guarded = target
        self.guard_history.append((engine.day, target))
        engine._log_god(f"🛡️ 守卫（{self.seat}号）"
                        + ("选择了【空守】(谁都不守)" if target is None else f"守护了 {target}号"))

    def skill_state(self):
        return {"last": self.last_guarded, "history": [list(item) for item in self.guard_history]}

    def load_skill_state(self, data):
        data = data or {}
        self.last_guarded = data.get("last")
        self.guard_history = [tuple(item) for item in (data.get("history") or [])]


# ============================================================
# 守墓人(V6.5)：神职。每晚法官私下告知昨日被放逐者的真实阵营(信息透传)
# ============================================================
class GraveKeeper(BaseRole):
    role_name = "守墓人"
    camp = "好人"
    emoji = "🪦"

    def __init__(self, seat):
        super().__init__(seat)
        self._last_seen = None    # 已被告知的最近放逐座位(防重复告知)
        self._asked = False
        self.results = []

    def get_private_memory(self, engine):
        return ("你是守墓人(神职/好人)。每天夜里法官会【私下告诉你】昨天白天被公投放逐出局的那个人是好人还是狼人，"
                "这是你独有的硬信息，帮你和好人锁定身份。")

    def get_playbook_prompt(self, engine):
        return ("你属于【好人阵营·守墓人】：你掌握【昨日被放逐者的真实阵营】这一硬信息！"
                "白天大胆用它带队——若昨天被票出去的是狼，说明好人方向对了、可顺藤摸瓜；若是好人，"
                "说明前一天被狼带了节奏、必须反思纠偏。适时跳守墓人公布墓地结果能极大帮助好人，但也会成为狼的刀靶，注意时机。")

    def has_night_action(self, engine):
        # 仅当存在【尚未告知过】的昨日放逐者时才唤醒
        return engine.last_exiled_seat is not None and engine.last_exiled_seat != self._last_seen

    def reset_night(self):
        self._asked = False

    def get_night_prompt(self, engine):
        if self._asked:
            return None
        self._asked = True
        seat, camp = engine.last_exiled_seat, engine.last_exiled_camp
        prompt = (f"【{engine.phase}·守墓人验墓】法官私下告知你：昨天白天被公投放逐出局的 {seat}号，"
                  f"真实阵营是【{camp}】。请牢记此硬信息用于白天带队。回复 speech:\"已确认\" 即可。")
        return (prompt, "ack", f"D{engine.day} 守墓")

    def parse_night_response(self, engine, resp):
        seat, camp = engine.last_exiled_seat, engine.last_exiled_camp
        self._last_seen = seat
        self.results.append((engine.day, seat, camp))
        engine.manager.inject(self.seat, f"【守墓结果·硬信息】你已确认：昨天被放逐出局的 {seat}号 真实阵营是【{camp}】。")
        engine._log_god(f"🪦 守墓人（{self.seat}号）获知 {seat}号 真实阵营是【{camp}】")

    def skill_state(self):
        return {"last_seen": self._last_seen, "results": [list(r) for r in self.results]}

    def load_skill_state(self, data):
        self._last_seen = (data or {}).get("last_seen")
        self.results = [tuple(r) for r in (data or {}).get("results", [])]


# ============================================================
# 骑士(V6.5)：神职。一次性【决斗】——白天发言时拦截结算(详见 game_engine._handle_knight_duel)
# ============================================================
class Knight(BaseRole):
    role_name = "骑士"
    camp = "好人"
    emoji = "🐎"

    def __init__(self, seat):
        super().__init__(seat)
        self.has_dueled = False   # 一次性技能，无论成败用掉就没了

    def get_private_memory(self, engine):
        return ("你是骑士(神职/好人)。你有【一次】决斗机会：白天发言时可指定一人决斗——"
                "若对方是狼，对方当场死亡且白天立即结束入夜；若对方是好人，你以死谢罪、白天继续。")

    def get_playbook_prompt(self, engine):
        return '你是骑士，一次性白天决斗：中狼则狼死，中好人则你死。比较立即行动与继续听发言的收益；证据不足可保留技能。游戏未结束、他人获益或警徽流位置都不能单独证明阵营。'

    # 决斗在引擎白天发言循环里拦截(_duel_requested/_handle_knight_duel)；骑士无夜间主动行动。
    def skill_state(self):
        return {"has_dueled": self.has_dueled}

    def load_skill_state(self, data):
        self.has_dueled = bool((data or {}).get("has_dueled", False))


# ============================================================
# 狼美人(V6.5)：狼阵营。夜里随狼队杀人后单独魅惑一人；自己一死，被魅者立刻殉情陪葬
# ============================================================
class WolfBeauty(Werewolf):
    role_name = "狼美人"
    camp = "狼人"
    emoji = "💋"

    def __init__(self, seat):
        super().__init__(seat)
        self.charm_target = None   # 最近魅惑的目标(不能连魅同一人；殉情看它)
        self._asked = False

    def get_private_memory(self, engine):
        return (super().get_private_memory(engine)
                + " 你还是【狼美人】：每晚狼队杀人后你单独醒来【魅惑】一名玩家(不能连魅同一人、不能魅惑自己)；"
                  "当你因【任何原因】死亡时，被你魅惑的人会立刻【殉情】陪葬。")

    def get_playbook_prompt(self, engine):
        return (super().get_playbook_prompt(engine)
                + "\n💋【狼美人专属】你每晚魅惑一人，一旦你死了 ta 就陪葬——你是好人最忌惮的‘连环炸弹’。"
                "魅惑要挑【关键神职或铁好人】，让自己临死也能拉一个垫背；白天注意隐藏，别轻易暴露狼美人身份。"
                "\n🚫【狼美人无自爆权】你【没有自爆技能】，绝不可自爆(系统会直接忽略你的自爆)；上面那段‘自爆纪律’不适用于你。"
                "你的杀伤全在【魅惑链殉情】——好好活着、把垫背目标钉死，比任何自爆都值钱。")

    def has_night_action(self, engine):
        return True

    def reset_night(self):
        self._asked = False

    def get_night_prompt(self, engine):
        if self._asked:
            return None
        self._asked = True
        last = self.charm_target
        prompt = (f"【{engine.phase}·狼美人魅惑】狼队已行动完毕，你单独醒来魅惑一人(她/他将在你死亡时殉情陪葬)。"
                  f"当前存活：{engine.alive_str()}。不能魅惑你自己({self.seat}号)"
                  + (f"，也不能连魅上次的 {last}号" if last else "")
                  + "。speech 明确写‘魅惑X号’。")
        return (prompt, "charm", f"D{engine.day} 夜晚")

    def parse_night_response(self, engine, resp):
        cands = set(engine.alive_seats()) - {self.seat}
        if self.charm_target in cands:
            cands.discard(self.charm_target)   # 不能连魅同一人
        target = _parse_action_seat(resp, cands)   # ★speech 优先
        self.charm_target = target
        engine.charm_target = target           # ★引擎维护当前魅惑链(殉情结算用)
        engine._log_god(f"💋 狼美人（{self.seat}号）魅惑了 {target}号")

    def skill_state(self):
        return {"charm_target": self.charm_target}

    def load_skill_state(self, data):
        self.charm_target = (data or {}).get("charm_target")


# ============================================================
# 丘比特(V6.6)：神职。开局之夜连定一对情侣，一方死则另一方殉情同死(纯双死，不设独立阵营)
# ============================================================
class Cupid(BaseRole):
    role_name = "丘比特"
    camp = "好人"
    emoji = "💘"

    def get_private_memory(self, engine):
        return ("你是丘比特(神职)。开局之夜你连定【一对情侣】(可任选两人，也可【自连】=你自己与另一人)；"
                "此后情侣一方死亡、另一方立刻殉情同死。\n"
                "【你的阵营随情侣而定】：情侣同阵营(都好/都狼)或你自连了好人 → 你属【好人阵营】(放逐所有狼即胜)；"
                "情侣是【一好一狼】、或你自连了狼 → 你与情侣组成【情侣第三方】(需活到最后、放逐所有非情侣方才算赢)。")

    def get_playbook_prompt(self, engine):
        return ("你属于【丘比特】：开局你连定了一对情侣(你知道是谁、但未必知道其阵营)，情侣一方死另一方殉情。\n"
                "🎯【阵营规则·牢记】①情侣同阵营 或 你自连好人 → 你是【好人阵营】，放逐所有狼即胜；"
                "②情侣是【一好一狼】或你自连了狼 → 你和情侣组成【情侣第三方】，需活到最后(清掉所有非情侣方)才算情侣胜。\n"
                "📋【打法】开局你不知情侣阵营，建议【上警争警徽】拿话语权；首要任务是【听情侣发言判断其阵营】：\n"
                "· 情侣是【狼狼】→ 第一时间跳丘比特、点出这两匹狼，帮好人快速排雷取胜；\n"
                "· 情侣是【人人】→ 务必【隐藏】他们身份，别让狼夜里刀一个赔一双；\n"
                "· 情侣是【人狼恋】(第三方)→ 想办法暗示情侣‘我是丘比特’，让作为情侣的狼【别刀情侣、指刀外人】，"
                "和情侣联手把好人与其余狼逐个清掉，最终情侣三方通吃。")

    # 连线是【开局一次性特殊阶段】(见引擎 _run_cupid_link)，丘比特无常规夜间行动/技能态。
    def get_link_prompt(self, engine):
        return (f"【开局·丘比特连线】你是丘比特，请连定一对【情侣】：可任选两人，也可【自连】(把你自己 {self.seat}号 与另一人连成情侣)。"
                f"情侣一方死、另一方殉情；⚠️若连成【一好一狼】，你与情侣会变成【情侣第三方】。"
                f"当前在座：{engine.alive_str()}。speech 明确写出两个座位号(可含你自己)，例如‘情侣：{self.seat}号和7号’ 或 ‘情侣：3号和7号’。")

    def parse_link_response(self, engine, resp):
        nums = (resp or {}).get("targets", [])
        if len(nums) != 2 or len(set(nums)) != 2 or any(n not in engine.alive_seats() for n in nums):
            return
        a, b = nums
        engine.lovers = (a, b)
        _rule = ("（一方死、另一方立刻殉情同死。⚠️若你俩恰是【一好一狼】，则你俩连同丘比特将组成【情侣第三方】，"
                 "要活到最后、清掉所有非情侣方才算情侣胜——请尽快从发言判断对方阵营，同阵营则互保、跨阵营则联手清场。）")
        engine.manager.inject(a, f"【丘比特连线】你和 {b}号 结为情侣。{_rule}")
        engine.manager.inject(b, f"【丘比特连线】你和 {a}号 结为情侣。{_rule}")
        engine.manager.inject(self.seat, f"【你的连线】你(丘比特)把 {a}号 和 {b}号 连成了情侣。"
                              "请判断他们阵营来确定你的归属：同阵营/自连好人→好人向；一好一狼/自连狼→情侣第三方。")
        engine._log_god(f"💘 丘比特({self.seat}号) 连定情侣：{a}号 ❤️ {b}号")
        engine._add_replay(f"- 💘 丘比特连线：{a}号 ❤️ {b}号 结为情侣")


# ============================================================
# 工厂：身份名 → 角色实例
# ============================================================

# ============================================================
# 摄梦人(V7.0)：神职。每晚必须选择一名玩家成为“梦游者”
#   · 梦游者当夜免疫狼刀伤害；
#   · 摄梦人若夜间死亡，梦游者随之一同殉情出局；
#   · 连续两晚摄梦同一人，该玩家在第二晚暴毙。
# ============================================================
class Dreamer(BaseRole):
    role_name = "摄梦人"
    camp = "好人"
    emoji = "🌙"

    def __init__(self, seat):
        super().__init__(seat)
        self.dream_target = None
        self.dream_history = []
        self._asked = False

    def get_private_memory(self, engine):
        return ("你是摄梦人(神职/好人)。每晚必须选择一名玩家成为【梦游者】(不可空选)。"
                "被摄梦者当晚免疫狼刀伤害；若摄梦人夜间死亡，梦游者随之一同殉情出局；"
                "若连续两晚摄梦同一人，该玩家将在第二晚暴毙。")

    def get_playbook_prompt(self, engine):
        return ("你属于【好人阵营·摄梦人】：你拥有强大的守护与致命连带技能。每晚摄梦一人，可保护关键神职免受狼刀，但注意："
                "①千万不要连续两晚摄梦同一位好人，否则会将其直接梦毙；"
                "②如果你被狼刀或被毒，你当晚的梦游者也会一起死亡，因此要隐藏好自己；"
                "③到了残局，可通过连续两晚摄梦狼人来直接击杀狼人。")

    def has_night_action(self, engine):
        return True

    def reset_night(self):
        self._asked = False

    def get_night_prompt(self, engine):
        if self._asked:
            return None
        self._asked = True
        last = self.dream_target
        prompt = (f"【{engine.phase}·摄梦人行动】当前存活：{engine.alive_str()}。"
                  f"请选择今晚摄梦成为梦游者的对象(不能选你自己{self.seat}号"
                  + (f"，若继续选上次的 {last}号 ta 将在明早暴毙" if last else "")
                  + ")。speech 明确写‘摄梦X号’。")
        return (prompt, "dream", f"D{engine.day} 夜晚")

    def parse_night_response(self, engine, resp):
        cands = set(engine.alive_seats()) - {self.seat}
        target = _parse_action_seat(resp, cands)
        self.dream_history.append(target)
        self.dream_target = target
        engine.night["dream_target"] = target
        engine.manager.inject(self.seat, f"【摄梦结果】你今晚摄梦了 {target}号 玩家。")
        engine._log_god(f"🌙 摄梦人（{self.seat}号）摄梦了 {target}号")

    def skill_state(self):
        return {"dream_target": self.dream_target, "history": list(self.dream_history)}

    def load_skill_state(self, data):
        self.dream_target = (data or {}).get("dream_target")
        self.dream_history = list((data or {}).get("history") or [])


# ============================================================
# 恶灵骑士(V7.0)：狼阵营特权牌。
#   · 夜间不与狼人分开行动，不能自爆；
#   · 夜间不能被狼刀杀死；
#   · 拥有一次性反伤盾：若被预言家查验或被女巫毒杀，恶灵骑士不死亡，反而反伤该神职致其死亡。
# ============================================================
class GhostRider(Werewolf):
    role_name = "恶灵骑士"
    camp = "狼人"
    emoji = "💀"

    def __init__(self, seat):
        super().__init__(seat)
        self.has_reflected = False

    def get_private_memory(self, engine):
        return (super().get_private_memory(engine)
                + " 你还是【恶灵骑士】：狼队特权底牌！你【不可自爆】，夜间【不能被狼刀杀死】。"
                  "你拥有一张【一次性反伤盾】：若夜间被预言家查验或被女巫毒杀，你不会受到伤害，反而会立刻【反伤该神职致其死亡】！")

    def get_playbook_prompt(self, engine):
        return (super().get_playbook_prompt(engine)
                + "\n💀【恶灵骑士专属】你是狼队的反伤利刃。①你没有自爆权，不可自爆；"
                "②夜间被狼人刀中也免疫死亡；"
                "③若预言家验你或女巫毒你，反伤触发，他们直接暴毙而你安然无恙（一次性反伤）。"
                "在白天可以大胆做作或者穿神职衣服吸引真神查验/下毒，借刀杀神！")

    def skill_state(self):
        return {"has_reflected": self.has_reflected}

    def load_skill_state(self, data):
        self.has_reflected = bool((data or {}).get("has_reflected", False))


# ============================================================
# 魔术师(V7.0)：神职。每晚在所有人行动前交换两人号码牌(当夜生效)
# ============================================================
class Magician(BaseRole):
    role_name = "魔术师"
    camp = "好人"
    emoji = "🎩"

    def __init__(self, seat):
        super().__init__(seat)
        self.swapped_history = []
        self.current_swap = None
        self._asked = False

    def get_private_memory(self, engine):
        return ("你是魔术师(神职/好人)。每晚在所有人行动前，你可以选择交换两名玩家的号码牌(当夜生效)。"
                "当晚狼刀、查验、用药、守护等所有技能目标都将按交换后的位置转移。")

    def get_playbook_prompt(self, engine):
        return ("你属于【好人阵营·魔术师】：你掌握移形换影的魔法！每晚在所有人行动前交换两个座位的号码牌。"
                "例如预料狼人要刀预言家，可将预言家与狼人或铁好人交换，使狼刀移位。发言时注意适时公布换人记录帮助好人复盘夜间逻辑。")

    def has_night_action(self, engine):
        return True

    def reset_night(self):
        self._asked = False
        self.current_swap = None

    def get_night_prompt(self, engine):
        if self._asked:
            return None
        self._asked = True
        prompt = (f"【{engine.phase}·魔术师行动】当前存活：{engine.alive_str()}。"
                  f"请选择今晚交换号码牌的两位玩家(例如‘交换3号和8号’)。所有人的夜间技能将移位。")
        return (prompt, "swap", f"D{engine.day} 夜晚")

    def parse_night_response(self, engine, resp):
        nums = (resp or {}).get("targets", [])
        if len(nums) != 2 or len(set(nums)) != 2 or any(n not in engine.alive_seats() for n in nums):
            return
        if len(nums) >= 2:
            a, b = nums[0], nums[1]
            self.current_swap = (a, b)
            self.swapped_history.append((engine.day, a, b))
            engine.night["magician_swap"] = (a, b)
            engine._log_god(f"🎩 魔术师（{self.seat}号）交换了 {a}号 与 {b}号 的号码牌！")
        else:
            self.current_swap = None
            engine._log_god(f"🎩 魔术师（{self.seat}号）今夜未执行有效交换。")

    def skill_state(self):
        return {"swapped_history": list(self.swapped_history), "current_swap": self.current_swap}

    def load_skill_state(self, data):
        self.swapped_history = list((data or {}).get("swapped_history") or [])
        self.current_swap = (data or {}).get("current_swap")

ROLE_CLASSES = {
    "平民": Villager, "狼人": Werewolf, "白狼王": WhiteWolfKing, "狼美人": WolfBeauty,
    "预言家": Seer, "女巫": Witch, "猎人": Hunter, "守卫": Guard,
    "守墓人": GraveKeeper, "骑士": Knight, "丘比特": Cupid,
    "摄梦人": Dreamer, "恶灵骑士": GhostRider, "魔术师": Magician,
}


def create_role(role_name, seat):
    if role_name not in ROLE_CLASSES:
        raise ValueError(f"未知角色：{role_name}")
    return ROLE_CLASSES[role_name](seat)


def validate_board_roles(board_roles):
    """在发牌或清空记忆之前拒绝未知角色及无法进行屠边的板子。"""
    names = list(board_roles)
    if len(names) != 12:
        raise ValueError("游戏板子必须恰好包含 12 个角色")
    unknown = sorted(set(names) - ROLE_CLASSES.keys())
    if unknown:
        raise ValueError("游戏板子含未知角色：" + "、".join(unknown))
    classes = [ROLE_CLASSES[name] for name in names]
    if not any(cls.camp == "狼人" for cls in classes):
        raise ValueError("游戏板子必须包含狼人")
    if "平民" not in names or not any(cls.is_god for cls in classes):
        raise ValueError("屠边板子必须同时包含神职和平民")
    for name in set(names):
        if ROLE_CLASSES[name].is_god and names.count(name) > 1:
            raise ValueError(f"当前规则不支持重复神职：{name}")
    return names


from engine.runtime_version import register_source as _register_source
_register_source(__name__, __file__)
