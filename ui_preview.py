"""固定示例快照。仅用于界面预览，不创建后台服务、网络客户端或 API 请求。"""
from game_engine import GameEngine
from roles import create_role
import config


class PreviewManager:
    def get_usage_snapshot(self):
        return {"total_tokens": 0}


def preview_engine():
    engine = GameEngine(PreviewManager())
    engine.game_id = "preview-only"
    engine.board_name = config.DEFAULT_BOARD
    engine.day = 2
    engine.phase = "第2天 · 讨论阶段"
    engine.sheriff = 3
    engine.current_speaker = 7
    names = ["狼人","女巫","预言家","平民","守卫","狼人","平民","猎人","狼人","平民","狼人","平民"]
    styles = ["谨慎观察", "冷静分析", "证据优先", "直言快语"]
    engine.players = {s: {"role": name, "alive": s != 10, "style": styles[(s-1)%4], "memory": "示例快照"}
                      for s, name in enumerate(names, 1)}
    engine.roles = {s: create_role(p["role"], s) for s, p in engine.players.items()}
    engine.wolves = [s for s in engine.players if engine.roles[s].camp == "狼人"]
    engine.role_seats = {p["role"]:s for s,p in engine.players.items() if engine.roles[s].is_god}
    engine.public_log = [
        {"kind":"judge","phase":"第2天","text":"天亮了，昨夜 10 号玩家出局。现在进入白天讨论阶段。"},
        {"kind":"player","seat":3,"phase":"第2天 · 发言","text":"先把昨天的票型放在一起看。6 号和 9 号都在最后改票，但给出的理由不一样。今天我希望两位分别解释，不要用同一套说法互相作证。"},
        {"kind":"player","seat":6,"phase":"第2天 · 发言","text":"我的改票来自 4 号的补充发言。3 号一直在把改票等同于狼队协同，我认为这个前提并不成立。"},
        {"kind":"god","phase":"第2夜 · 私密","text":"预言家查验 7 号：好人。该信息为示例中的秘密行动。"},
        {"kind":"player","seat":7,"phase":"第2天 · 发言","text":"我更在意大家的判断有没有变化。6 号解释了为什么改票，但 9 号还没有回应。先听完发言，再决定今天的票。"},
    ]
    for seat in range(1,13):
        engine.reasoning_log[seat] = [{"label":"第2天 · 发言前","reasoning":"我会先核对公开发言与票型，再判断哪些解释仍然成立。这里展示的是编写的界面示例，并非真实模型运行结果。", "speech":"先核对证据，再决定站边。"}]
    engine.reasoning_log[7][-1]["speech"] = engine.public_log[-1]["text"]
    engine.reasoning_log[7][-1]["reasoning"] = "3 号提出了可以逐项核对的疑点，6 号给出了解释，但 9 号还没有回应。暂不把两人打成同一阵营，等发言结束后再比较动机。此内容为静态示例。"
    return engine
