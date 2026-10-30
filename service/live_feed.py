"""实时动态的数据边界与滚动状态；不推进对局，也不调用模型。"""
import hashlib
from copy import deepcopy

from storage.immutable_log import FrozenDict


def feed_identity(game_id, viewer_mode, viewer_seat, channel):
    value = f"{game_id}|{viewer_mode}|{viewer_seat}|{channel}"
    return "live_feed_" + hashlib.sha256(value.encode()).hexdigest()[:20]


def project_events(rows, game_id, winner, *, limited=False):
    """Project event rows without exposing mutable engine-owned payloads.

    Frozen production rows can be reused for an authorized full view. A limited
    view gets a new top-level dict with private role data removed; its nested
    values are safe to share because the source row is recursively immutable.
    Legacy or unsupported mutable rows are deep-copied at this boundary.
    """
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

        can_share_full_row = (
            isinstance(original, FrozenDict)
            and not limited
            and original.get("game_id", game_id) == game_id
            and "event_seq" in original
        )
        if can_share_full_row:
            row = original
        elif isinstance(original, FrozenDict):
            row = dict(original)
        else:
            row = deepcopy(dict(original))

        if row is not original:
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


def select_feed_window(state, rows, *, show_god, channel, full_history, limit=90):
    """Select visible events from the tail without filtering the full log on every refresh.

    Event sequence numbers are monotonic within a game. For the bounded live view,
    scan newest-to-oldest until the visible window and unread cursor are both resolved.
    """
    def matches(row):
        if not show_god and row.get("kind") == "god":
            return False
        if channel == "玩家发言":
            return row.get("kind") == "player"
        if channel == "法官播报":
            return row.get("kind") in {"judge", "win", "votechart"}
        return True

    if full_history:
        visible = [row for row in rows if matches(row)]
        unseen = unread_events(state, visible)
        tail = visible[-1].get("event_seq", len(visible)) if visible else 0
        return visible, unseen, False, tail

    cap = max(1, int(limit))
    follow = bool(state.get("feed_follow_latest"))
    mark_seen = state.pop("_feed_mark_seen", False)
    seen_before = int(state.get("_feed_seen_seq", 0) or 0)
    selected_reversed = []
    unseen = 0
    tail = 0
    truncated = False

    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        if not matches(row):
            continue
        seq = int(row.get("event_seq", index + 1))
        if not tail:
            tail = seq
        if len(selected_reversed) < cap:
            selected_reversed.append(row)
        else:
            truncated = True
        if not follow and not mark_seen and seq > seen_before:
            unseen += 1
        # Stop only after observing an additional matching row beyond the
        # visible window. While paused, keep counting unseen rows until the
        # previous seen cursor is reached; with no cursor, exact unread counts
        # necessarily require scanning the remaining matching history.
        if truncated and (follow or mark_seen or seq <= seen_before):
            break

    visible = list(reversed(selected_reversed[:cap]))
    if follow or mark_seen:
        state["_feed_seen_seq"] = tail
        unseen = 0
    return visible, unseen, truncated, tail


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
