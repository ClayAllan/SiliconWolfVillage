"""Read legacy evidence conservatively: ordered public text, never invented past boards."""
import json


def legacy_public_rows(raw):
    if len(raw) > 32 * 1024 * 1024:
        raise ValueError("旧档超过 32 MB")
    text = raw.decode("utf-8-sig")
    issue = "旧档仅支持公开事件浏览；历史身份、技能和棋盘状态未重建。"
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = []
        for line in text.splitlines():
            try:
                data.append(json.loads(line))
            except json.JSONDecodeError:
                issue += " 文件存在损坏行。"
                break
    if isinstance(data, dict):
        rows = data.get("public_log", [])
    elif isinstance(data, list):
        rows = [r.get("entry", {}) for r in data if isinstance(r, dict) and r.get("event") in {"public_log", "vote_result"}]
    else:
        raise ValueError("不支持的旧档格式")
    clean = []
    game_id = None
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in {"player", "judge", "votechart", "win"}:
            continue
        row_game = row.get("game_id")
        game_id = game_id or row_game
        if row_game and row_game != game_id:
            issue += " 已忽略其他对局事件。"
            continue
        # No nested state, role, response, diagnostics or source metadata are forwarded.
        if row["kind"] == "votechart":
            summary = "票型记录：" + str(row.get("title", ""))
            votes = row.get("votes", {})
            if isinstance(votes, dict):
                summary += "；" + "，".join(f"{s}号→{v}号" for s, v in votes.items() if str(s).isdigit() and str(v).isdigit())
        else:
            summary = str(row.get("text", ""))
        if "API 已暂停" in summary:
            summary = "对局已暂停。"
        clean.append({"kind": row["kind"], "text": summary, "phase": str(row.get("phase", "")),
                      "seat": row.get("seat") if type(row.get("seat")) is int else None})
    return {"rows": clean, "issue": issue}
