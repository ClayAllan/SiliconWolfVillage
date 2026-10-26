# -*- coding: utf-8 -*-
"""
config.py —— 全局配置中心
=========================
集中管理 API 凭证、模型参数与游戏基础参数。
所有其它模块都从这里读取配置，便于一处修改、全局生效。
"""

import os
from engine.roles import ROLE_CLASSES

# ============================================================
# 一、大模型 API 配置（兼容 OpenAI 格式，默认适配 DeepSeek）
# ============================================================
# 推荐用环境变量注入 Key，避免把密钥写死在代码里：
#   Windows PowerShell:  $env:DEEPSEEK_API_KEY = "sk-xxxx"
#   也可以直接把下面的占位符替换成你的真实 Key（不推荐提交到仓库）。
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "sk-请在这里填入你的Key或在网页左侧输入框填写")

# DeepSeek 官方地址；用 "https://api.deepseek.com/v1" 也可以。
# 如果想换成 OpenAI 官方：BASE_URL = "https://api.openai.com/v1"，并把下面两个阵营模型改成如 "gpt-4o-mini"。
BASE_URL = "https://api.deepseek.com"

# ============================================================
# 阵营多模型对战（V6.0+）：好人阵营 / 狼人阵营 各用一个模型，
#   用来对比测试不同 LLM 的“撒谎(狼) / 识谎(好人)”能力。
#   路由在 AgentManager.get_ai_response() 里按座位【真实身份】动态判定：
#     座位身份为“狼人” → 发往 WOLF_CAMP_MODEL；其余(神职/平民) → 发往 GOOD_CAMP_MODEL。
#   当前可用的两个【真实】模型：
#     · deepseek-v4-flash —— 快、便宜，默认主力；摘要/回退也用它。
#     · deepseek-v4-pro   —— 带深层思维链(CoT)，更聪明但更慢(AGENT_TIMEOUT 已放宽到 180s 适配)。
GOOD_CAMP_MODEL = os.getenv("GOOD_CAMP_MODEL", "deepseek-flash")
WOLF_CAMP_MODEL = os.getenv("WOLF_CAMP_MODEL", "deepseek-flash")

# 侧边栏两个下拉框的候选模型（上帝可随时切换双方模型）。按需增删真实可用模型名即可。
MODEL_OPTIONS = [
    "deepseek-flash",
    "deepseek-v4-pro",
    "deepseek-chat",
    "deepseek-reasoner",
]

# 回退模型：当某阵营模型不可用/超时时自动切到它（必须真实可用）。摘要任务也用它（取更快的 flash）。
MODEL_FALLBACK = "deepseek-flash"

# 采样温度：略降到 0.8，减少“凭空编造剧情/多守了几晚”这类幻觉，逻辑更稳；
# 想要更多“演技/随机性”可在网页滑块调到 1.0~1.2。
TEMPERATURE = 0.8

# 单个 AI “思考”的硬超时（秒）。既防卡死，又要给真实模型留足出字时间。
# ★ 放宽到 180s(3分钟)：适配 deepseek-v4-pro 等带深层思维链(CoT)的模型，给足深度推理时间，
#   避免深度思考被强行兜底成“过/弃权”。（app.py 的“僵死锁”阈值会基于本值自动 +30s，无需手改。）
AGENT_TIMEOUT = 180

# 记忆压缩(动态滚动摘要)单次超时（秒）。摘要任务比发言轻、且用更快的回退模型，给 20s 足够；
# 超时则该座位 FIFO 截断兜底，绝不卡死。
SUMMARY_TIMEOUT = 20

# 只有某位玩家的短期工作台达到该字符数，才在入夜时调用一次摘要模型。
# 低于阈值直接保留原文，避免“每天固定 12 次摘要”成为最大的 token 开销。
MEMORY_COMPRESS_MIN_CHARS = 9000

# 摘要模型偶尔会无视字数要求；由 Python 再做硬上限，防止摘要层自身继续膨胀。
SUMMARY_MAX_CHARS = 700

# 兼容保留（已被 AGENT_TIMEOUT 取代为主要超时来源）
REQUEST_TIMEOUT = 60

# 每个完整行动最多调用两次（含首次），共用 AGENT_TIMEOUT 墙钟预算。
MAX_RETRIES = 2

# 生成预算包括隐式推理；预算不足时仅允许一次有限扩容。
QUICK_OUTPUT_TOKENS = 4096
QUICK_REPAIR_TOKENS = 6144
ACTION_OUTPUT_TOKENS = 8192
ACTION_REPAIR_TOKENS = 12288
SUMMARY_OUTPUT_TOKENS = 1200
DIAGNOSTIC_OUTPUT_TOKENS = 256

# 连续多少个【完整玩家请求】失败后暂停本局。防止供应商整体断线时，12名玩家全部用兜底继续跑出无效样本。
API_CIRCUIT_BREAKER_THRESHOLD = 3

