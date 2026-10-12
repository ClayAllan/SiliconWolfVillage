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
import re
import threading
import time
import uuid
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from openai import OpenAI

import config
from roles import ROLE_CLASSES   # 仅取各身份的 camp(类属性)做阵营路由；roles 不反向 import 本模块


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
    "要求4(深度思考·防降智): 请在 reasoning 里【充分推演】再下结论——【盘票型】(谁带头冲票、是否一边倒抱团)、"
    "从【狼人收益】角度反推每个人的身份、警惕【悍跳狼往往比真神更凶悍】、注意‘跳神后退水’可能是在【挡刀】。"
    "好人切忌无脑迷信‘查杀力度大’或随大流散票/抱团；高端操作(穿衣服挡刀、盘逻辑、强硬带队)都可大胆使用。\n"
    "要求5(防倒钩·质疑权威): 【警长/金水也不可全信】！倒钩狼最爱骗信任后突然改口踩好人。"
    "投票/站边前务必回忆每人前几天的发言逻辑，谁【推翻自己旧逻辑、改口踩好人】极可能是【深水倒钩狼】，"
    "要盘出其【逻辑断层】果断反打，绝不因对方是警长或被发金水就盲目跟票。\n"
    "要求6(技能常识+人性·防降智): 只按下方【本局角色关系】理解技能；没有列出的技能绝不存在。"
    "玩家会紧张、会上头、会划水、会口误——【发言莽撞/情绪化/逻辑不严密 ≠ 狼】，"
    "切勿仅凭‘态度激动’或‘盘得不够清’就给好人扣狼帽；请结合【阵营收益与立场动机】综合判断、给好人留足容错。\n"
    "要求7(机制证据优先): 公开发动并经法官结算的技能属于机械事实，优先级高于语气、站边、警徽和多数票；"
    "但任何玩家口头声称身份或查验，都仍只是公开声明，必须结合机制与收益判断真假。\n"
    "要求8(本局角色关系): 只允许使用下面列出的本局角色规则；没有列出的身份绝对不存在：\n{role_rules}\n"
    "必须区分【引擎事实账本】、【公开身份声明】和【你的主观判断】：声明不等于事实，主观判断也不准篡改事实账本。"
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
    ):
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
        self.max_retries = max_retries
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
        self._pending_lock = threading.Lock()
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
        with self._usage_lock:
            self.token_usage = self._empty_token_usage()

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
        """按请求类型精细化限制 max_tokens，避免盲投/定序时输出大段冗余 token。"""
        if response_kind in ("vote", "withdraw", "join_police", "badge_target", "order", "quick"):
            return 600
        if response_kind in ("memory_summary",):
            return 1200
        return 1500

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

    def _create_with_hard_timeout(self, timeout_s, **kwargs):
        """★真·墙钟硬超时(V6.4.1)：在 daemon 线程里跑 create()，主线程最多 join 等 timeout_s 秒，
        到点仍未返回 → 抛 TimeoutError 并忽略该线程的迟到结果、立刻兜底。

        为什么必须这样：传给 SDK 的 timeout= 只是 httpx 的 read/connect 超时(两次收字节之间的间隔)，
        深度思考模型(长 CoT)或反代/网关的 keep-alive 心跳会让它【永不触发】——单步 create() 能阻塞数百秒，
        把 Streamlit 单线程脚本彻底卡死(进而 WebSocket 闲置断开、媒体文件失效)。这里用线程 join 做兜底，
        无论底层如何都保证调用方【不超过 timeout_s】。注意 Python 无法安全杀死阻塞线程，
        所以线程可能继续到网络请求自行结束；上层任务池和 request_id 会限制数量并丢弃迟到结果。"""
        box = {}

        def _worker():
            try:
                box["ok"] = self.client.chat.completions.create(**kwargs)
            except Exception as e:    # 线程内异常(超时/网络/模型错误)带回主线程统一处理
                box["err"] = e

        th = threading.Thread(target=_worker, daemon=True)
        th.start()
        th.join(timeout_s)
        if th.is_alive():
            raise TimeoutError(f"硬超时 >{timeout_s}s（API 未在墙钟时限内返回，迟到结果将被忽略）")
        if "err" in box:
            raise box["err"]
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
        use_model = self._model_for(seat_number)
        request_id = uuid.uuid4().hex
        with self._health_lock:
            self.api_stats["submitted"] += 1
        self._record_api(
            record_type="submitted", request_id=request_id, status="pending",
            seat=seat_number, role=mem.get("role"), response_kind=response_kind,
            model=use_model, temperature=self.temperature, timeout=self.agent_timeout,
            messages=messages,
        )
        future = self._request_pool.submit(
            self._execute_response, request_id, seat_number, response_kind, messages, use_model
        )
        with self._pending_lock:
            self._pending[request_id] = {
                "future": future,
                "seat": seat_number,
                "kind": response_kind,
                "started": time.monotonic(),
            }
        return request_id

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
            result = self._fallback_payload("系统故障")
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
        result = self._fallback_payload(reason)
        if item is None:
            return result
        item["future"].cancel()
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
            ids = list(self._pending)
            compression_items = list(self._compression_pending.items())
            self._compression_pending.clear()
        for request_id in ids:
            self.cancel_ai_response(request_id, reason)
        for request_id, future in compression_items:
            future.cancel()
            config.get_logger().warning(f"🧠⊘ 记忆压缩 request={request_id[:8]} 已失效：{reason}")

    def has_pending(self) -> bool:
        with self._pending_lock:
            return bool(self._pending or self._compression_pending)

    def submit_memory_compression(self, alive_seats) -> str:
        """后台启动整批记忆压缩；调用立即返回，供生成器逐步轮询。"""
        request_id = uuid.uuid4().hex
        future = self._request_pool.submit(self.compress_memories, list(alive_seats))
        with self._pending_lock:
            self._compression_pending[request_id] = future
        return request_id

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
                          messages: list, use_model: str) -> dict:
        """后台执行完整 API 策略。不得修改 agent_memory。"""
        mem = self.agent_memory[seat_number]
        _log = config.get_logger()
        request_started_wall = datetime.now().astimezone().isoformat(timespec="milliseconds")
        request_t0 = time.monotonic()
        requested_model = use_model
        attempts = []
        ctx_chars = sum(len(m.get("content", "")) for m in messages)
        _log.info(f"API→ 座位{seat_number}({mem.get('role')}) kind={response_kind} 模型={use_model} "
                  f"上下文{len(messages)}条/{ctx_chars}字"
                  f"(摘要{len(mem['summary'])}字+近{len(mem['recent'])}条) 超时={self.agent_timeout}s")

        last_error = None
        switched = False
        attempt = 0
        while attempt < max(1, self.max_retries):
            attempt += 1
            t0 = time.monotonic()
            raw_content = None
            usage = None
            try:
                max_tok = self._max_tokens_for_kind(response_kind)
                completion = self._create_with_hard_timeout(
                    self.agent_timeout,
                    model=use_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=self.temperature,
                    max_tokens=max_tok,
                    timeout=self.agent_timeout,
                )
                raw_content = completion.choices[0].message.content
                usage = self._usage_dict(getattr(completion, "usage", None))
                # API 已真实返回即计入成本；即使随后 JSON 解析失败并重试，本次 token 也不能漏算。
                self._accumulate_usage(usage, response_kind)
                data = self._parse_json_payload(raw_content)
                result = self._normalize(data)
                dt = time.monotonic() - t0
                attempts.append({
                    "attempt": attempt, "model": use_model, "status": "success",
                    "duration_ms": round(dt * 1000), "raw_response": raw_content,
                    "parsed_response": data, "usage": usage,
                })
                consecutive = self._note_api_success()
                _log.info(f"API✓ 座位{seat_number} 用时{dt:.1f}s 成功 (model={use_model})")
                self._record_api(
                    record_type="completed", request_id=request_id, status="success",
                    seat=seat_number, role=mem.get("role"), response_kind=response_kind,
                    requested_model=requested_model, final_model=use_model,
                    started_at=request_started_wall,
                    duration_ms=round((time.monotonic() - request_t0) * 1000),
                    attempts=attempts, raw_response=raw_content,
                    normalized_response=result, usage=usage,
                    consecutive_failures=consecutive,
                )
                return result
            except Exception as err:
                last_error = err
                dt = time.monotonic() - t0
                status = self._status_code(err)
                attempts.append({
                    "attempt": attempt, "model": use_model, "status": "error",
                    "duration_ms": round(dt * 1000), "error_type": type(err).__name__,
                    "error": str(err), "status_code": status,
                    "raw_response": raw_content, "usage": usage,
                })
                with self._health_lock:
                    self.api_stats["attempt_errors"] += 1
                if self._is_timeout(err):
                    with self._health_lock:
                        self.api_stats["timeouts"] += 1
                    _log.warning(f"API✗ 座位{seat_number} 用时{dt:.1f}s 思考超时(>{self.agent_timeout}s)→立即兜底")
                    return self._finish_failed_request(
                        request_id, seat_number, response_kind, mem.get("role"),
                        requested_model, use_model, request_started_wall, request_t0,
                        attempts, "思考超时", last_error,
                    )

                if status in (401, 402, 403):
                    reason = {
                        401: "API Key 无效或未授权",
                        402: "API 余额不足",
                        403: "API 无权限访问该模型",
                    }[status]
                    _log.error(f"API✗ 座位{seat_number} HTTP {status} {reason}→不重试并暂停自动推演")
                    return self._finish_failed_request(
                        request_id, seat_number, response_kind, mem.get("role"),
                        requested_model, use_model, request_started_wall, request_t0,
                        attempts, reason, last_error, fatal_error=f"HTTP {status}: {reason}",
                    )

                if (not switched and self.fallback_model
                        and use_model != self.fallback_model
                        and self._looks_like_model_error(err)):
                    _log.warning(f"API✗ 座位{seat_number} 用时{dt:.1f}s 模型{use_model}不可用→回退 {self.fallback_model}")
                    use_model = self.fallback_model
                    switched = True
                    attempt -= 1
                    continue

                if status == 429:
                    wait_s = min(8.0, self._retry_after(err) or (0.75 * (2 ** (attempt - 1))))
                    _log.warning(f"API限流 座位{seat_number}，{wait_s:.1f}s后重试（第{attempt}次）")
                    time.sleep(wait_s)
                    continue

                if status is not None and 400 <= status < 500:
                    _log.error(f"API✗ 座位{seat_number} HTTP {status} 客户端请求错误→不重试")
                    return self._finish_failed_request(
                        request_id, seat_number, response_kind, mem.get("role"),
                        requested_model, use_model, request_started_wall, request_t0,
                        attempts, f"请求错误 HTTP {status}", last_error,
                    )

                _log.warning(
                    f"API✗ 座位{seat_number} 用时{dt:.1f}s 错误:{type(err).__name__}:{err}"
                    f"（第{attempt}次，重试）"
                )
                time.sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))

        _log.error(f"API✗ 座位{seat_number} 重试耗尽→兜底。最后错误:{last_error}")
        return self._finish_failed_request(
            request_id, seat_number, response_kind, mem.get("role"),
            requested_model, use_model, request_started_wall, request_t0,
            attempts, "系统故障", last_error,
        )

    def _note_api_success(self) -> int:
        with self._health_lock:
            self._consecutive_failures = 0
            self.api_stats["success"] += 1
            return self._consecutive_failures

    def _note_api_failure(self, force_break: bool = False) -> tuple[int, bool]:
        with self._health_lock:
            self._consecutive_failures += 1
            self.api_stats["fallback"] += 1
            tripped = force_break or self._consecutive_failures >= self._failure_threshold
            if tripped and (force_break or self._consecutive_failures == self._failure_threshold):
                self.api_stats["circuit_breaks"] += 1
            return self._consecutive_failures, tripped

    def _finish_failed_request(
        self, request_id, seat, response_kind, role, requested_model, final_model,
        started_at, request_t0, attempts, reason, last_error, fatal_error="",
    ) -> dict:
        consecutive, tripped = self._note_api_failure(force_break=bool(fatal_error))
        if tripped and not fatal_error:
            fatal_error = (
                f"连续{consecutive}个模型请求失败，已触发API熔断；"
                "请检查网络/服务商后再点击继续"
            )
        result = self._fallback_payload(reason, fatal_error=fatal_error)
        self._record_api(
            record_type="completed", request_id=request_id, status="fallback",
            seat=seat, role=role, response_kind=response_kind,
            requested_model=requested_model, final_model=final_model,
            started_at=started_at,
            duration_ms=round((time.monotonic() - request_t0) * 1000),
            attempts=attempts, normalized_response=result,
            failure_reason=reason,
            last_error={"type": type(last_error).__name__, "message": str(last_error)},
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
        cache_hit = as_int(usage.get("prompt_cache_hit_tokens"))
        if not cache_hit:
            cache_hit = as_int(prompt_details.get("cached_tokens"))
        cache_miss = as_int(usage.get("prompt_cache_miss_tokens"))
        if not cache_miss and prompt:
            cache_miss = max(0, prompt - cache_hit)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
            "reasoning_tokens": as_int(completion_details.get("reasoning_tokens")),
        }

    def _accumulate_usage(self, usage: dict | None, response_kind: str) -> None:
        """线程安全累计每次真正返回的 API 用量；不依赖归档文件扫描。"""
        kind = str(response_kind or "unknown")
        values = self._usage_values(usage)
        with self._usage_lock:
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
            return 0.0

    def _fallback_payload(self, reason: str, fatal_error: str = "") -> dict:
        fb = {
            "reasoning": reason, "speech": "过/弃权", "vote_target": 0,
            "is_explode": False, "explode_target": 0,
            "is_duel": False, "duel_target": 0,
            "badge_target": 0, "join_police": False, "withdraw": False,
            "_is_fallback": True, "_fallback_reason": reason,
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
    def _summarize_one(self, seat_number: int):
        """压缩单个座位：把 旧备忘录+近期细节 → 新的第一人称备忘录；失败则 FIFO 截断兜底。"""
        mem = self.agent_memory[seat_number]
        if not mem["recent"]:
            return ("skip", seat_number)
        _log = config.get_logger()
        request_id = "summary-" + uuid.uuid4().hex
        before_summary = mem["summary"]
        before_recent = list(mem["recent"])
        started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        request_t0 = time.monotonic()
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
                self.summary_timeout,                            # 真·墙钟硬超时(线程 join 兜底)
                model=self.summary_model, messages=messages,
                temperature=0.2, timeout=self.summary_timeout,   # 摘要用低温 + 短超时
            )
            new_summary = (completion.choices[0].message.content or "").strip()
            usage = self._usage_dict(getattr(completion, "usage", None))
            self._accumulate_usage(usage, "memory_summary")
            if not new_summary:
                raise ValueError("空摘要")
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
                usage=usage,
            )
            self._record_memory(
                "compression_completed", seat=seat_number, request_id=request_id,
                status="success", previous_summary=before_summary,
                new_summary=mem["summary"], discarded_recent=before_recent,
            )
            return ("ok", seat_number)
        except Exception as e:
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
                last_error={"type": type(e).__name__, "message": str(e)},
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
            futures = [ex.submit(self._summarize_one, s) for s in seats]
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
        return {
            "reasoning": str(data.get("reasoning", "")).strip() or "（无内心独白）",
            "speech": str(data.get("speech", "")).strip() or "过。",
            "vote_target": self._coerce_seat(data.get("vote_target", 0)),
            "is_explode": bool(data.get("is_explode", False)),
            "explode_target": self._coerce_seat(data.get("explode_target", 0)),   # ★V6.4 白狼王带人目标
            "is_duel": bool(data.get("is_duel", False)),                          # ★V6.5 骑士决斗
            "duel_target": self._coerce_seat(data.get("duel_target", 0)),         # ★V6.5 骑士决斗目标
            "badge_target": self._coerce_seat(data.get("badge_target", 0)),
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
        if code in (400, 404):
            return True
        msg = str(err).lower()
        return any(k in msg for k in ("model", "not found", "does not exist", "不存在", "无效"))
