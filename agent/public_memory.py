"""公开证词的有界展示；输入只能来自公屏玩家发言，不接受底牌或私密推理。"""
import re


def select_evidence(entries, cursor=0, max_chars=16000):
    latest = {row["seat"]: row["id"] for row in entries}
    ranked = []
    for row in entries:
        text = row["text"]
        # 保留原话和出处，不把角色/查验声明升级为事实。
        claim = bool(re.search(r"查验|查杀|金水|守墓|救|毒|守护|我是|我跳|我查|我验|警徽流", text))
        unseen = row["id"] not in cursor if isinstance(cursor, (list, set)) else row["id"] > cursor
        rank = (unseen, claim, latest[row["seat"]] == row["id"], row["id"])
        ranked.append((rank, row))
    selected, size = [], 0
    for _, row in sorted(ranked, key=lambda pair: pair[0], reverse=True):
        line = f"[公开事件{row['id']} / {row['phase']} / {row['seat']}号声称] {row['text']}"
        if size + len(line) + bool(selected) <= max_chars:
            selected.append((row["id"], line))
            size += len(line) + (len(selected) > 1)
    selected.sort()
    ids = [i for i, _ in selected]
    chosen = set(ids)
    omitted = [row["id"] for row in entries if row["id"] not in chosen]
    return "\n".join(line for _, line in selected), ids, omitted


from engine.runtime_version import register_source as _register_source
_register_source(__name__, __file__)
