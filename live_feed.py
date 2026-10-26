"""实时动态的数据边界与滚动状态；不推进对局，也不调用模型。"""
import hashlib


def feed_identity(game_id, viewer_mode, viewer_seat, channel):
    value = f"{game_id}|{viewer_mode}|{viewer_seat}|{channel}"
    return "live_feed_" + hashlib.sha256(value.encode()).hexdigest()[:20]


def project_events(rows, game_id, winner, *, limited=False):
    """只投影当前引擎持有的日志。无ID旧行仅在该明确快照边界兼容。"""
    result, issues = [], []
    for index, original in enumerate(rows):
        if original.get("game_id", game_id) != game_id:
            issues.append("foreign_game")
            continue
        if original.get("kind") == "win" and not winner:
            issues.append("premature_win")
            continue
        if limited and original.get("kind") not in {"judge", "player", "votechart", "win"}:
            continue
        row = dict(original)
        row["game_id"] = game_id
        row["event_seq"] = original.get("event_seq", index + 1)
        if limited:
            row.pop("role", None)
        result.append(row)
    return result, tuple(issues)


def prepare_feed_state(state, scope):
    if state.get("_feed_scope") != scope:
        state["_feed_scope"] = scope
        state["feed_follow_latest"] = True
        state["_feed_seen_seq"] = 0
        state["_feed_scroll_action"] = None
        state["_feed_scroll_seq"] = state.get("_feed_scroll_seq", 0) + 1
        state["_feed_channel_identity"] = None


def request_scroll(state, action):
    if action == "top":
        state["feed_follow_latest"] = False
    state["_feed_scroll_action"] = action
    state["_feed_scroll_seq"] = state.get("_feed_scroll_seq", 0) + 1
    if action == "bottom":
        state["_feed_mark_seen"] = True


def unread_events(state, rows):
    tail = max((r["event_seq"] for r in rows), default=0)
    mark_seen = state.pop("_feed_mark_seen", False)
    if state.get("feed_follow_latest") or mark_seen:
        state["_feed_seen_seq"] = tail
    return sum(r["event_seq"] > state.get("_feed_seen_seq", 0) for r in rows)


def schedule_page_refresh(st, enabled, interval):
    """首次调用正常返回，让整页完成清理；后续定时tick才请求下一次完整刷新。"""
    first_call = True

    @st.fragment(run_every=interval if enabled else None)
    def refresh_tick():
        nonlocal first_call
        if first_call:
            first_call = False
            return
        st.rerun(scope="app")

    refresh_tick()
