"""Read-only spectator UI; no imports of model clients or game constructors."""
import hashlib
import json
from pathlib import Path
import time
import uuid

from room_registry import get_registry
from replay_frames import DisplayReplay
from replay_reader import legacy_public_rows
from live_feed import schedule_page_refresh
from feed_ui import scroll_script
import ui_design as design


def feed_command(state, key, action):
    if action == "top":
        state[key + "_follow"] = False
    state[key + "_scroll"] = action
    state[key + "_scroll_token"] = uuid.uuid4().hex


def render_board(st, view):
    st.subheader(f"{view['phase']} · {view['winner'] + '获胜' if view['winner'] else '对局观战'}")
    st.caption({"public": "公开观众", "omniscient": "全知观众", "seat": f"{view.get('viewer_seat')} 号视角"}[view["view"]]
               + " · 只读观战；播放、回看不控制真实对局")
    cards = []
    for seat, p in view["players"].items():
        dead = " dead" if p["alive"] is False else ""
        badge = " · 警长" if int(seat) == view.get("sheriff") else ""
        current = " · 当前发言" if int(seat) == view.get("current_speaker") else ""
        cards.append(f'<div class="wv-player{dead}"><b>{design.text(seat)} 号 · {design.text(p["role"])}</b>'
                     f'<p>{design.text(p["status"] + badge + current)}</p></div>')
    st.markdown('<div class="wv-matrix">' + "".join(cards) + '</div>', unsafe_allow_html=True)


def render_player(st, read, key):
    """read(cursor) always revalidates authorization, including exports."""
    ss = st.session_state
    latest = read(None)
    timeline = latest["timeline"]
    if not timeline:
        st.info("尚无展示记录")
        return False
    cursor_key, playing_key, tick_key = key + "_cursor", key + "_playing", key + "_tick"
    cursor = ss.get(cursor_key)
    positions = {r["cursor"]: i for i, r in enumerate(timeline)}
    if cursor is not None and cursor not in positions:
        cursor = ss[cursor_key] = None
        ss[playing_key] = False
    index = positions.get(cursor, len(timeline) - 1)
    buttons = st.columns(4)
    if buttons[0].button("上一事件", key=key + "_prev", disabled=index == 0):
        ss[cursor_key], ss[playing_key] = timeline[max(0, index - 1)]["cursor"], False
        st.rerun()
    if buttons[1].button("下一事件", key=key + "_next", disabled=index == len(timeline) - 1):
        ss[cursor_key], ss[playing_key] = timeline[index + 1]["cursor"], False
        st.rerun()
    if buttons[2].button("暂停回放" if ss.get(playing_key) else "播放回放", key=key + "_play"):
        ss[playing_key] = not ss.get(playing_key, False)
        if cursor is None or index == len(timeline) - 1:
            ss[cursor_key] = timeline[0]["cursor"]
        ss[tick_key] = time.monotonic()
        st.rerun()
    if buttons[3].button("返回直播", key=key + "_live"):
        ss[cursor_key], ss[playing_key] = None, False
        st.rerun()
    speed = st.select_slider("回放速度", options=[0.5, 1.0, 2.0], value=1.0, key=key + "_speed")
    # Only visible frames are listed. The live engine is never advanced here.
    choice = st.selectbox("按天 / 阶段跳转", options=list(range(len(timeline))),
                          format_func=lambda i: f"{i + 1} · {timeline[i]['phase']}", key=key + "_jump")
    if st.button("跳到选定事件", key=key + "_jump_button"):
        ss[cursor_key], ss[playing_key] = timeline[choice]["cursor"], False
        st.rerun()
    if ss.get(playing_key) and time.monotonic() - ss.get(tick_key, 0) >= 1.5 / speed:
        index = min(index + 1, len(timeline) - 1)
        ss[cursor_key] = cursor = timeline[index]["cursor"]
        ss[tick_key] = time.monotonic()
        if index == len(timeline) - 1:
            ss[playing_key] = False
    packet = latest if cursor is None else read(cursor)
    view = packet["view"]
    st.caption("直播 · 最新可见画面" if cursor is None else f"正在回看 · {index + 1}/{len(timeline)} · 后方还有 {len(timeline) - index - 1} 个可见更新")
    if packet["issue"]:
        st.warning(packet["issue"])
    render_board(st, view)
    scope = hashlib.sha256(f"{key}:{cursor or 'live'}".encode()).hexdigest()[:16]
    if ss.get(key + "_feed_scope") != scope:
        ss[key + "_feed_scope"] = scope
        ss[key + "_seen"] = len(view["public_log"])
    controls = st.columns([2, 1, 1])
    following = controls[0].toggle("跟随当前画面最新动态", value=True, key=key + "_follow")
    controls[1].button("沉底一次", key=key + "_bottom", on_click=feed_command, args=(ss, key, "bottom"))
    controls[2].button("顶部", key=key + "_top", on_click=feed_command, args=(ss, key, "top"))
    action = ss.pop(key + "_scroll", None)
    if following or action == "bottom":
        ss[key + "_seen"] = len(view["public_log"])
    unseen = max(0, len(view["public_log"]) - ss.get(key + "_seen", 0))
    st.caption("跟随当前画面 · 最近 90 条" if following else f"暂停跟随 · {unseen} 条新动态 · 最近 90 条")
    with st.container(height=420, key="spectator_feed_" + scope, autoscroll=following):
        rows = view["public_log"][-90:]
        for row in rows:
            if row["kind"] == "votechart":
                st.markdown("**" + str(row.get("title", "票型")) + "**")
                st.json({"票型": row.get("votes", {}), "票重合计": row.get("totals", {})})
            else:
                st.markdown(design.event(row), unsafe_allow_html=True)
    scroll_slot = st.empty()
    if following or action:
        with scroll_slot:
            st.html(scroll_script("spectator_feed_" + scope, "bottom" if following else action,
                                  f"{len(view['public_log'])}:{ss.get(key + '_scroll_token', '')}"),
                    unsafe_allow_javascript=True)
    with st.expander("获准的私密信息"):
        if view["skill_states"]:
            st.json(view["skill_states"])
        if view.get("lovers"):
            st.write("已知情侣关系：", view["lovers"])
        if view["war_board"]:
            st.json(view["war_board"])
        for seat, entries in view["reasoning_log"].items():
            if entries:
                st.markdown(f"**{seat} 号**")
                st.markdown(design.reasoning(entries[-1]), unsafe_allow_html=True)
    if st.button("准备当前画面导出", key=key + "_export"):
        safe_view = read(cursor)["view"]
        st.download_button("下载已授权画面 JSON", json.dumps(safe_view, ensure_ascii=False, indent=2),
                           file_name="观战画面.json", mime="application/json", key=key + "_download")
    speeches = [r for r in view["public_log"] if r["kind"] == "player"]
    if speeches and st.button("逐字播放最近发言", key=key + "_animate"):
        # Completed and validated speech only; no model streaming or API calls.
        fresh = read(cursor)["view"]
        speech = next(r["text"] for r in reversed(fresh["public_log"]) if r["kind"] == "player")
        st.caption("已完成发言的播放效果；可关闭此区，不影响对局。")
        st.write_stream((speech[i:i + 8] for i in range(0, len(speech), 8)))
    return bool(ss.get(playing_key))


