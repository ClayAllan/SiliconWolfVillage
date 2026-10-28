# -*- coding: utf-8 -*-
"""
ai_agent.py —— AI 玩家接口与大脑（V3.0）
========================================
职责：
  1) 为 12 个 AI 各自维护独立对话上下文（记忆）。
  2) 初始化时注入“身份/风格/私密信息”+ 高配面杀规则的 System Prompt。
  3) 封装大模型调用：强制 JSON、硬超时掐断、模型自动回退、按需兜底。

V3.0 变化：
  - System Prompt 写入：身份名单约束、警长1.5票特权、女巫面杀规则、狼人 is_explode 自爆格式。
  - 统一返回结构新增字段：vote_target / is_explode / badge_target / join_police / withdraw，
    缺省都给安全默认值，引擎按当轮场景各取所需，不会 KeyError。
  - 防卡死机制保持不变：timeout=AGENT_TIMEOUT、client max_retries=0、超时即兜底不重试。
"""

import json
import copy
import os
import re
import threading
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

from engine import config
from engine.roles import ROLE_CLASSES   # 仅取各身份的 camp(类属性)做阵营路由；roles 不反向 import 本模块
from agent.model_protocol import (ERROR_LABELS, ResponseProtocolError, provider_for, validate_base_url,
                            output_budget, request_options, message_metadata, validate_content,
                            parse_action, error_category, action_instruction)
from agent.public_memory import select_evidence


