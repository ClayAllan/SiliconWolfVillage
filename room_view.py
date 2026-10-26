"""White-listed spectator data. No engine/client/configuration crosses this boundary."""
from copy import deepcopy


def audience_key(view, seat=None):
    if view not in {"public", "seat", "omniscient"}:
        raise ValueError("无效观战权限")
    if view == "seat":
        if type(seat) is not int or not 1 <= seat <= 12:
            raise ValueError("无效席位")
        return f"seat:{seat}"
    if seat is not None:
        raise ValueError("该权限不能指定席位")
    return view


def project_room(engine, view="public", seat=None):
    """Capture knowledge at this publication boundary, never reconstruct it from a final state."""
    audience_key(view, seat)
    god = view == "omniscient"
    wolf = view == "seat" and seat in engine.wolves
    visible = set(engine.players) if god else ({seat} | set(engine.wolves) if wolf else {seat})
    published = getattr(engine, "published_alive", {})
    players = {}
    for s, p in engine.players.items():
        alive = bool(p["alive"]) if god else published.get(s)
        players[str(s)] = {"role": p["role"] if s in visible else "未知",
                           "alive": alive, "status": "未知" if alive is None else "存活" if alive else "已出局"}
    rows = []
    for original in engine.public_log:
        if original.get("game_id", engine.game_id) != engine.game_id:
            continue
        kind = original.get("kind")
        if kind == "win" and not engine.winner:
            continue
        if kind not in ({"judge", "player", "votechart", "win", "god"} if god else {"judge", "player", "votechart", "win"}):
            continue
        row = {k: deepcopy(original[k]) for k in ("kind", "text", "seat", "phase", "title", "totals", "votes", "winner", "sheriff", "decision_sources") if k in original}
        if kind == "judge" and "API 已暂停" in row.get("text", ""):
            row["text"] = "对局已暂停，请等待房主处理。"
        # Audience-local cursors do not disclose hidden event counts.
        row["event_seq"] = len(rows) + 1
        rows.append(row)
    reasoning = {}
    skills = {}
    for s in (engine.players if god else [seat] if view == "seat" else []):
        reasoning[str(s)] = [{k: str(r.get(k, "")) for k in ("label", "reasoning", "speech")}
                             for r in engine.reasoning_log.get(s, [])]
        skills[str(s)] = deepcopy(engine.roles[s].skill_state())
    current = engine.current_speaker if god else getattr(engine, "public_speaker", None)
    if view == "seat" and engine.current_speaker == seat:
        current = seat
    # A finalized board is delivered only to the participating wolves.
    board = engine.war_board if god or (wolf and seat in getattr(engine, "war_board_recipients", [])) else None
    lovers = list(engine.lovers) if engine.lovers and (god or seat in engine.lovers or (view == "seat" and engine.players[seat]["role"] == "丘比特")) else None
    winner = engine.winner if god or any(r["kind"] == "win" for r in rows) else None
    return {"game_id": engine.game_id, "board_name": engine.board_name, "day": engine.day,
            "phase": engine.phase if god else ("游戏结束" if winner else f"第{engine.day}{'夜' if '夜' in engine.phase or engine.day == 0 else '天'}"),
            "winner": winner, "sheriff": engine.sheriff, "players": players,
            "current_speaker": current, "public_log": rows, "reasoning_log": reasoning,
            "skill_states": skills, "war_board": deepcopy(board), "lovers": lovers,
            "view": view, "viewer_seat": seat}