def spectator_entry(st, room_id):
    registry = get_registry()
    st.title("硅基狼村 · 观战与回放")
    ss = st.session_state
    ss.setdefault("spectator_browser", uuid.uuid4().hex)
    if ss.get("spectator_room") != room_id:
        ss.pop("spectator_token", None)
        ss["spectator_room"] = room_id
    if not ss.get("spectator_token"):
        with st.form("spectator_join"):
            owner_mode = st.checkbox("使用房主管理凭证")
            secret = st.text_input("邀请码或管理凭证", type="password")
            submitted = st.form_submit_button("加入观战")
        if submitted:
            try:
                if owner_mode:
                    registry.owner_read(room_id, secret)
                    ss["spectator_token"] = secret
                else:
                    ss["spectator_token"] = registry.join(room_id, secret, ss.spectator_browser)
                ss["spectator_is_owner"] = owner_mode
                st.rerun()
            except (PermissionError, ValueError) as error:
                st.error(str(error))
        st.caption("邀请码由房主提供；房间编号本身不授予权限。")
        return
    token = ss.spectator_token
    playing = False
    if st.button("退出观战"):
        try:
            registry.leave(room_id, token, ss.spectator_browser)
        except PermissionError:
            pass
        ss.pop("spectator_token", None)
        st.rerun()
    try:
        if ss.get("spectator_is_owner"):
            current = registry.owner_read(room_id, token)
            if current["issue"] and current["revision"] is not None and st.button("重试展示记录写入"):
                registry.control(room_id, token, uuid.uuid4().hex, current["revision"], "retry_display_recording")
                st.rerun()
            st.caption("真实对局控制 · 与回放位置独立")
            cols = st.columns(4)
            for col, command, label in zip(cols, ("pause", "play", "step", "request_skip_current"),
                                            ("暂停真实对局", "继续真实对局", "单步真实对局", "跳过当前玩家")):
                if col.button(label, disabled=current["done"], key="owner_control_" + command):
                    registry.control(room_id, token, uuid.uuid4().hex, current["revision"], command)
                    st.rerun()
            read = lambda cursor: registry.owner_read(room_id, token, cursor=cursor)
        else:
            read = lambda cursor: registry.read(room_id, token, ss.spectator_browser, cursor)
        playing = render_player(st, read,
                                "viewer_" + hashlib.sha256(token.encode()).hexdigest()[:12])
    except (PermissionError, ValueError, RuntimeError):
        # Complete the render to remove old nodes; never leave old private panels mounted.
        st.error("房间或权限已失效，请退出后重新加入。")
    schedule_page_refresh(st, True, 0.75 if playing else 1.5)