def _positive_env_int(name, default):
    try:
        return max(1, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return default


# A process-wide cap prevents each Streamlit session from multiplying outbound model traffic.
_MODEL_REQUEST_LIMIT = _positive_env_int("SWV_MAX_CONCURRENT_MODEL_REQUESTS", 24)
_MODEL_REQUEST_SLOTS = threading.BoundedSemaphore(_MODEL_REQUEST_LIMIT)


ROLE_RULE_TEXT = {
    "预言家": "预言家每夜查验一人阵营；其公开查验只是身份声明，只有先判断其真假后才能把结果当信息。",
    "女巫": "女巫有一瓶解药和一瓶毒药，每晚最多使用一种；解药仅第一夜可以自救。",
    "猎人": "猎人被刀或被放逐时可以开枪，被毒不能开枪；公开发动猎枪可机械确认开枪者是真猎人。",
    "守卫": "守卫每夜可守一人、不能连续守同一人；同守同救会导致目标死亡。",
    "守墓人": "守墓人每夜获知上一名被放逐者的真实阵营。",
    "骑士": "骑士白天可决斗一次：决斗狼则狼死，决斗好人则骑士死亡；成功决斗可机械确认骑士身份。",
    "丘比特": "丘比特首夜连接情侣；情侣一方死亡会导致另一方殉情。",
    "白狼王": "白狼王白天自爆可以同时带走一名玩家。",
    "狼美人": "狼美人夜间魅惑一人；狼美人死亡会使当夜魅惑目标殉情。",
    "摄梦人": "摄梦人每晚摄梦一人成为梦游者，免疫当晚狼刀；若连续两晚摄同一人或摄梦人夜死则梦游者死亡。",
    "恶灵骑士": "恶灵骑士不可自爆、夜死免疫，拥有一张反伤盾，若被预言家查验或女巫毒杀则反伤该神职致其死亡。",
    "魔术师": "魔术师每晚交换两人号码牌，当晚所有夜间技能目标移位。",
}


def _role_rules_for_roster(roster: str) -> str:
    """只注入本局真实存在的角色规则，避免用不存在身份污染模型上下文。"""
    lines = [text for role, text in ROLE_RULE_TEXT.items() if role in str(roster or "")]
    return "\n".join(f"- {line}" for line in lines) or "- 本局没有额外神职技能。"

# ------------------------------------------------------------
# System Prompt 模板（动态替换花括号；输出 JSON 示例用 {{ }} 转义）
# ------------------------------------------------------------
# 常用繁简对照表（防止繁体推理/发言混入）
_T2S_MAP = {
    '預': '预', '言': '言', '邏': '逻', '輯': '辑', '鉤': '钩', '隊': '队', '發': '发',
    '殺': '杀', '驗': '验', '證': '证', '據': '据', '歸': '归', '票': '票', '懷': '怀',
    '疑': '疑', '態': '态', '勢': '势', '認': '认', '為': '为', '點': '点', '偽': '伪',
    '裝': '装', '決': '决', '鬥': '斗', '劇': '剧', '輪': '轮', '棄': '弃', '權': '权',
    '實': '实', '質': '质', '關': '关', '鍵': '键', '結': '结', '果': '果', '衝': '冲',
    '擊': '击', '險': '险', '護': '护', '衛': '卫', '條': '条', '總': '总', '觀': '观',
    '戰': '战', '術': '术', '銀': '银', '銅': '铜', '鐵': '铁', '獨': '独', '獲': '获',
    '勝': '胜', '負': '负', '傷': '伤', '亡': '亡', '陣': '阵', '營': '营', '聽': '听',
    '說': '说', '讀': '读', '寫': '写', '選': '选', '擇': '择', '舉': '举', '報': '报',
    '隨': '随', '號': '号', '當': '当', '這': '这', '樣': '样', '還': '还', '與': '与',
    '個': '个', '們': '们', '裡': '里', '後': '后', '開': '开', '間': '间', '門': '门',
    '問': '问', '題': '题', '會': '会', '經': '经', '過': '过', '給': '给', '機': '机',
    '構': '构', '體': '体', '兩': '两', '無': '无', '規': '规', '則': '则', '視': '视',
    '內': '内', '導': '导', '致': '致', '對': '对', '錯': '错', '難': '难', '顯': '显',
    '標': '标', '注': '注', '帶': '带', '幫': '帮', '動': '动', '場': '场', '長': '长',
    '邊': '边', '線': '线', '斷': '断', '層': '层', '頭': '头', '進': '进', '步': '步',
    '覺': '觉', '得': '得', '極': '极', '確': '确', '定': '定', '隨': '随', '時': '时',
    '處': '处', '理': '理', '現': '现', '象': '象', '義': '义', '務': '务', '員': '员',
    '組': '组', '織': '织', '聯': '联', '盟': '盟', '反': '反', '饋': '馈', '單': '单',
    '純': '纯', '網': '网', '絡': '络', '傳': '传', '輸': '输', '數': '数', '測': '测',
    '試': '试', '查': '查', '看': '看', '補': '补', '償': '偿', '變': '变', '化': '化',
    '產': '产', '生': '生', '強': '强', '弱': '弱', '項': '项', '目': '目', '簽': '签',
    '準': '准', '備': '备', '軟': '软', '硬': '硬', '盤': '盘', '錄': '录', '像': '像',
    '廣': '广', '播': '播', '電': '电', '腦': '脑', '車': '车', '輕': '轻', '重': '重',
    '適': '适', '應': '应', '優': '优', '劣': '劣', '嚴': '严', '格': '格', '臺': '台',
    '灣': '湾', '萬': '万', '億': '亿', '齊': '齐', '擠': '挤', '壓': '压', '損': '损',
    '失': '失', '貪': '贪', '婪': '婪', '貧': '贫', '窮': '穷', '賈': '贾', '貨': '货',
    '購': '购', '買': '买', '賣': '卖', '贊': '赞', '賞': '赏', '賜': '赐', '賬': '账',
    '賭': '赌', '賴': '赖', '賺': '赚', '贈': '赠', '贗': '赝', '贓': '赃', '資': '资',
    '財': '财', '寶': '宝', '費': '费', '賀': '贺', '貴': '贵', '貶': '贬', '貼': '贴',
    '貿': '贸', '貸': '贷', '軍': '军', '軌': '轨', '軸': '轴', '載': '载', '輔': '辅',
    '輛': '辆', '輩': '辈', '辦': '办', '辭': '辞', '農': '农', '運': '运', '遊': '游',
    '達': '达', '遷': '迁', '遺': '遗', '郵': '邮', '鄉': '乡', '針': '针', '釘': '钉',
    '釣': '钓', '鈍': '钝', '鈔': '钞', '鈕': '钮', '鋼': '钢', '錐': '锥', '錦': '锦',
    '鍊': '链', '鍋': '锅', '鍍': '镀', '鏡': '镜', '鐘': '钟', '閉': '闭', '閏': '闰',
    '閑': '闲', '閔': '闵', '閘': '闸', '閣': '阁', '閱': '阅', '闢': '辟', '階': '阶',
    '隕': '陨', '際': '际', '隱': '隐', '隻': '只', '雙': '双', '雜': '杂', '雞': '鸡',
    '離': '离', '雲': '云', '靈': '灵', '靜': '静', '頂': '顶', '頃': '顷', '順': '顺',
    '須': '须', '頑': '顽', '頓': '顿', '頒': '颁', '頌': '颂', '領': '领', '頗': '颇',
    '頻': '频', '顆': '颗', '額': '额', '風': '风', '飛': '飞', '飢': '饥', '飯': '饭',
    '飲': '饮', '飼': '饲', '飽': '饱', '飾': '饰', '餃': '饺', '餅': '饼', '養': '养',
    '館': '馆', '馬': '马', '馳': '驰', '馴': '驯', '駁': '驳', '駐': '驻', '駒': '驹',
    '駕': '驾', '駿': '骏', '騎': '骑', '騙': '骗', '騰': '腾', '騷': '骚', '驅': '驱',
    '驚': '惊', '骨': '骨', '髮': '发', '鬧': '闹', '鬼': '鬼', '魁': '魁', '魂': '魂',
    '魄': '魄', '魅': '魅', '魘': '魇', '魚': '鱼', '魯': '鲁', '鮫': '鲛', '鮮': '鲜',
    '鯨': '鲸', '鳥': '鸟', '鳳': '凤', '鳴': '鸣', '鳶': '鸢', '鴉': '鸦', '鷗': '鸥',
    '鴨': '鸭', '鴻': '鸿', '鴿': '鸽', '鵑': '鹃', '鵝': '鹅', '鵠': '鹄', '鵡': '鹉',
    '鵬': '鹏', '鷹': '鹰', '鷺': '鹭', '鸚': '鹦', '鶴': '鹤', '麥': '麦', '黃': '黄',
    '黨': '党', '鼓': '鼓', '齋': '斋', '齒': '齿', '齡': '龄', '龍': '龙', '龜': '龟',
}
_T2S_TRANS = str.maketrans(_T2S_MAP)

SYSTEM_TEMPLATE = (
    "你正在参与12人局狼人杀（高配面杀）。本局身份【有且仅有】：{roster}。"
    "严禁提及或臆想任何【不在上面名单内】的身份（不在名单里的身份本局一律没有）。\n"
    "【警长规则】白天先竞选警长：警长投票算【1.5票】，可归票带队，出局时可把警徽移交他人或当场撕毁。\n"
    "【女巫规则】女巫有1瓶解药+1瓶毒药，每晚最多用一种（双药互斥）；解药【仅第一夜可自救】，第二夜起被刀不能自救。\n"
    "你的座位:{seat_number}。你的身份:{role}。你的风格:{style}。私密信息:{private_memory}。\n"
    "要求1: 结合局势发言，好人找狼、狼人伪装；只能基于【已真实发生】的客观事实，严禁臆造身份/夜晚/技能记录或前后矛盾。\n"
    "要求2: 默认输出严格JSON："
    '{{"reasoning":"内心OS(上帝视角)","speech":"公开发言(不暴露reasoning,100-200字)"}}；'
    "若当轮指令要求其它字段（投票轮 vote_target、上警 join_police、退水 withdraw、警徽 badge_target）则以当轮为准。\n"
    "要求3(仅狼人可用): 你可以在任意白天发言的JSON里附加 "
    '"is_explode": true 来【自爆】——立即出局、当天白天直接结束入夜，还能打断警长竞选；不自爆则填 false 或不写。\n'
    "要求4(证据分层): 本人合法获得的技能结果与公开结算的机制事实优先；其次是带来源的公开声明；最后是行为假说。"
    "reasoning 仅需简短可核验依据、主要反例及结论，不需要长篇推演。\n"
    "要求5(纠错): 警徽、票数、抱团、孤立或改口都不能单独认证阵营；改口可能是合理纠错。"
    "发现矛盾时指出来源并修正判断，不因反对共识而反对，也不因多数而跟随。\n"
    "要求6(技能常识+人性·防降智): 只按下方【本局角色关系】理解技能；没有列出的技能绝不存在。"
    "玩家会紧张、会上头、会划水、会口误——【发言莽撞/情绪化/逻辑不严密 ≠ 狼】，"
    "切勿仅凭‘态度激动’或‘盘得不够清’就给好人扣狼帽；请结合【阵营收益与立场动机】综合判断、给好人留足容错。\n"
    "要求7(机制证据优先): 公开发动并经法官结算的技能属于机械事实，优先级高于语气、站边、警徽和多数票；"
    "但任何玩家口头声称身份或查验，都仍只是公开声明，必须结合机制与收益判断真假。\n"
    "要求8(本局角色关系): 只允许使用下面列出的本局角色规则；没有列出的身份绝对不存在：\n{role_rules}\n"
    "必须区分【引擎事实账本】、【公开身份声明】和【你的主观判断】：声明不等于事实，主观判断也不准篡改事实账本。\n"
    "【信息边界】公开状态、你的私密知识必须分开。刀口、验人、用药未被公开前，不能称全场都已知道。"
    "狼人可以在公开发言中伪装和编造查验声明，但须按伪装身份可知道的内容表达；不得篡改自己的真实技能记录。"
    "【推断边界】游戏未结束不等于上次出局者是好人；某人获益不等于其必为狼人。"
    "决斗、自爆等不可逆动作需比较等待收益与失败代价，证据不足可保留技能。\n"
    "要求9(语言铁律): 必须全篇严格使用规范标准简体中文输出（严禁输出繁体字或繁简混用，如‘倒鉤’应为‘倒钩’、‘預言家’应为‘预言家’）。"
)

# ------------------------------------------------------------
# 记忆压缩·总结 Prompt（动态滚动摘要用）——严防总结时丢失/篡改底牌
# ------------------------------------------------------------
SUMMARY_TEMPLATE = (
    "【任务：整理你的记忆】你是狼人杀玩家 {seat}号，真实身份【{role}】。"
    "下面给你‘你之前的备忘录’和‘从昨晚到现在发生的全部细节’，请把它们【合并、浓缩】成一份"
    "**第一人称的逻辑备忘录**，供你后续轮次快速回忆。\n"
    "【最高铁律】身份、技能、公开查验声明和机械确认身份已由 Python 写入单独的【结构化事实账本】，"
    "你绝不负责改写这些事实。若旧备忘录与事实账本冲突，必须删除冲突内容，不得折中。\n"
    "摘要只保留三类内容：1)我的公开站边及其理由；2)我对各座位的主观身份判断，并明确写成‘我怀疑/我倾向’；"
    "3)下一轮计划。严禁把‘A号给B号金水’改写成‘我/B以外的人是A号金水’，严禁把主观推断写成机械事实。\n"
    "【格式】用‘我’的口吻，像高玩笔记：我的站边及依据→仍待验证的狼坑→我可能站错在哪里→下一轮计划。"
    "控制在 450-650 字以内。只输出备忘录正文，不要解释、不要JSON。"
)


class AgentManager:
    """管理 12 个 AI 的上下文与 API 调用。"""

    def __init__(
        self,
        api_key: str = config.DEEPSEEK_API_KEY,
        base_url: str = config.BASE_URL,
        good_model: str = config.GOOD_CAMP_MODEL,
        wolf_model: str = config.WOLF_CAMP_MODEL,
        temperature: float = config.TEMPERATURE,
        agent_timeout: int = config.AGENT_TIMEOUT,
        max_retries: int = config.MAX_RETRIES,
        fallback_model: str = config.MODEL_FALLBACK,
        provider_mode: str = "auto",
        thinking: bool = True,
        reasoning_effort: str = "low",
    ):
        base_url = validate_base_url(base_url)
        self.provider = provider_for(base_url, provider_mode)
        if reasoning_effort not in {"low", "high", "max"}:
            raise ValueError("思考强度须为 low、high 或 max")
        self.provider_mode = provider_mode
        self.thinking = bool(thinking)
        self.reasoning_effort = reasoning_effort
        # max_retries=0 ：禁用 SDK 自带重试，确保硬超时不被放大
        self.client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self.base_url = str(base_url)
        # ★ 阵营多模型(V6.0)：狼人发往 wolf_model，神职/平民发往 good_model；按座位身份动态路由。
        self.good_model = good_model
        self.wolf_model = wolf_model
        self.fallback_model = fallback_model
        # 摘要用更快的回退模型(如 deepseek-chat)，避免深度模型总结超时
        self.summary_model = fallback_model or good_model
        self.summary_timeout = int(getattr(config, "SUMMARY_TIMEOUT", 20))
        self.temperature = temperature
        self.agent_timeout = agent_timeout
        self.max_retries = min(2, max(1, int(max_retries)))
        # ===== 三层混合记忆池 =====
        #  system  : 绝对真理层之一(身份/规则 System Prompt)，永不压缩
        #  hard_facts: Python 生成的结构化事实账本，永不压缩、优先级高于摘要
        #  summary : 摘要记忆层(只保存主观判断与计划)
        #  recent  : 短期工作台(昨晚入夜至今的原始发言/内心OS/注入)
        self.agent_memory: dict[int, dict] = {
            i: {"system": None, "role": None, "hard_facts": "", "summary": "", "recent": []}
            for i in range(1, 13)
        }
        self.truth = ""   # 绝对真理层之二：引擎每轮刷新的全局当前局势(谁活着/警长是谁)，永不压缩
        # 玩家行动任务池：API 在后台执行，GameEngine 每次 next() 只轮询一次 Future，
        # 因此 Streamlit 主线程不再被一次长模型调用锁死。
        self._request_pool = ThreadPoolExecutor(max_workers=12, thread_name_prefix="wolf-agent")
        self._pending: dict[str, dict] = {}
        self._compression_pending: dict[str, object] = {}
        self._memory_generation = 0
        self._compression_context = threading.local()
        self._pending_lock = threading.Lock()
        self._request_guards = {}
        self._transport_lock = threading.Lock()
        self._transport_calls = set()
        self._health_lock = threading.Lock()
        self._usage_lock = threading.Lock()
        self._recorder = None
        self._context_provider = None
        self._consecutive_failures = 0
        self._failure_threshold = int(getattr(config, "API_CIRCUIT_BREAKER_THRESHOLD", 3))
        self.api_stats = {
            "submitted": 0, "success": 0, "fallback": 0, "canceled": 0,
            "attempt_errors": 0, "timeouts": 0, "circuit_breaks": 0,
        }
        self.token_usage = self._empty_token_usage()
        self._usage_seen = set()
        self._unknown_attempts = set()
        self._diagnostics = []
        self._last_diagnostic = None
        self._circuit_open = False

    # --------------------------------------------------------
    def clear(self):
        """彻底清空全部 12 个 AI 的记忆（开新局调用，杜绝上一局残留）。"""
        self.cancel_all_pending("新游戏清空")
        self.agent_memory = {
            i: {"system": None, "role": None, "hard_facts": "", "summary": "", "recent": []}
            for i in range(1, 13)
        }
        self.truth = ""
        with self._health_lock:
            self._consecutive_failures = 0
            self.api_stats = {k: 0 for k in self.api_stats}
            self._diagnostics = []
            self._last_diagnostic = None
            self._circuit_open = False
        with self._usage_lock:
            self.token_usage = self._empty_token_usage()
            self._usage_seen.clear()
            self._unknown_attempts.clear()

    def set_recorder(self, recorder, context_provider=None):
        """挂载每局取证记录器；记录失败绝不能反向打断游戏。"""
        self._recorder = recorder
        self._context_provider = context_provider

    def _record_context(self) -> dict:
        if not self._context_provider:
            return {}
        try:
            return dict(self._context_provider() or {})
        except Exception:
            return {}

    def _record_api(self, **payload):
        if not self._recorder:
            return
        try:
            self._recorder.record_api(**self._record_context(), **payload)
        except Exception as exc:
            config.get_logger().warning(f"RECORDER✗ API记录失败: {exc}")

    def _record_memory(self, event: str, **payload):
        if not self._recorder:
            return
        try:
            self._recorder.record_memory(event, **self._record_context(), **payload)
        except Exception as exc:
            config.get_logger().warning(f"RECORDER✗ 记忆记录失败: {exc}")

    def init_agent(self, seat_number: int, role: str, style: str, private_memory: str, roster: str = None):
        roster_text = roster or "狼人×4、平民×4、预言家×1、女巫×1、猎人×1、守卫×1"
        system_prompt = SYSTEM_TEMPLATE.format(
            seat_number=seat_number, role=role, style=style, private_memory=private_memory,
            roster=roster_text, role_rules=_role_rules_for_roster(roster_text),
        )
        self.agent_memory[seat_number] = {
            "system": {"role": "system", "content": system_prompt},
            "role": role, "hard_facts": "", "summary": "", "recent": [],
        }
        self._record_memory(
            "agent_initialized", seat=seat_number, role=role, style=style,
            private_memory=private_memory, system_prompt=system_prompt,
        )

    def set_truth(self, text: str):
        """引擎每轮刷新全局‘当前局势’(绝对真理层)，所有座位共用、永不被压缩。"""
        new_truth = text or ""
        if new_truth != self.truth:
            self.truth = new_truth
            self._record_memory("truth_updated", truth=new_truth)

    def set_hard_facts(self, seat_number: int, text: str):
        """设置某座位的 Python 事实账本；账本不进入摘要，也不会被 FIFO 丢弃。"""
        mem = self.agent_memory[seat_number]
        value = str(text or "").strip()
        if value != mem.get("hard_facts", ""):
            mem["hard_facts"] = value
            self._record_memory("hard_facts_updated", seat=seat_number, hard_facts=value)

    def set_action_context(self, seat_number, context):
        self.agent_memory[seat_number]["_action_context"] = copy.deepcopy(context)

    def set_public_evidence(self, seat_number, entries):
        # 保留完整公开证词，不与短期 FIFO 混在一起；只读公开来源。
        self.agent_memory[seat_number]["public_evidence"] = copy.deepcopy(entries)

    # --------------------------------------------------------
    # 存档 / 读档（V6.0）：序列化三层记忆池。JSON 键须为字符串，读档时转回 int。
    # 注意：不序列化模型名/超时等运行期配置——读档时按当前侧边栏配置重建，便于上帝换模型续跑。
    # --------------------------------------------------------
    def export_state(self) -> dict:
        return {
            "agent_memory": {str(k): v for k, v in self.agent_memory.items()},
            "truth": self.truth,
            "api_stats": dict(self.api_stats),
            "token_usage": self.get_usage_snapshot(),
            "consecutive_failures": self._consecutive_failures,
            "last_diagnostic": self.get_health_snapshot()["last_diagnostic"],
            "circuit_open": self.get_health_snapshot()["circuit_open"],
            "diagnostics": self.get_health_snapshot()["diagnostics"],
        }

    def load_state(self, data: dict):
        am = (data or {}).get("agent_memory", {}) or {}
        self.agent_memory = {}
        for k, v in am.items():
            v = v or {}
            self.agent_memory[int(k)] = {
                "system": v.get("system"),
                "role": v.get("role"),
                "hard_facts": v.get("hard_facts", "") or "",
                "summary": v.get("summary", "") or "",
                "recent": list(v.get("recent", []) or []),
                "public_evidence": list(v.get("public_evidence", []) or []),
                "public_cursor": int(v.get("public_cursor", 0) or 0),
                "public_seen": list(v.get("public_seen", []) or []),
                "decisions": list(v.get("decisions", []) or []),
            }
        # 补齐 12 个座位，杜绝读档后 KeyError
        for i in range(1, 13):
            self.agent_memory.setdefault(
                i, {"system": None, "role": None, "hard_facts": "", "summary": "", "recent": []}
            )
        self.truth = (data or {}).get("truth", "") or ""
        saved_stats = (data or {}).get("api_stats") or {}
        for key in self.api_stats:
            self.api_stats[key] = int(saved_stats.get(key, 0) or 0)
        saved_usage = (data or {}).get("token_usage") or {}
        restored = self._empty_token_usage()
        for key in restored:
            if key == "by_response_kind":
                continue
            restored[key] = int(saved_usage.get(key, 0) or 0)
        restored["by_response_kind"] = {}
        for kind, values in (saved_usage.get("by_response_kind") or {}).items():
            bucket = self._empty_kind_usage()
            for key in bucket:
                bucket[key] = int((values or {}).get(key, 0) or 0)
            restored["by_response_kind"][str(kind)] = bucket
        with self._usage_lock:
            self.token_usage = restored
        self._consecutive_failures = int((data or {}).get("consecutive_failures", 0) or 0)
        # 存档不能通过诊断字段注入提示词或凭证；只恢复白名单元数据。
        self._last_diagnostic = self._safe_diagnostic((data or {}).get("last_diagnostic"))
        self._circuit_open = bool((data or {}).get("circuit_open")) or self._consecutive_failures >= self._failure_threshold
        history = (data or {}).get("diagnostics") or []
        self._diagnostics = [safe for row in (history[-30:] if isinstance(history, list) else [])
                             if (safe := self._safe_diagnostic(row))]

    def inject(self, seat_number: int, info_text: str):
        """把一条客观事实硬塞进某座位的短期工作台（不触发 API）。"""
        self.agent_memory[seat_number]["recent"].append({"role": "user", "content": info_text})
        self._record_memory("injected", seat=seat_number, content=info_text)

    def _model_for(self, seat_number: int) -> str:
        """阵营路由(V6.0/6.4)：按座位身份的【camp】选模型——狼阵营(狼人/白狼王)→狼营模型，其余→好营模型。"""
        role = (self.agent_memory.get(seat_number) or {}).get("role")
        cls = ROLE_CLASSES.get(role)
        is_wolf = (cls is not None and cls.camp == "狼人")
        return self.wolf_model if is_wolf else self.good_model


    @staticmethod
    def _max_tokens_for_kind(response_kind: str) -> int:
        return output_budget(response_kind)

    @classmethod
    def _parse_json_payload(cls, raw_content: str) -> dict:
        """多重容错的 JSON 解析器：支持标准 JSON、Markdown 包裹、代码块提取与轻微截断自动修复。"""
        if not raw_content or not isinstance(raw_content, str):
            raise ValueError("空响应内容")
        text = raw_content.strip()
        # 1. 直接尝试标准 json 解析
        try:
            return json.loads(text)
        except Exception:
            pass

        # 2. 去除 markdown 代码块包裹 (```json ... ``` 或 ``` ...)
        clean = text
        if "```" in clean:
            match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", clean, re.DOTALL)
            if match:
                clean = match.group(1).strip()
                try:
                    return json.loads(clean)
                except Exception:
                    pass

        # 3. 提取最外层 {...}
        start = clean.find("{")
        end = clean.rfind("}")
        if start != -1 and end != -1 and end > start:
            bracket_substr = clean[start:end + 1]
            try:
                return json.loads(bracket_substr)
            except Exception:
                pass

        # 4. 容错修复：尝试闭合未完成的引号与花括号（针对意外截断）
        if start != -1:
            candidate = clean[start:]
            # 补齐未闭合的双引号
            unescaped_quotes = len(re.findall(r'(?<!\\)"', candidate))
            if unescaped_quotes % 2 != 0:
                candidate += '"'
            if "}" not in candidate or candidate.count("{") > candidate.count("}"):
                candidate += "}" * (candidate.count("{") - candidate.count("}"))
            try:
                return json.loads(candidate)
            except Exception:
                pass

        # 5. 如果实在无法解析，再次运行原生 json.loads 抛出标准异常供上层捕获记录
        return json.loads(raw_content)

    def _create_with_hard_timeout(self, timeout_s, *, request_context=None, **kwargs):
        """墙钟限时；晚到响应仅补记用量，不提交行动或改变健康状态。"""
        wait_budget = max(0.0, float(timeout_s))
        if not _MODEL_REQUEST_SLOTS.acquire(timeout=wait_budget):
            raise TimeoutError("服务器模型请求并发已达上限；请稍后重试")
        box, box_lock = {}, threading.Lock()
        client = self.client
        transport_id = uuid.uuid4().hex
        context = dict(request_context or {})
        with self._transport_lock:
            self._transport_calls.add(transport_id)

        def worker():
            try:
                completion = client.chat.completions.create(**kwargs)
                with box_lock:
                    box["ok"] = completion
                    if box.get("abandoned"):
                        usage = self._usage_dict(getattr(completion, "usage", None))
                        self._accumulate_usage(usage, context.get("kind", "unknown"),
                                               attempt_key=context.get("attempt_key"))
                        self._record_api(record_type="late_usage", status="ignored_late_response",
                                         request_id=context.get("request_id"),
                                         response_kind=context.get("kind"),
                                         attempt_key=context.get("attempt_key"), usage=usage)
                    box["done"] = True
            except Exception as error:
                with box_lock:
                    box["error"] = error
                    box["done"] = True
            finally:
                with self._transport_lock:
                    self._transport_calls.discard(transport_id)
                _MODEL_REQUEST_SLOTS.release()

        thread = threading.Thread(target=worker, name="wolf-model-http", daemon=True)
        try:
            thread.start()
        except Exception:
            with self._transport_lock:
                self._transport_calls.discard(transport_id)
            _MODEL_REQUEST_SLOTS.release()
            raise
        thread.join(wait_budget)
        with box_lock:
            if not box.get("done"):
                box["abandoned"] = True
                self._mark_unknown_usage(context.get("attempt_key", transport_id))
                raise TimeoutError("行动墙钟时限已到；远端请求可能尚未结束，迟到结果仅补记用量")
            if "error" in box:
                raise box["error"]
            return box.get("ok")

    def _build_messages(self, seat_number: int) -> list:
        """按三层记忆池拼出本次请求的消息：system + 当前局势 + 备忘录 + 短期细节。"""
        mem = self.agent_memory[seat_number]
        msgs = [mem["system"]]
        if self.truth:
            msgs.append({"role": "user", "content": "【当前局势·实时同步(绝对准确，以此为准)】" + self.truth})
        if mem.get("hard_facts"):
            msgs.append({
                "role": "user",
                "content": (
                    "【Python结构化事实账本·最高优先级·不可被摘要或多数意见覆盖】\n"
                    + mem["hard_facts"]
                    + "\n若后面的备忘录与本账本冲突，必须立即丢弃备忘录中的冲突内容。"
                ),
            })
        if mem["summary"]:
            msgs.append({"role": "user",
                         "content": "【主观逻辑备忘录·可能过时或有误，冲突时以事实账本为准】\n" + mem["summary"]})
        evidence, ids, omitted = select_evidence(mem.get("public_evidence", []), mem.get("public_seen", mem.get("public_cursor", 0)))
        mem["_public_delivery"] = {"event_ids": ids, "omitted_event_ids": omitted}
        if evidence:
            msgs.append({"role": "user", "content": "【公开证词·均为玩家原话，未经身份验证】\n" + evidence
                         + (f"\n篇幅限制未展开事件：{omitted}；不得将未展示内容当作不存在。" if omitted else "")})
        if mem.get("decisions"):
            msgs.append({"role": "user", "content": "【我的近期决策·可能有误，保留纠错依据】\n"
                         + json.dumps(mem["decisions"][-8:], ensure_ascii=False)})
        msgs.extend(mem["recent"])
        return msgs

    # --------------------------------------------------------
    # 非阻塞玩家请求：提交 -> 轮询 -> 收集/取消。
    # 后台 worker 不修改记忆；只有 collect/cancel 会写 assistant 消息，避免迟到响应串进后续轮次。
    # --------------------------------------------------------
    def submit_ai_response(self, seat_number: int, prompt_text: str, response_kind: str = "speech") -> str:
        mem = self.agent_memory[seat_number]
        mem["recent"].append({"role": "user", "content": prompt_text})
        messages = self._build_messages(seat_number)
        contract = mem.pop("_action_context", {})
        messages.append({"role": "user", "content": action_instruction(response_kind, contract)})
        use_model = self._model_for(seat_number)
        request_id = uuid.uuid4().hex
        with self._health_lock:
            self.api_stats["submitted"] += 1
        self._record_api(
            record_type="submitted", request_id=request_id, status="pending",
            seat=seat_number, role=mem.get("role"), response_kind=response_kind,
            model=use_model, temperature=self.temperature, timeout=self.agent_timeout,
                messages=messages, public_delivery=mem.get("_public_delivery", {}), action_contract=contract,
        )
        with self._pending_lock:
            self._request_guards[request_id] = threading.Event()
            future = self._request_pool.submit(
                self._run_response, request_id, seat_number, response_kind, messages, use_model, contract
            )
            self._pending[request_id] = {
                "future": future,
                "seat": seat_number,
                "kind": response_kind,
                "started": time.monotonic(),
                "prompt_text": prompt_text,
                "public_delivery": copy.deepcopy(mem.get("_public_delivery", {})),
            }
        return request_id

    def _run_response(self, request_id, *args):
        try:
            return self._execute_response(request_id, *args)
        finally:
            with self._pending_lock:
                self._request_guards.pop(request_id, None)

    def poll_ai_response(self, request_id: str):
        """返回 (done, result)。未完成时绝不阻塞；完成时才把有效结果写入短期记忆。"""
        with self._pending_lock:
            item = self._pending.get(request_id)
        if item is None:
            return True, self._fallback_payload("请求已失效")
        future = item["future"]
        if not future.done():
            return False, None
        try:
            result = future.result()
        except Exception as err:
            config.get_logger().exception(f"API任务异常 request={request_id[:8]}: {err}")
            model = self._model_for(item["seat"])
            result = self._finish_failed_request(
                request_id, item["seat"], item["kind"], self.agent_memory[item["seat"]].get("role"),
                model, model, datetime.now().astimezone().isoformat(timespec="milliseconds"),
                item["started"], [], "模型任务异常，请查看诊断", err,
            )
        with self._pending_lock:
            current = self._pending.pop(request_id, None)
        if current is None:
            return True, self._fallback_payload("请求已跳过")
        mem = self.agent_memory[current["seat"]]
        # 优化 Token：历史轮次只保留公开 speech 与核心行动，不重复堆叠大段内部 reasoning
        compact_entry = {
            "speech": result.get("speech", ""),
            "vote_target": result.get("vote_target", 0),
        }
        for key in ("use_antidote", "targets", "badge_target", "join_police", "withdraw",
                    "is_explode", "explode_target", "is_duel", "duel_target"):
            if key in result:
                compact_entry[key] = result[key]
        decisions = mem.setdefault("decisions", [])
        decisions.append({"kind": current["kind"], "action": compact_entry,
                          "basis": result.get("reasoning", "")[:220]})
        mem["decisions"] = decisions[-8:]
        if not result.get("_is_fallback"):
            seen = set(mem.get("public_seen", [])) | set(current.get("public_delivery", {}).get("event_ids", []))
            mem["public_seen"] = sorted(seen)
        # 旧回合提示往往重复整天的发言，证词已有独立记录，不再反复发送整份提示。
        for entry in reversed(mem["recent"]):
            if entry.get("role") == "user" and entry.get("content") == current.get("prompt_text"):
                entry["content"] = f"【历史行动】{current['kind']}；结果见下一条结构化行动。"
                break
        if result.get("_is_fallback"):
            compact_entry["_is_fallback"] = True
            compact_entry["_fallback_reason"] = result.get("_fallback_reason", "")
            compact_entry["reasoning"] = result.get("reasoning", "")
        mem["recent"].append({"role": "assistant", "content": json.dumps(compact_entry, ensure_ascii=False)})
        # 短期工作台保持最大 10 条滑动窗口，配合 hard_facts 事实账本，避免指数级输入膨胀
        if len(mem["recent"]) > 10:
            mem["recent"] = mem["recent"][-10:]
        return True, result

    def cancel_ai_response(self, request_id: str, reason: str = "上帝跳过当前玩家") -> dict:
        """使 request_id 立即失效并写入兜底；运行中的网络结果随后返回也不会进入记忆。"""
        with self._pending_lock:
            item = self._pending.pop(request_id, None)
            guard = self._request_guards.get(request_id)
            if guard is not None:
                guard.set()
        result = self._fallback_payload(reason)
        result["decision_source"] = "manual_skip"
        if item is None:
            return result
        if item["future"].cancel():
            with self._pending_lock:
                self._request_guards.pop(request_id, None)
        self.agent_memory[item["seat"]]["recent"].append(
            {"role": "assistant", "content": json.dumps(result, ensure_ascii=False)}
        )
        config.get_logger().warning(
            f"API⊘ 座位{item['seat']} request={request_id[:8]} 被跳过，迟到结果将丢弃"
        )
        with self._health_lock:
            self.api_stats["canceled"] += 1
        self._record_api(
            record_type="canceled", request_id=request_id, status="canceled",
            seat=item["seat"], response_kind=item["kind"], reason=reason,
            normalized_response=result,
        )
        return result

    def cancel_all_pending(self, reason: str = "对局重置"):
        with self._pending_lock:
            self._memory_generation += 1
            for guard in self._request_guards.values():
                guard.set()
            ids = list(self._pending)
            compression_items = list(self._compression_pending.items())
            self._compression_pending.clear()
        for request_id in ids:
            self.cancel_ai_response(request_id, reason)
        for request_id, future in compression_items:
            if future.cancel():
                with self._pending_lock:
                    self._request_guards.pop(request_id, None)
            config.get_logger().warning(f"🧠⊘ 记忆压缩 request={request_id[:8]} 已失效：{reason}")

    def has_pending(self) -> bool:
        with self._pending_lock:
            pending = bool(self._pending or self._compression_pending or self._request_guards)
        with self._transport_lock:
            return pending or bool(self._transport_calls)

    def get_runtime_snapshot(self) -> dict:
        """Return safe in-flight request metadata without prompts, responses, or credentials."""
        now = time.monotonic()
        with self._pending_lock:
            pending_requests = []
            for request_id, item in self._pending.items():
                future = item.get("future")
                started = float(item.get("started", now))
                pending_requests.append({
                    "request_id": str(request_id)[:8],
                    "seat": item.get("seat"),
                    "kind": item.get("kind", "unknown"),
                    "age_seconds": round(max(0.0, now - started), 1),
                    "future_done": bool(future and future.done()),
                })
            compression_pending_count = len(self._compression_pending)
            request_guard_count = len(self._request_guards)
        with self._transport_lock:
            transport_call_count = len(self._transport_calls)

        pending_requests.sort(key=lambda item: item["age_seconds"], reverse=True)
        ages = [item["age_seconds"] for item in pending_requests]
        pending_count = len(pending_requests)
        in_flight_count = sum(not item["future_done"] for item in pending_requests)
        has_pending = bool(
            pending_count or compression_pending_count or request_guard_count or transport_call_count
        )
        return {
            "has_pending": has_pending,
            "pending_count": pending_count,
            "in_flight_count": in_flight_count,
            "pending_requests": pending_requests,
            "oldest_pending_seconds": max(ages) if ages else None,
            "compression_pending_count": compression_pending_count,
            "request_guard_count": request_guard_count,
            "transport_call_count": transport_call_count,
        }

    def shutdown(self):
        """关闭已退出对局的任务池；运行中的网络调用按原超时结束。"""
        self.cancel_all_pending("对局服务关闭")
        self._request_pool.shutdown(wait=False, cancel_futures=True)

    def submit_memory_compression(self, alive_seats) -> str:
        """后台启动整批记忆压缩；调用立即返回，供生成器逐步轮询。"""
        request_id = uuid.uuid4().hex
        with self._pending_lock:
            self._request_guards[request_id] = threading.Event()
            future = self._request_pool.submit(self._run_compression, request_id,
                                               list(alive_seats), self._memory_generation)
            self._compression_pending[request_id] = future
        return request_id

    def _run_compression(self, request_id, seats, generation):
        try:
            if not self._request_is_active(request_id):
                return {"compressed": 0, "fallback": 0, "total": 0}
            self._compression_context.generation = generation
            return self.compress_memories(seats)
        finally:
            with self._pending_lock:
                self._request_guards.pop(request_id, None)

    def poll_memory_compression(self, request_id: str):
        """返回 (done, stats)。未完成时不等待任何网络或摘要线程。"""
        with self._pending_lock:
            future = self._compression_pending.get(request_id)
        if future is None:
            return True, {"compressed": 0, "fallback": 0, "total": 0}
        if not future.done():
            return False, None
        try:
            stats = future.result()
        except Exception as err:
            config.get_logger().exception(f"🧠✗ 记忆压缩任务异常 request={request_id[:8]}: {err}")
            stats = {"compressed": 0, "fallback": 1, "total": 0}
        with self._pending_lock:
            current = self._compression_pending.pop(request_id, None)
        if current is None:
            return True, {"compressed": 0, "fallback": 0, "total": 0}
        return True, stats

    def get_ai_response(self, seat_number: int, prompt_text: str, response_kind: str = "speech") -> dict:
        """同步兼容接口。新引擎使用 submit/poll；测试或旧调用仍可使用本方法。"""
        request_id = self.submit_ai_response(seat_number, prompt_text, response_kind)
        while True:
            done, result = self.poll_ai_response(request_id)
            if done:
                return result
            time.sleep(0.05)

    def _execute_response(self, request_id: str, seat_number: int, response_kind: str,
                          messages: list, use_model: str, action_context=None) -> dict:
        """所有尝试共用一次行动时限，最多一次修复；后台不修改玩家记忆。"""
        mem = self.agent_memory[seat_number]
        started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        request_t0 = time.monotonic()
        deadline = request_t0 + float(self.agent_timeout)
        action_context = action_context or {}
        requested_model = use_model
        attempts, last_error = [], None
        budget = output_budget(response_kind)
        call_messages = list(messages)
        repair_mode = False
        for attempt in range(1, self.max_retries + 1):
            if not self._request_is_active(request_id):
                return self._fallback_payload("请求已取消，迟到结果不提交")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last_error = TimeoutError("行动总时限已到")
                break
            t0, raw_content, usage = time.monotonic(), None, None
            attempt_key = f"{request_id}:{attempt}"
            metadata = {"attempt": attempt, "model": use_model, "provider": self.provider,
                        "output_budget": budget,
                        "thinking": self.provider == "deepseek" and self.thinking and not repair_mode and response_kind not in {"ack", "order"},
                        "reasoning_effort": self.reasoning_effort if self.provider == "deepseek" and self.thinking and not repair_mode and response_kind not in {"ack", "order"} else None}
            try:
                completion = self._create_with_hard_timeout(
                    remaining, request_context={"request_id": request_id, "kind": response_kind,
                                                "attempt_key": attempt_key},
                    model=use_model, messages=call_messages, timeout=remaining,
                    **request_options(self.provider, response_kind, budget=budget,
                                      temperature=self.temperature, thinking=self.thinking and not repair_mode,
                                      effort=self.reasoning_effort),
                )
                metadata["response_received"] = True
                usage = self._usage_dict(getattr(completion, "usage", None))
                self._accumulate_usage(usage, response_kind, attempt_key=attempt_key)
                raw_content, response_meta = message_metadata(completion)
                metadata.update(response_meta)
                validate_content(raw_content, metadata, usage, budget, response_kind, action_context)
                data = parse_action(raw_content, response_kind, action_context)
                result = self._normalize(data)
                result["decision_source"] = "model"
                metadata.update(status="success", duration_ms=round((time.monotonic()-t0)*1000),
                                raw_response=raw_content, parsed_response=data, usage=usage)
                attempts.append(metadata)
                consecutive = self._note_api_success(request_id)
                self._record_api(
                    record_type="completed", request_id=request_id, status="success",
                    seat=seat_number, role=mem.get("role"), response_kind=response_kind,
                    requested_model=requested_model, final_model=use_model, started_at=started_at,
                    duration_ms=round((time.monotonic()-request_t0)*1000), attempts=attempts,
                    raw_response=raw_content, normalized_response=result, usage=usage,
                    consecutive_failures=consecutive,
                )
                config.get_logger().info(f"API✓ 座位{seat_number} kind={response_kind} 尝试{attempt}次成功")
                return result
            except Exception as error:
                last_error = error
                status = self._status_code(error)
                category = error_category(error, status)
                if self._is_timeout(error) and usage is None:
                    self._mark_unknown_usage(attempt_key)
                metadata.update(status="error", duration_ms=round((time.monotonic()-t0)*1000),
                                error_type=type(error).__name__, error=self._safe_error_text(error),
                                category=category, status_code=status, raw_response=raw_content, usage=usage)
                if category == "budget_exhausted":
                    metadata["budget_evidence"] = ("finish_reason" if metadata.get("finish_reason") == "length"
                                                   else "reasoning_usage_inferred")
                attempts.append(metadata)
                with self._health_lock:
                    self.api_stats["attempt_errors"] += 1
                    if self._is_timeout(error):
                        self.api_stats["timeouts"] += 1
                self._publish_diagnostic(request_id, {**metadata, "seat": seat_number,
                                                      "response_kind": response_kind,
                                                      "timestamp": started_at})
                config.get_logger().warning(f"API✗ 座位{seat_number} {category} 尝试{attempt}/{self.max_retries}")
                if status in (401, 402, 403):
                    return self._finish_failed_request(
                        request_id, seat_number, response_kind, mem.get("role"), requested_model,
                        use_model, started_at, request_t0, attempts, ERROR_LABELS[category], error,
                        fatal_error=f"HTTP {status}: {ERROR_LABELS[category]}",
                    )
                # 超时不并发重发尚未结算的远端请求；非暂时性错误也不重复调用。
                if (self._is_timeout(error) or category == "content_filter"
                        or attempt == self.max_retries):
                    break
                if status is not None and 400 <= status < 500 and status != 429:
                    if (self.fallback_model and use_model != self.fallback_model
                            and self._looks_like_model_error(error)):
                        use_model = self.fallback_model
                        continue  # 模型回退也占用总尝试次数
                    break
                if category == "budget_exhausted":
                    increased = output_budget(response_kind, repair=True)
                    if increased <= budget:
                        break
                    budget = increased
                if category in {"budget_exhausted", "empty_content", "invalid_json", "invalid_action"}:
                    repair_mode = True
                    call_messages = list(messages) + [{"role": "user", "content":
                        "上次最终输出未通过格式校验。请依据本轮原始指令，只返回完整 JSON 对象；"
                        "保留所需行动字段，reasoning 简洁，公开 speech 遵守本轮长度要求。"
                        + action_instruction(response_kind, action_context)}]
                    continue
                wait_s = self._retry_after(error) if status == 429 else 0.5
                if wait_s <= 0:
                    wait_s = 0.75
                remaining = deadline - time.monotonic()
                if wait_s >= remaining:
                    if status == 429:
                        return self._finish_failed_request(
                            request_id, seat_number, response_kind, mem.get("role"), requested_model,
                            use_model, started_at, request_t0, attempts, ERROR_LABELS[category], error,
                            fatal_error=f"服务商限流，建议等待 {wait_s:g} 秒，已超过行动剩余时限；已暂停，请稍后继续",
                        )
                    break
                self._wait_for_retry(request_id, wait_s)
        category = error_category(last_error, self._status_code(last_error))
        return self._finish_failed_request(
            request_id, seat_number, response_kind, mem.get("role"), requested_model, use_model,
            started_at, request_t0, attempts, ERROR_LABELS.get(category, ERROR_LABELS["unknown"]), last_error,
        )

    def _request_is_active(self, request_id):
        with self._pending_lock:
            guard = self._request_guards.get(request_id)
            return guard is None or not guard.is_set()

    def _wait_for_retry(self, request_id, seconds):
        with self._pending_lock:
            guard = self._request_guards.get(request_id)
        if guard is not None:
            guard.wait(seconds)
        else:
            time.sleep(seconds)

    def _safe_error_text(self, error):
        value = str(error)
        key = getattr(self.client, "api_key", "")
        if isinstance(key, str) and key:
            value = value.replace(key, "[REDACTED]")
        return value[:350]

    def _note_api_success(self, request_id=None) -> int:
        with self._pending_lock, self._health_lock:
            guard = self._request_guards.get(request_id)
            if guard is not None and guard.is_set():
                return self._consecutive_failures
            self._consecutive_failures = 0
            self._circuit_open = False
            self.api_stats["success"] += 1
            if self._last_diagnostic and self._last_diagnostic.get("request_id") == request_id:
                self._last_diagnostic["resolved"] = True
            return 0

    def _note_api_failure(self, force_break=False, request_id=None) -> tuple[int, bool]:
        with self._pending_lock, self._health_lock:
            guard = self._request_guards.get(request_id)
            if guard is not None and guard.is_set():
                return self._consecutive_failures, False
            self._consecutive_failures += 1
            self.api_stats["fallback"] += 1
            tripped = force_break or self._consecutive_failures >= self._failure_threshold
            if tripped and (force_break or self._consecutive_failures == self._failure_threshold):
                self.api_stats["circuit_breaks"] += 1
            self._circuit_open = tripped
            return self._consecutive_failures, tripped

    @staticmethod
    def _safe_diagnostic(value):
        if not isinstance(value, dict):
            return None
        fields = {"request_id", "seat", "response_kind", "category", "attempt", "attempt_count",
                  "model", "returned_model", "finish_reason", "content_length", "reasoning_content_length",
                  "output_budget", "provider", "thinking", "reasoning_effort", "duration_ms",
                  "timestamp", "status_code", "budget_evidence", "resolved"}
        result = {k: v for k, v in value.items() if k in fields
                  and isinstance(v, (str, int, float, bool, type(None)))}
        if isinstance(value.get("usage"), dict):
            result["usage"] = AgentManager._usage_values(value["usage"])
        return result

    def _publish_diagnostic(self, request_id, value):
        safe = self._safe_diagnostic({**value, "request_id": request_id})
        with self._pending_lock, self._health_lock:
            guard = self._request_guards.get(request_id)
            if guard is not None and guard.is_set():
                return
            self._last_diagnostic = safe
            self._diagnostics.append(safe)
            self._diagnostics = self._diagnostics[-30:]

    def get_health_snapshot(self):
        with self._health_lock:
            return {"consecutive_failures": self._consecutive_failures,
                    "circuit_open": self._circuit_open,
                    "last_diagnostic": copy.deepcopy(self._last_diagnostic),
                    "diagnostics": copy.deepcopy(self._diagnostics),
                    "provider": self.provider, "thinking": self.thinking,
                    "reasoning_effort": self.reasoning_effort}

    def diagnose_connection(self):
        """用户明确点击时执行一次短协议测试；不清零完整行动的熔断计数。"""
        if self.has_pending():
            raise RuntimeError("请先暂停并等待后台请求完成，再测试连接")
        request_id = "diagnostic-" + uuid.uuid4().hex
        attempt_key = request_id + ":1"
        usage, meta = None, {}
        started = time.monotonic()
        messages = [{"role": "user", "content": '只回复完整JSON对象：{"ok":1}'}]
        self._record_api(record_type="submitted", request_id=request_id, status="pending",
                         response_kind="diagnostic", model=self.good_model)
        try:
            timeout = min(20.0, float(self.agent_timeout))
            completion = self._create_with_hard_timeout(
                timeout, model=self.good_model, messages=messages, timeout=timeout,
                request_context={"request_id": request_id, "kind": "diagnostic", "attempt_key": attempt_key},
                **request_options(self.provider, "diagnostic", budget=output_budget("diagnostic"),
                                  temperature=0),
            )
            usage = self._usage_dict(getattr(completion, "usage", None))
            self._accumulate_usage(usage, "diagnostic", attempt_key=attempt_key)
            raw, meta = message_metadata(completion)
            validate_content(raw, meta, usage, output_budget("diagnostic"))
            parse_action(raw, "diagnostic")
            ok, category = True, None
        except Exception as error:
            ok, category = False, error_category(error, self._status_code(error))
            if self._is_timeout(error) and usage is None:
                self._mark_unknown_usage(attempt_key)
        diagnostic = self._safe_diagnostic({**meta, "request_id": request_id,
            "response_kind": "diagnostic", "category": category, "provider": self.provider,
            "model": self.good_model, "output_budget": output_budget("diagnostic"),
            "attempt_count": 1, "duration_ms": round((time.monotonic()-started)*1000), "usage": usage})
        self._record_api(record_type="completed", request_id=request_id,
                         response_kind="diagnostic", status="success" if ok else "fallback",
                         usage=usage, diagnostic=diagnostic)
        message = ("短协议验证通过；不代表完整行动预算充足或长局稳定，熔断计数保持不变。" if ok else
                   "短协议验证失败：" + ERROR_LABELS.get(category, ERROR_LABELS["unknown"]))
        return ok, message, diagnostic

    def _finish_failed_request(
        self, request_id, seat, response_kind, role, requested_model, final_model,
        started_at, request_t0, attempts, reason, last_error, fatal_error="",
    ) -> dict:
        consecutive, tripped = self._note_api_failure(force_break=bool(fatal_error), request_id=request_id)
        if tripped and not fatal_error:
            fatal_error = (
                f"连续{consecutive}个行动失败，已触发API熔断；最近原因：{reason}。"
                "请查看诊断并在暂停时应用设置；继续不会撤销已提交的兜底行动"
            )
        result = self._fallback_payload(reason, fatal_error=fatal_error)
        diagnostic = {**(attempts[-1] if attempts else {}), "seat": seat,
                      "response_kind": response_kind, "attempt_count": len(attempts),
                      "category": error_category(last_error, self._status_code(last_error)),
                      "timestamp": started_at, "resolved": False}
        self._publish_diagnostic(request_id, diagnostic)
        result["_api_diagnostic"] = self._safe_diagnostic({**diagnostic, "request_id": request_id})
        self._record_api(
            record_type="completed", request_id=request_id, status="fallback",
            seat=seat, role=role, response_kind=response_kind,
            requested_model=requested_model, final_model=final_model,
            started_at=started_at,
            duration_ms=round((time.monotonic() - request_t0) * 1000),
            attempts=attempts, normalized_response=result,
            failure_reason=reason,
            last_error={"type": type(last_error).__name__, "message": self._safe_error_text(last_error)},
            consecutive_failures=consecutive, circuit_breaker=tripped,
        )
        if self._recorder:
            self._recorder.log(
                "ERROR", f"API fallback seat={seat} request={request_id[:8]} "
                f"reason={reason} consecutive={consecutive} circuit={tripped}"
            )
        return result

    @staticmethod
    def _usage_dict(usage) -> dict | None:
        if usage is None:
            return None
        if hasattr(usage, "model_dump"):
            return usage.model_dump()
        if isinstance(usage, dict):
            return dict(usage)
        result = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = getattr(usage, key, None)
            if value is not None:
                result[key] = value
        return result or None

    @staticmethod
    def _empty_token_usage() -> dict:
        return {
            "api_returns": 0,
            "unmetered_returns": 0,
            "unknown_requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
            "summary_requests": 0,
            "summary_total_tokens": 0,
            "by_response_kind": {},
        }

    @staticmethod
    def _empty_kind_usage() -> dict:
        return {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "reasoning_tokens": 0,
        }

    @staticmethod
    def _usage_values(usage: dict | None) -> dict:
        """兼容 DeepSeek/OpenAI usage 字段，返回统一整数指标。"""
        usage = usage or {}

        def as_int(value) -> int:
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        prompt = as_int(usage.get("prompt_tokens"))
        completion = as_int(usage.get("completion_tokens"))
        total = as_int(usage.get("total_tokens")) or (prompt + completion)
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        cache_hit = as_int(usage.get("prompt_cache_hit_tokens", usage.get("cache_hit_tokens")))
        if not cache_hit:
            cache_hit = as_int(prompt_details.get("cached_tokens"))
        cache_miss = as_int(usage.get("prompt_cache_miss_tokens", usage.get("cache_miss_tokens")))
        if not cache_miss and prompt:
            cache_miss = max(0, prompt - cache_hit)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "reasoning_tokens": as_int(completion_details.get("reasoning_tokens", usage.get("reasoning_tokens"))),
        }

    def _mark_unknown_usage(self, attempt_key):
        if not attempt_key:
            return
        with self._usage_lock:
            if attempt_key not in self._usage_seen and attempt_key not in self._unknown_attempts:
                self._unknown_attempts.add(attempt_key)
                self.token_usage["unknown_requests"] += 1

    def _accumulate_usage(self, usage: dict | None, response_kind: str, attempt_key=None) -> None:
        """线程安全累计每次真正返回的 API 用量；不依赖归档文件扫描。"""
        kind = str(response_kind or "unknown")
        values = self._usage_values(usage)
        with self._usage_lock:
            if attempt_key:
                if attempt_key in self._usage_seen:
                    return
                self._usage_seen.add(attempt_key)
                if attempt_key in self._unknown_attempts:
                    self._unknown_attempts.remove(attempt_key)
                    self.token_usage["unknown_requests"] = max(0, self.token_usage["unknown_requests"] - 1)
            self.token_usage["api_returns"] += 1
            if not usage:
                self.token_usage["unmetered_returns"] += 1
            bucket = self.token_usage["by_response_kind"].setdefault(
                kind, self._empty_kind_usage(),
            )
            bucket["requests"] += 1
            for key, value in values.items():
                self.token_usage[key] += value
                bucket[key] = int(bucket.get(key, 0)) + value
            if kind == "memory_summary":
                self.token_usage["summary_requests"] += 1
                self.token_usage["summary_total_tokens"] += values["total_tokens"]

    def get_usage_snapshot(self) -> dict:
        """给前端/存档读取的不可变快照。"""
        with self._usage_lock:
            result = {
                key: int(value)
                for key, value in self.token_usage.items()
                if key != "by_response_kind"
            }
            result["by_response_kind"] = {
                kind: {key: int(value) for key, value in values.items()}
                for kind, values in self.token_usage["by_response_kind"].items()
            }
        return result

    @staticmethod
    def _status_code(err):
        return (getattr(err, "status_code", None)
                or getattr(getattr(err, "response", None), "status_code", None))

    @staticmethod
    def _retry_after(err):
        response = getattr(err, "response", None)
        headers = getattr(response, "headers", {}) or {}
        try:
            return float(headers.get("retry-after", 0) or 0)
        except (TypeError, ValueError):
            try:
                from email.utils import parsedate_to_datetime
                return max(0.0, parsedate_to_datetime(headers.get("retry-after")).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return 0.0

    def _fallback_payload(self, reason: str, fatal_error: str = "") -> dict:
        fb = {
            "reasoning": reason, "speech": "过/弃权", "vote_target": 0,
            "is_explode": False, "explode_target": 0,
            "is_duel": False, "duel_target": 0,
            "badge_target": 0, "join_police": False, "withdraw": False,
            "_is_fallback": True, "_fallback_reason": reason, "decision_source": "technical_fallback",
        }
        if fatal_error:
            fb["_fatal_error"] = fatal_error
        return fb

    def _record_fallback(self, mem: dict, reason: str) -> dict:
        """兼容旧测试：构造并直接写入兜底。"""
        fb = self._fallback_payload(reason)
        mem["recent"].append({"role": "assistant", "content": json.dumps(fb, ensure_ascii=False)})
        return fb

    # ========================================================
    # 记忆压缩（动态滚动摘要）：每天入夜时由引擎调用，并发执行、绝不卡死
    # ========================================================
    def _summarize_one(self, seat_number: int, generation=None):
        """压缩单个座位：把 旧备忘录+近期细节 → 新的第一人称备忘录；失败则 FIFO 截断兜底。"""
        mem = self.agent_memory[seat_number]
        generation = self._memory_generation if generation is None else generation
        if generation != self._memory_generation:
            return ("canceled", seat_number)
        if not mem["recent"]:
            return ("skip", seat_number)
        _log = config.get_logger()
        request_id = "summary-" + uuid.uuid4().hex
        before_summary = mem["summary"]
        before_recent = list(mem["recent"])
        started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        request_t0 = time.monotonic()
        usage, response_meta = None, {}
        attempt_key = f"{request_id}:1"
        self._record_memory(
            "compression_started", seat=seat_number, role=mem.get("role"),
            previous_summary=before_summary, recent_events=before_recent,
            request_id=request_id,
        )
        try:
            recent_text = "\n".join(f"[{m['role']}] {m['content']}" for m in mem["recent"])
            user_msg = (SUMMARY_TEMPLATE.format(seat=seat_number, role=mem.get("role") or "未知")
                        + "\n\n【Python结构化事实账本·只读·绝不可改写】\n"
                        + (mem.get("hard_facts") or "（暂无）")
                        + "\n\n【你之前的备忘录】\n" + (mem["summary"] or "（暂无）")
                        + "\n\n【从昨晚入夜到现在发生的全部细节(你的发言/内心OS/收到的播报与上帝法旨)】\n" + recent_text)
            messages = [mem["system"], {"role": "user", "content": user_msg}]
            self._record_api(
                record_type="submitted", request_id=request_id, status="pending",
                seat=seat_number, role=mem.get("role"), response_kind="memory_summary",
                model=self.summary_model, temperature=0.2, timeout=self.summary_timeout,
                messages=messages,
            )
            t0 = time.monotonic()
            completion = self._create_with_hard_timeout(
                self.summary_timeout, model=self.summary_model, messages=messages,
                request_context={"request_id": request_id, "kind": "memory_summary", "attempt_key": attempt_key},
                timeout=self.summary_timeout,
                **request_options(self.provider, "memory_summary", budget=output_budget("memory_summary"),
                                  temperature=0.2),
            )
            usage = self._usage_dict(getattr(completion, "usage", None))
            self._accumulate_usage(usage, "memory_summary", attempt_key=attempt_key)
            raw, response_meta = message_metadata(completion)
            validate_content(raw, response_meta, usage, output_budget("memory_summary"))
            new_summary = raw.strip()
            if generation != self._memory_generation or self.agent_memory[seat_number] is not mem:
                self._record_api(record_type="completed", request_id=request_id, status="canceled",
                                 response_kind="memory_summary", usage=usage, **response_meta)
                return ("canceled", seat_number)
            mem["summary"] = self._truncate_summary(new_summary)
            mem["recent"] = []                    # 短期工作台清空（已并入备忘录）
            _log.info(f"🧠✓ 座位{seat_number} 摘要成功 {len(new_summary)}字 用时{time.monotonic()-t0:.1f}s")
            self._record_api(
                record_type="completed", request_id=request_id, status="success",
                seat=seat_number, role=mem.get("role"), response_kind="memory_summary",
                requested_model=self.summary_model, final_model=self.summary_model,
                started_at=started_at,
                duration_ms=round((time.monotonic() - request_t0) * 1000),
                raw_response=new_summary, normalized_response={"summary": mem["summary"]},
                usage=usage, **response_meta,
            )
            self._record_memory(
                "compression_completed", seat=seat_number, request_id=request_id,
                status="success", previous_summary=before_summary,
                new_summary=mem["summary"], discarded_recent=before_recent,
            )
            return ("ok", seat_number)
        except Exception as e:
            if self._is_timeout(e) and usage is None:
                self._mark_unknown_usage(attempt_key)
            if generation != self._memory_generation or self.agent_memory[seat_number] is not mem:
                self._record_api(record_type="completed", request_id=request_id, status="canceled",
                                 response_kind="memory_summary", usage=usage,
                                 category=error_category(e, self._status_code(e)), **response_meta)
                return ("canceled", seat_number)
            # ★终极兜底：摘要失败/超时 → 粗暴 FIFO 丢弃最老的一半 recent，保 summary 不变、绝不卡死
            n = len(mem["recent"])
            mem["recent"] = mem["recent"][n // 2:]
            _log.warning(f"🧠✗ 座位{seat_number} 摘要失败({type(e).__name__})→FIFO截断，剩{len(mem['recent'])}条")
            self._record_api(
                record_type="completed", request_id=request_id, status="fallback",
                seat=seat_number, role=mem.get("role"), response_kind="memory_summary",
                requested_model=self.summary_model, final_model=self.summary_model,
                started_at=started_at,
                duration_ms=round((time.monotonic() - request_t0) * 1000),
                failure_reason="memory_compression_failed",
                last_error={"type": type(e).__name__, "message": self._safe_error_text(e)},
                usage=usage, category=error_category(e, self._status_code(e)), **response_meta,
            )
            self._record_memory(
                "compression_completed", seat=seat_number, request_id=request_id,
                status="fifo_fallback", previous_summary=before_summary,
                retained_recent=list(mem["recent"]), discarded_count=n - len(mem["recent"]),
            )
            return ("fallback", seat_number)

    def compress_memories(self, alive_seats) -> dict:
        """并发压缩所有‘有新内容’的存活座位记忆。整体阻塞≈单次摘要超时(并发)，不会 N 倍叠加。"""
        min_chars = int(getattr(config, "MEMORY_COMPRESS_MIN_CHARS", 9000))
        seats = [
            s for s in alive_seats
            if sum(len(str(m.get("content", ""))) for m in self.agent_memory[s]["recent"]) >= min_chars
        ]
        _log = config.get_logger()
        skipped = sum(1 for s in alive_seats if self.agent_memory[s]["recent"] and s not in seats)
        stats = {"compressed": 0, "fallback": 0, "total": len(seats), "skipped": skipped}
        if not seats:
            return stats
        _log.info(f"🧠 记忆压缩(并发)开始：{len(seats)}个座位 model={self.summary_model} 超时={self.summary_timeout}s")
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(12, len(seats))) as ex:
            generation = getattr(self._compression_context, "generation", self._memory_generation)
            futures = [ex.submit(self._summarize_one, s, generation) for s in seats]
            for fut in futures:
                try:
                    res = fut.result(timeout=self.summary_timeout + 10)
                except Exception as e:
                    _log.warning(f"🧠✗ 某座位 future 异常/超时：{e}")
                    res = ("fallback", -1)
                if res[0] == "ok":
                    stats["compressed"] += 1
                elif res[0] == "fallback":
                    stats["fallback"] += 1
        _log.info(f"🧠 记忆压缩完成 用时{time.monotonic()-t0:.1f}s {stats}")
        return stats

    @staticmethod
    def _truncate_summary(text: str, limit: int = None) -> str:
        """把摘要限制在可控长度，尽量在中文句号或换行处截断。"""
        limit = int(limit or getattr(config, "SUMMARY_MAX_CHARS", 700))
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        floor = max(200, int(limit * 0.68))
        cut = max(value.rfind(mark, floor, limit) for mark in ("。", "！", "？", "\n"))
        if cut < floor:
            cut = limit
        else:
            cut += 1
        return value[:cut].rstrip()

    def _normalize(self, data) -> dict:
        """规整为统一结构，字段齐全且安全默认。"""
        if not isinstance(data, dict):
            data = {}
        reasoning = str(data.get("reasoning", "")).strip() or "（无内心独白）"
        # 必需字段已由本轮协议验证；纯动作不再伪造一句“过”。
        speech = str(data.get("speech", "")).strip()
        if _T2S_TRANS:
            reasoning = reasoning.translate(_T2S_TRANS)
            speech = speech.translate(_T2S_TRANS)
        return {
            "reasoning": reasoning,
            "speech": speech,
            "vote_target": self._coerce_seat(data.get("vote_target", 0)),
            "is_explode": bool(data.get("is_explode", False)),
            "explode_target": self._coerce_seat(data.get("explode_target", 0)),   # ★V6.4 白狼王带人目标
            "is_duel": bool(data.get("is_duel", False)),                          # ★V6.5 骑士决斗
            "duel_target": self._coerce_seat(data.get("duel_target", 0)),         # ★V6.5 骑士决斗目标
            "badge_target": self._coerce_seat(data.get("badge_target", 0)),
            **({"use_antidote": data["use_antidote"]} if "use_antidote" in data else {}),
            **({"targets": list(data["targets"])} if "targets" in data else {}),
            "join_police": bool(data.get("join_police", False)),
            "withdraw": bool(data.get("withdraw", False)),
        }

    @staticmethod
    def _coerce_seat(value) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            n = int(value)
        else:
            m = re.search(r"\d{1,2}", str(value))
            n = int(m.group()) if m else 0
        return n if 0 <= n <= 12 else 0

    @staticmethod
    def _is_timeout(err) -> bool:
        if isinstance(err, TimeoutError):
            return True
        return "timeout" in type(err).__name__.lower() or "timed out" in str(err).lower()

    @staticmethod
    def _looks_like_model_error(err) -> bool:
        code = getattr(err, "status_code", None) or getattr(getattr(err, "response", None), "status_code", None)
        if code == 404:
            return True
        msg = str(err).lower()
        return ("model" in msg or "模型" in msg) and any(
            k in msg for k in ("not found", "does not exist", "unsupported model", "invalid model", "不存在", "无效"))


from engine.runtime_version import register_source as _register_source
_register_source(__name__, __file__)