# ★V6.7 反应式抢爆/抢决斗预算（吞发言用）：白天每出现一次“查杀/归票”等威胁发言，
#   才给存活的狼/白狼王/骑士【私下】一次"现在要不要立刻自爆/决斗"的机会——
#   让自爆能真正【吞掉后续发言与投票】，而不是只能在自己发言回合才爆。
# ⚠️这是“受控的”反应式轮询：用【每天硬上限 N 次额外调用】严格封顶，绝不退化成
#   “每人发言后全场狼都请求”的无界轮询(那会单日破百→APIConnectionError+卡顿)。
#   设为 0 = 关闭(回到纯自己回合自爆)；调大 = 更灵敏但更多 API/延迟。
EXPLODE_REACT_BUDGET = 3

# 前端默认只渲染频道尾部，完整记录仍持续写入本局归档。
# 这样不会因数百条 Markdown 组件反复全量下发而压垮 Streamlit WebSocket。
PUBLIC_LOG_RENDER_LIMIT = 90
REASONING_RENDER_LIMIT = 36

# ============================================================
# 二、UI / 推演节奏配置
# ============================================================
# 每推进一步后的额外停顿（秒），让观众有时间“看戏”。0 表示不额外等待。
STEP_DELAY = 0.6

# ============================================================
# 三、游戏基础参数（12 人局：4 狼 4 民 + 预言家/女巫/猎人/守卫）
# ============================================================
# 游戏板子（V6.4）：可在前端侧边栏切换，开新局时把对应身份列表发给 GameEngine。
#   每个板子必须恰好 12 个身份。白狼王(camp=狼人)拥有白天自爆带人特权，屠边判负仍按 camp 计狼。
GAME_BOARDS = {
    "12人标准预女猎守": ["狼人"] * 4 + ["平民"] * 4 + ["预言家", "女巫", "猎人", "守卫"],
    "12人白狼王守卫": ["白狼王"] * 1 + ["狼人"] * 3 + ["平民"] * 4 + ["预言家", "女巫", "猎人", "守卫"],
    "12人白狼王守墓人": ["白狼王"] * 1 + ["狼人"] * 3 + ["平民"] * 4 + ["预言家", "女巫", "猎人", "守墓人"],
    "12人狼美骑士": ["狼美人"] * 1 + ["狼人"] * 3 + ["平民"] * 4 + ["预言家", "女巫", "骑士", "守卫"],
    "12人丘比特": ["狼人"] * 4 + ["平民"] * 4 + ["预言家", "女巫", "猎人", "丘比特"],
    "12人摄梦人恶灵骑士": ["恶灵骑士"] * 1 + ["狼人"] * 3 + ["平民"] * 4 + ["预言家", "女巫", "猎人", "摄梦人"],
    "12人魔术师白狼王": ["白狼王"] * 1 + ["狼人"] * 3 + ["平民"] * 4 + ["魔术师", "女巫", "猎人", "守卫"],
}
DEFAULT_BOARD = "12人标准预女猎守"

# 神职阵营（屠边局判负用）：神全死 或 民全死 → 狼胜。
#   ⚠️守墓人/骑士/丘比特都是【神职】，必须计入；狼美人/白狼王是【狼】(camp)，不在这里。
#   跨阵营情侣存活时，由引擎优先处理情侣第三方胜负。
#   从角色注册表派生，新增神职无需再维护第二份名单。
GOD_ROLES = frozenset(name for name, cls in ROLE_CLASSES.items() if cls.is_god)

# 给每个 AI 随机分配的“说话风格”，增加观赏性与多样性
STYLE_POOL = [
    "激进刚猛、喜欢冲锋陷阵",
    "沉稳老练、滴水不漏",
    "划水摸鱼、能少说就少说",
    "心理博弈、擅长揣摩人心",
    "逻辑流、用证据链说话",
    "悍跳狂魔、动不动就跳身份",
    "卖萌装弱、博取同情",
    "毒舌嘴炮、句句带刺",
    "理中客、喜欢和稀泥",
    "戏精影帝、表演型人格",
    "阴谋论者、谁都怀疑",
    "老好人、谁也不想得罪",
]

# 身份对应的头像 emoji（上帝视角圆桌用）
ROLE_EMOJI = {name: cls.emoji for name, cls in ROLE_CLASSES.items()}
ROLE_EMOJI["未知"] = "❓"

# ============================================================
# 四、运行日志（排查“卡顿/超时/崩溃”用）
# ============================================================
import logging  # noqa: E402
from logging.handlers import RotatingFileHandler  # noqa: E402

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime.log")
_LOGGER = None


def get_logger():
    """全局单例日志器：写入项目目录下的 runtime.log（自动滚动，最多 ~9MB）。"""
    global _LOGGER
    if _LOGGER is not None:
        return _LOGGER
    lg = logging.getLogger("cyber_wolf")
    lg.setLevel(logging.INFO)
    lg.handlers.clear()
    lg.propagate = False
    try:
        fh = RotatingFileHandler(LOG_FILE, maxBytes=3_000_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"))
        lg.addHandler(fh)
    except Exception:
        lg.addHandler(logging.NullHandler())
    _LOGGER = lg
    return _LOGGER