def owner_panel(st, replay_root):
    registry = get_registry()
    ss = st.session_state
    playing = False
    with st.expander("共享房间与独立回放"):
        st.caption("本机多浏览器可用。现有服务仅监听 127.0.0.1，尚未开放远程访问。下方原工作台始终显示实时对局。")
        receipts = ss.setdefault("owned_room_receipts", {})
        active_receipt = ss.get("room_receipt")
        if active_receipt and active_receipt[0] not in receipts:
            receipts[active_receipt[0]] = active_receipt
            ss["managed_room_id"] = active_receipt[0]
        receipt = None
        if receipts:
            selected = st.selectbox("管理的房间", list(receipts), key="managed_room_id")
            receipt = receipts[selected]
        if receipt:
            room_id, owner = receipt
            st.code(f"http://127.0.0.1:8503/?room={room_id}", language=None)
            st.download_button("保存房主管理凭证（勿分享）", owner, file_name="房主管理凭证.txt",
                               key="owner_secret_" + room_id, on_click="ignore")
            mode = st.selectbox("邀请权限", ["public", "seat", "omniscient"],
                                format_func=lambda m: {"public": "公开观众", "seat": "指定席位", "omniscient": "全知观众"}[m])
            seat = st.selectbox("邀请席位", list(range(1, 13)), disabled=mode != "seat")
            hours = st.number_input("邀请有效期（小时）", min_value=1, max_value=168, value=24)
            try:
                current = registry.owner_read(room_id, owner)
                if current["issue"] and current["revision"] is not None and st.button("重试展示记录写入"):
                    registry.control(room_id, owner, uuid.uuid4().hex, current["revision"], "retry_display_recording")
                    st.rerun()
                if st.button("生成邀请码"):
                    grant, secret = registry.issue(room_id, owner, mode, seat if mode == "seat" else None, hours)
                    st.code(secret, language=None)
                    st.caption("请单独保存邀请码；下次刷新不再显示。")
                for grant in registry.invitations(room_id, owner):
                    if not grant["revoked"] and st.button(f"撤销 {grant['audience']} 邀请 {grant['id']}", key="revoke_" + grant["id"]):
                        registry.revoke(room_id, owner, grant["id"])
                        st.rerun()
                if st.checkbox("打开独立回放画面", key="owner_replay_open"):
                    playing = render_player(st, lambda cursor: registry.owner_read(room_id, owner, cursor=cursor), "owner_" + room_id)
                if st.button("释放已结束的观战房间"):
                    registry.release(room_id, owner)
                    receipts.pop(room_id, None)
                    if ss.get("room_receipt") == receipt:
                        ss.pop("room_receipt", None)
                    ss.pop("managed_room_id", None)
                    st.rerun()
            except (ValueError, PermissionError) as error:
                st.error(str(error))
        else:
            st.caption("创建新局前开启“共享观战与回放记录”即可生成房间。旧运行对象不自动接入。")
        dirs = sorted(p for p in Path(replay_root).glob("*") if p.is_dir() and (p / "replay_frames.jsonl").exists())
        if dirs:
            choice = st.selectbox("本会话已记录档案", dirs, format_func=lambda p: p.name)
            if st.button("打开归档回放房间"):
                try:
                    replay = DisplayReplay.load(choice)
                    ss.room_receipt = registry.register(replay=replay)
                    st.rerun()
                except (OSError, ValueError):
                    st.error("无法读取该展示档案，请检查记录是否完整。")
        upload = st.file_uploader("只读浏览旧归档（final_state.json 或 events.jsonl）", type=["json", "jsonl"])
        if upload is not None:
            try:
                data = legacy_public_rows(upload.getvalue())
                st.caption(data["issue"])
                if data["rows"]:
                    pos = st.slider("旧档事件位置", 1, len(data["rows"]), 1) if len(data["rows"]) > 1 else 1
                    for row in data["rows"][max(0, pos - 90):pos]:
                        st.text(f"{row['phase']} · {row['seat'] or '法官'}：{row['text']}")
            except (ValueError, UnicodeError):
                st.error("文件不是可读取的旧归档。")
    return playing
