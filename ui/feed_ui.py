"""对局动态的有界容器与定向滚动控件。"""
import json

from service.live_feed import feed_identity, prepare_feed_state, request_scroll, unread_events


def scroll_script(identity, command, sequence):
    # identity为哈希生成的CSS标识，脚本不插入任何模型或用户消息。
    settings = json.dumps({"selector": ".st-key-" + identity, "command": command,
                           "token": f"{identity}:{sequence}"})
    return """<script>(()=>{
      const settings = SETTINGS;
      let attempts = 0;
      const apply = () => {
        const root = document.querySelector(settings.selector);
        if (!root) { if (++attempts < 15) requestAnimationFrame(apply); return; }
        if (root.dataset.wvScrollToken === settings.token) return;
        const candidates = [root, ...root.querySelectorAll('*')];
        const box = candidates.find(el => ['auto','scroll'].includes(getComputedStyle(el).overflowY)
                                          && el.clientHeight > 0);
        if (!box) { if (++attempts < 15) requestAnimationFrame(apply); return; }
        requestAnimationFrame(() => requestAnimationFrame(() => {
          if (!root.isConnected) return;
          box.scrollTop = settings.command === 'top' ? 0 : box.scrollHeight;
          root.dataset.wvScrollToken = settings.token;
        }));
      };
      requestAnimationFrame(apply);
    })();</script>""".replace("SETTINGS", settings)


def render_feed(st, engine, render_entry, empty_html, *, show_god, full_history, limit=90):
    state = st.session_state
    game_id = engine.game_id if engine else "empty"
    mode = state.get("viewer_mode", "全知视角")
    seat = state.get("viewer_seat", 1) if mode == "单一视角" else 0
    prepare_feed_state(state, (game_id, mode, seat))
    channel = st.radio("频道", ["全部动态", "玩家发言", "法官播报"], horizontal=True,
                       label_visibility="collapsed", key="feed_channel")
    identity = feed_identity(game_id, mode, seat, channel)
    if state.get("_feed_channel_identity") != identity:
        state["_feed_channel_identity"] = identity
        state["_feed_scroll_action"] = None
        state["_feed_seen_seq"] = 0
        state["_feed_mark_seen"] = True
    controls = st.columns([1.45, 1, 1])
    controls[0].toggle("跟随最新", key="feed_follow_latest")
    controls[1].button("⬇️ 沉底一次", key="feed_bottom", use_container_width=True,
                       on_click=request_scroll, args=(state, "bottom"))
    controls[2].button("⬆️ 顶部", key="feed_top", use_container_width=True,
                       on_click=request_scroll, args=(state, "top"))
    rows = [r for r in engine.public_log if show_god or r.get("kind") != "god"] if engine else []
    if channel == "玩家发言":
        rows = [r for r in rows if r.get("kind") == "player"]
    elif channel == "法官播报":
        rows = [r for r in rows if r.get("kind") in {"judge", "win", "votechart"}]
    unseen = unread_events(state, rows)
    following = state["feed_follow_latest"]
    status = "正在跟随最新" if following else f"已暂停跟随 · {unseen} 条新动态"
    st.caption(status + (f" · 对局 {game_id[:8]}" if engine else ""))
    visible = rows if full_history else rows[-max(1, int(limit)):]
    with st.container(height=480, border=True, key=identity, autoscroll=following):
        # 两个槽位始终存在，90条阈值切换不会改变正文的位置。
        notice, content = st.empty(), st.empty()
        if len(rows) > len(visible):
            notice.caption(f"仅展示最近 {len(visible)} 条；完整内容保存在本局归档。")
        else:
            notice.empty()
        content.markdown("".join(render_entry(row) for row in visible) if visible else empty_html,
                         unsafe_allow_html=True)
    command_slot = st.empty()
    # 单次命令只投递一帧，避免之后关闭跟随时重放旧的“顶部/沉底”。
    action = state.pop("_feed_scroll_action", None)
    sequence = state["_feed_scroll_seq"]
    if following:
        # 原生 autoscroll 会在手动上翻后停止粘底。显式跟随模式在新事件到达时
        # 恢复到底部，游标也覆盖最近90条窗口长度不再增长的情况。
        action = "bottom"
        tail = rows[-1].get("event_seq", len(rows)) if rows else 0
        sequence = f"follow:{sequence}:{tail}:{len(rows)}:{len(visible)}"
    if action:
        with command_slot:
            st.html(scroll_script(identity, action, sequence),
                    unsafe_allow_javascript=True)
    else:
        command_slot.empty()
    return identity
