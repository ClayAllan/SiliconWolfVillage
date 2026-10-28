# -*- coding: utf-8 -*-
"""硅基狼村观战工作台。运行：python -m streamlit run app.py
后台服务推进对局；圆桌、动态与玩家观察室只读取快照。"""

import json
import os
import time
import uuid

import streamlit as st

from engine import config
from ui import ui_design as design
from ui.ui_preview import preview_engine
from service.view_projection import project_view, faction_counts, project_api_health
from agent.model_protocol import ERROR_LABELS
from storage.session_storage import storage_paths
from storage.save_package import store_uploaded_save, MAX_SAVE_PACKAGE_BYTES
from agent.ai_agent import AgentManager
from storage.game_archive import game_statistics
from engine.game_engine import GameEngine
from service.game_service import GameService
from ui.ui_runtime import AUTO_POLL_SECONDS
from service.live_feed import schedule_page_refresh
from ui.feed_ui import render_feed

# 手动进度存档与自动终局档案分开保存。
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# 页面基础设置与原生双主题
# ============================================================
st.set_page_config(page_title="硅基狼村 · AI 观战工作台", page_icon="🐺", layout="wide", initial_sidebar_state="collapsed")
if "_storage_id" not in st.session_state:
    st.session_state._storage_id = str(uuid.uuid4())
SAVE_DIR, REPLAY_DIR = storage_paths(st.session_state._storage_id)
st.markdown(design.stylesheet(st.context.theme.type), unsafe_allow_html=True)

# An invitation routes into a separate read-only page before any owner state is initialized.
if st.query_params.get_all("room"):
    from ui.spectator_ui import spectator_entry
    if len(st.query_params.get_all("room")) != 1:
        st.error("房间参数重复，请使用房主提供的完整链接。")
    else:
        spectator_entry(st, st.query_params["room"])
    st.stop()

def render_vote_chart(e: dict) -> str:
    """📊 横向 HTML/CSS 票型柱状图：支持加权票型渲染、票王皇冠与投票来源标签。"""
    totals = {int(k): float(v) for k, v in (e.get("totals") or {}).items()}
    votes = {int(k): int(v) for k, v in (e.get("votes") or {}).items()}
    winner = e.get("winner")
    winner = int(winner) if winner not in (None, "", 0) else None
    sheriff = e.get("sheriff")
    sheriff = int(sheriff) if sheriff not in (None, "", 0) else None
    title = design.text(e.get("title", "票型"))

    by_target, abstain, missing = {}, [], []
    sources = {int(k): v for k, v in (e.get("decision_sources") or {}).items()}
    for v, t in votes.items():
        if t and t in totals:
            by_target.setdefault(t, []).append(v)
        elif not t:
            if sources.get(v) in {"technical_fallback", "manual_skip", "invalid_target"}:
                missing.append(v)
            else:
                abstain.append(v)

    if not totals:
        inner = '<div style="opacity:.6;font-size:12px;">本轮无有效票，请查看提交状态。</div>'
    else:
        maxw = max(totals.values()) or 1.0
        inner = ""
        for target, w in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
            pct = max(8.0, w / maxw * 100.0)
            is_king = (winner is not None and target == winner)
            bar_color = "#c7a36d" if is_king else "#779fa5"
            crown = " 👑" if is_king else ""
            vlist = sorted(by_target.get(target, []))
            vtxt = "、".join(f"{x}号" + ("🎖️" if x == sheriff else "") for x in vlist) or "—"
            inner += (
                '<div style="margin:6px 0;">'
                '<div style="display:flex;justify-content:space-between;font-size:12px;color:var(--ink);">'
                f'<b>{target}号{crown}</b><span style="font-weight:700;color:var(--gold);">{w:g} 票</span></div>'
                '<div style="background:rgba(130,140,165,0.15);border-radius:6px;overflow:hidden;height:18px;margin-top:3px;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">'
                f'<div style="width:{pct:.1f}%;height:100%;background:{bar_color};border-radius:6px;box-shadow:0 0 8px rgba(10,132,255,0.4);"></div></div>'
                f'<div style="font-size:11px;opacity:.75;margin-top:2px;">投给此座位的玩家：{vtxt}</div>'
                '</div>'
            )
    if abstain:
        inner += f'<div style="font-size:11px;margin-top:4px;">弃票/来源未标注：{"、".join(f"{x}号" for x in sorted(abstain))}</div>'
    if missing:
        labels = {"technical_fallback": "技术失票", "manual_skip": "人工跳过", "invalid_target": "无效目标"}
        inner += '<div style="font-size:11px;margin-top:4px;">' + "、".join(
            f"{x}号（{labels[sources[x]]}）" for x in sorted(missing)) + '；不代表主动弃票</div>'
    return (
        '<div style="border:1px solid rgba(10,132,255,0.35);border-radius:10px;padding:10px 12px;'
        'background:rgba(10,132,255,0.06);margin:10px 0;box-shadow:0 2px 10px rgba(0,0,0,0.15);">'
        f'<div style="color:var(--gold);font-weight:800;font-size:13px;margin-bottom:6px;">📊 {title}</div>'
        f'{inner}</div>'
    )


def render_log_entry(row):
    return render_vote_chart(row) if row.get("kind") == "votechart" else design.event(row)


def current_token_usage(engine) -> dict:
    """读取内存快照，不扫描持续增长的 api_calls.jsonl。"""
    manager = getattr(engine, "manager", None) if engine else None
    if manager and hasattr(manager, "get_usage_snapshot"):
        return manager.get_usage_snapshot()
    return {
        "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        "cache_hit_tokens": 0, "cache_miss_tokens": 0,
        "reasoning_tokens": 0, "summary_requests": 0,
        "summary_total_tokens": 0, "api_returns": 0,
        "unmetered_returns": 0, "by_response_kind": {},
    }


def compact_token_count(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


# ============================================================
# 会话状态
# ============================================================
def init_state():
    ss = st.session_state
    ss.setdefault("game_service", None)
    ss.setdefault("preview_mode", False)
    ss.setdefault("viewer_mode", "全知视角")
    ss.setdefault("viewer_seat", 1)
    ss.setdefault("engine", None)
    ss.setdefault("running", False)
    ss.setdefault("done", False)
    ss.setdefault("status", "等待开始")
    ss.setdefault("error", "")
    ss.setdefault("sel_seat", 1)
    ss.setdefault("advancing_since", 0.0)
    ss.setdefault("last_event_type", "")
    ss.setdefault("last_archive", None)
    ss.setdefault("archive_error", "")
    ss.setdefault("agent_timeout", config.AGENT_TIMEOUT)
    # 热更新时移交旧生成器，保留当前步骤；之后界面不再持有可推进的生成器。
    if ss.game_service is None and ss.engine is not None:
        lock = ss.get("advance_lock")
        if lock is not None:
            lock.acquire()
        try:
            ss.game_service = GameService(ss.engine, REPLAY_DIR, generator=ss.get("gen"))
            if ss.running and not ss.done:
                ss.game_service.play()
        finally:
            if lock is not None:
                lock.release()
    for obsolete in ("gen", "advance_lock", "advancing"):
        ss.pop(obsolete, None)


def _human_action_integrity_error(engine):
    """Detect a model-authored action recorded for the configured human seat."""
    human_seat = getattr(engine, "human_seat", None)
    if human_seat is None:
        return ""
    for row in getattr(engine, "reasoning_log", {}).get(human_seat, []):
        response = row.get("response") or {}
        if (row.get("actor_source") == "human" or response.get("_human_action")
                or response.get("decision_source") == "human"):
            continue
        label = row.get("label", "未知行动")
        return (f"安全保护：真人席位 {int(human_seat):02d} 已出现非真人提交的行动“{label}”。"
                "对局已暂停，避免继续由 AI 代打；请重置并重新开始一局。")
    return ""


def sync_game_state():
    """只在 Streamlit 线程写会话状态，后台只操作 GameService。"""
    ss = st.session_state
    if ss.game_service is not None:
        # 已打开对局可能仍持有热更新前的服务实例；使用当前快照协议读取，不重启生成器。
        snapshot = GameService.snapshot(ss.game_service)
        ss.engine = snapshot.engine
        for field in ("running", "done", "status", "error", "last_event_type",
                      "last_archive", "archive_error"):
            ss[field] = getattr(snapshot, field)
        ss.advancing_since = snapshot.waiting_since
        ss.runtime_diagnostics = getattr(snapshot, "runtime_diagnostics", {})
        integrity_error = _human_action_integrity_error(ss.engine)
        if integrity_error:
            # 即使后台服务来自热更新前的旧代码，也在页面层立即暂停并报告真人席位被代打。
            if snapshot.running:
                ss.game_service.pause()
            ss.running = False
            ss.error = integrity_error
            ss.status = "真人行动完整性检查失败，已暂停"
    return ss.engine


def install_game(engine, *, automatic, delay, turbo=False, generator=None):
    service = GameService(engine, REPLAY_DIR, automatic=automatic, delay=delay,
                          turbo=turbo, generator=generator,
                          record_display=bool(st.session_state.get("enable_sharing", False)))
    receipt = None
    if service.display_replay is not None:
        from service.room_registry import get_registry
        try:
            receipt = get_registry().register(service=service)
        except Exception:
            service.close("room_registration_failed")
            raise
    old_service = st.session_state.game_service
    if old_service is not None:
        old_service.close("replaced_by_new_game")
    st.session_state.game_service = service
    st.session_state.room_receipt = receipt
    sync_game_state()
    return service


def _new_game_engine(manager):
    """Create an engine only if the currently imported class has the human-action guard.

    Do not reload this module here: other modules and tests may hold references to the
    existing GameEngine class. A stale runtime must fail closed and be restarted, not
    create a second class identity and bypass existing hooks or instrumentation.
    """
    engine_class = GameEngine
    guard_constants = "\\n".join(
        item for item in engine_class._record_reasoning.__code__.co_consts
        if isinstance(item, str)
    )
    if "安全保护" not in guard_constants or "真人席位" not in guard_constants:
        raise RuntimeError(
            "当前运行的游戏引擎未加载真人行动安全保护，已拒绝开局。"
            "请重启线上应用实例后重试。"
        )
    config.get_logger().info(
        f"GameEngine runtime verified from {engine_class.__module__}; "
        "human-seat action guard verified."
    )
    return engine_class(manager)

def start_game(api_key, base_url, good_model, wolf_model, temperature, agent_timeout,
               board=None, *, automatic=True, delay=0.6, turbo=False,
               provider_mode="auto", thinking=True, reasoning_effort="low", human_seat=None):
    if human_seat is not None:
        if type(human_seat) is not int or not 1 <= human_seat <= 12:
            raise ValueError("真人座位必须在 1—12 号之间")
        board = config.DEFAULT_BOARD
    board_roles = config.GAME_BOARDS[board or config.DEFAULT_BOARD]
    config.get_logger().info(f"==== 新游戏开始 板子={board} 好营={good_model} 狼营={wolf_model} "
                             f"单步超时={agent_timeout}s temp={temperature} ====")
    manager = AgentManager(api_key=api_key, base_url=base_url,
                           good_model=good_model, wolf_model=wolf_model,
                           temperature=temperature, agent_timeout=int(agent_timeout),
                           provider_mode=provider_mode, thinking=thinking, reasoning_effort=reasoning_effort)
    engine = _new_game_engine(manager)
    engine.human_seat = human_seat
    engine.board_name = board or config.DEFAULT_BOARD
    try:
        engine.enable_recording(REPLAY_DIR)
        engine.setup(board_roles)
        # Setup/发牌完成后再次固定真人座位，避免初始化逻辑变更时覆盖开局配置。
        engine.human_seat = human_seat
        if human_seat is not None:
            config.get_logger().info(f"真人入座模式已启用：{human_seat:02d} 号真人，其余座位由 AI 驱动。")
        service = install_game(engine, automatic=automatic, delay=delay, turbo=turbo)
    except Exception:
        manager.shutdown()
        raise
    st.session_state.agent_timeout = int(agent_timeout)
    st.session_state.preview_mode = False
    service.play()
    sync_game_state()


# 进度存档仍沿用 v6 格式；读档重建引擎并从下一夜继续，不恢复 Python 调用栈。
def list_saves() -> list:
    try:
        os.makedirs(SAVE_DIR, exist_ok=True)
        return sorted((f for f in os.listdir(SAVE_DIR) if f.endswith(".json")), reverse=True)
    except Exception:
        return []


def load_game(filename, api_key, base_url, good_model, wolf_model, temperature, agent_timeout,
              *, provider_mode="auto", thinking=True, reasoning_effort="low"):
    with open(os.path.join(SAVE_DIR, filename), "r", encoding="utf-8") as f:
        payload = json.load(f)
    manager = AgentManager(api_key=api_key, base_url=base_url,
                           good_model=good_model, wolf_model=wolf_model,
                           temperature=temperature, agent_timeout=int(agent_timeout),
                           provider_mode=provider_mode, thinking=thinking, reasoning_effort=reasoning_effort)
    try:
        manager.load_state(payload.get("manager", {}))
        engine = _new_game_engine(manager)
        engine.load_state(payload.get("engine", {}))
        engine.enable_recording(REPLAY_DIR)
        return engine
    except Exception:
        manager.shutdown()
        raise


def advance_step() -> bool:
    service = st.session_state.game_service
    if service is None:
        return False
    try:
        return service.step()
    finally:
        sync_game_state()


def read_log_tail(path: str, max_lines: int = 40, max_bytes: int = 131072) -> str:
    """只读取日志尾部，避免长局中每次页面渲染都加载完整日志。"""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            raw = f.read()
        text = raw.decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-max_lines:]) or "（暂无日志）"
    except OSError:
        return "（暂无日志）"

def test_connection(api_key, base_url, model, **settings):
    manager = AgentManager(api_key=api_key, base_url=base_url, good_model=model, **settings)
    try:
        return manager.diagnose_connection()
    finally:
        manager.shutdown()


# ============================================================
# 主流程
# ============================================================
init_state()
engine = sync_game_state()
ss = st.session_state
live_for_mode = ss.engine
if live_for_mode is not None and getattr(live_for_mode, "human_seat", None) is not None:
    ss.viewer_seat = live_for_mode.human_seat
    ss.viewer_mode = "单一视角"
single_view = ss.viewer_mode == "单一视角"

# 热更新兼容：若修改代码前已有一局刚结束但尚未落盘，在当前会话下一次 rerun 时补归档。
if engine is not None and ss.done and not ss.last_archive and not ss.archive_error:
    ss.game_service.archive("terminal_session_backfill")
    engine = sync_game_state()

# ---------------- 侧边栏：配置 + 操作 ----------------
with st.sidebar:
    st.markdown("### 对局设置")
    st.caption("选择板子与模型，然后在工作台开始对局。")
    # 🎲 游戏板子选择(V6.4)：开新局/读档前切换，决定本局发什么身份(如含白狼王)。
    _boards = list(config.GAME_BOARDS.keys())
    human_mode = st.toggle("🎮 真人入座模式（1 真人 + 11 AI）", value=bool(getattr(ss.engine, "human_seat", None)),
                              help="首版仅支持 12 人标准预女猎守板子；真人身份随机分配。")
    current_human_seat = getattr(ss.engine, "human_seat", None) if ss.engine is not None else None
    if ss.game_service is not None and not ss.done and human_mode != (current_human_seat is not None):
        st.warning("当前对局的模式不会因切换此开关而改变。请点击“开始新游戏”以按当前选择重新开局；新局会结束并替换当前对局。")
    if ss.game_service is not None and not ss.done:
        if current_human_seat is None:
            st.caption("当前实际对局：纯 AI 模式（没有真人座位）。")
        else:
            st.caption(f"当前实际对局：真人入座模式，{current_human_seat:02d} 号为真人。")
    human_seat = st.selectbox("真人座位", list(range(1, 13)), index=0,
                              format_func=lambda seat: f"{seat:02d} 号", disabled=not human_mode)
    board = st.selectbox("🎲 游戏板子", _boards,
                         index=_boards.index(config.DEFAULT_BOARD) if config.DEFAULT_BOARD in _boards else 0,
                         disabled=human_mode,
                         help="真人模式固定使用 12 人标准预女猎守板子。")
    if human_mode:
        board = config.DEFAULT_BOARD
        st.caption("真人模式下会隐藏全知视角与上帝干预入口。")
    default_key = ""  # Public visitors always supply their own key.
    api_key = st.text_input("DeepSeek API Key", value=default_key, type="password",
                            help="填写你自己的 API Key；请求由托管服务器转发给所选模型服务。")
    with st.expander("模型与高级设置", expanded=False):
        base_url = st.text_input(
            "Base URL", value=config.BASE_URL,
            help=("公共托管版仅允许 HTTPS 且域名在部署者允许列表中的模型服务。"
                  "自定义公网域名需由部署者配置 SWV_ALLOWED_MODEL_HOSTS；"
                  "本地/私有模型地址仅限可信自托管环境显式开启 SWV_ALLOW_PRIVATE_MODEL_URLS=1。"),
        )
        _providers = {"自动识别（仅官方地址启用专用参数）": "auto",
                      "通用 OpenAI 兼容接口": "generic", "DeepSeek 兼容接口（明确指定）": "deepseek"}
        provider_mode = _providers[st.selectbox("接口类型", list(_providers), key="provider_mode")]
        thinking = st.selectbox("DeepSeek 主行动思考", ["保留思考", "关闭隐式思考（省费）"], key="thinking_mode") == "保留思考"
        reasoning_effort = st.selectbox("DeepSeek 思考强度", ["low", "high", "max"], key="reasoning_effort",
                                        help="默认 low；思考和最终正文共用有限的输出预算。")
        st.caption("默认发言预算 8192、投票预算 4096；预算不足时修复上限分别为 12288、6144。确认、定序与摘要关闭隐式思考。")
        # 🎭 阵营多模型对战(V6.0)：好人/狼人各选一个模型，测试不同 LLM 的撒谎/识谎能力。
        _mopts = list(dict.fromkeys(list(config.MODEL_OPTIONS) + [config.GOOD_CAMP_MODEL, config.WOLF_CAMP_MODEL]))
        good_model = st.selectbox("🛡️ 好人阵营模型", _mopts, index=_mopts.index(config.GOOD_CAMP_MODEL),
                                  help="神职(预/女/猎/守)与平民发往该模型")
        wolf_model = st.selectbox("🐺 狼人阵营模型", _mopts, index=_mopts.index(config.WOLF_CAMP_MODEL),
                                  help="4 名狼人发往该模型，可与好人不同以对比撒谎能力")
        temperature = st.slider("Temperature", 0.0, 1.5, float(config.TEMPERATURE), 0.1)
        agent_timeout = st.slider("单步思考上限（秒）", 15, 240, int(config.AGENT_TIMEOUT), 5,
                                  help="一次行动含重试和退避的总时限。新局或暂停后点击应用设置生效。")
        speed_mode = st.select_slider(
            "⚡ 推演倍速",
            options=["0.5x 慢速", "1.0x 标准", "2.0x 快速", "Turbo 极速"],
            value="1.0x 标准",
            help="控制每个行动步骤后的观看等待时间。Turbo 极速模式无额外停顿。",
        )
        speed_delay_map = {"0.5x 慢速": 1.5, "1.0x 标准": 0.6, "2.0x 快速": 0.25, "Turbo 极速": 0.0}
        delay = speed_delay_map.get(speed_mode, 0.6)
        is_turbo = (speed_mode == "Turbo 极速")
        react_budget = st.slider("⚡ 反应式抢爆预算/天", 0, 12, int(getattr(config, "EXPLODE_REACT_BUDGET", 3)), 1,
                                 help="白天出现【查杀/归票】时，每个窗口只轮询一只狼，骑士按规则另行判断；这是每天额外 API 调用的硬上限。"
                                      "0=关闭(回到纯自己回合自爆)；调大更灵敏但更多调用/延迟。改了【下一个白天发言阶段】即生效。")
    auto = st.checkbox("🔁 自动推演", value=True)
    show_god = False if single_view else st.checkbox("👁️ 公屏显示上帝视角(夜间)情报", value=True)
    show_full_history = st.checkbox(
        "📚 渲染完整频道历史",
        value=False,
        help="默认只渲染最新记录以减小 WebSocket 消息；完整原始记录仍会写入本局归档。暂停后可开启回看。",
    )

    if human_mode:
        # 真人对局默认不生成共享回放，避免把本地私密行动上下文带入展示记录。
        ss.enable_sharing = False
    st.checkbox("共享观战与回放记录（仅新建对局生效）", key="enable_sharing", value=False,
                disabled=ss.game_service is not None or human_mode,
                help="真人入座模式关闭共享回放；纯 AI 对局可按需启用。")

    if ss.game_service is not None:
        ss.game_service.configure(automatic=auto, delay=delay, turbo=is_turbo, react_budget=react_budget)
        engine = sync_game_state()
    auto_running = bool(auto and ss.running and not ss.done)
    model_settings = dict(api_key=api_key, base_url=base_url, good_model=good_model,
                          wolf_model=wolf_model, temperature=temperature, agent_timeout=agent_timeout,
                          provider_mode=provider_mode, thinking=thinking, reasoning_effort=reasoning_effort)
    pending_model = bool(engine and engine.manager.has_pending())
    if engine:
        if st.button("应用模型设置（保持暂停）", use_container_width=True,
                     disabled=ss.running or pending_model or ss.done):
            if not api_key:
                st.error("请先填写本次对局的 API Key。")
            else:
                try:
                    GameService.apply_model_settings(ss.game_service, **model_settings)
                    engine = sync_game_state()
                    st.success("设置已应用；对局、记忆和用量保留，尚未发起请求。点击继续推进下一步。")
                except Exception as error:
                    st.error(f"应用设置失败：{error}")
        st.caption("修改配置后先应用。连接测试使用已应用配置；继续不会重新生成上一次已记录的兜底响应。")
    if st.button("🔌 测试连接", use_container_width=True,
                 disabled=ss.running or pending_model or (not engine and not api_key),
                 help="仅点击时调用一次模型：最多 256 个输出 Token。验证完整 JSON，不能证明长局稳定。"):
        try:
            result = (GameService.diagnose_connection(ss.game_service) if engine else
                      test_connection(api_key, base_url, good_model, provider_mode=provider_mode))
            ss.connection_diagnostic = result
            engine = sync_game_state()
        except Exception as error:
            ss.connection_diagnostic = (False, f"诊断无法执行：{type(error).__name__}", {})
    if ss.get("connection_diagnostic"):
        ok, msg, diagnostic = ss.connection_diagnostic
        (st.success if ok else st.error)(msg)
        if diagnostic.get("usage"):
            st.caption(f"该次诊断用量：{diagnostic['usage']['total_tokens']:,} Token；使用当时配置。")
    # ===== 🧮 本局实时 Token =====
    _live_usage = current_token_usage(engine)
    with st.expander("🧮 本局 Token 消耗", expanded=bool(engine)):
        st.metric("已结算 Token", f"{int(_live_usage['total_tokens']):,}")
        tc1, tc2 = st.columns(2)
        tc1.metric("输入", f"{int(_live_usage['prompt_tokens']):,}")
        tc2.metric("输出", f"{int(_live_usage['completion_tokens']):,}")
        st.caption(
            f"缓存命中 {_live_usage['cache_hit_tokens']:,}　·　"
            f"缓存未命中 {_live_usage['cache_miss_tokens']:,}　·　"
            f"隐式推理 {_live_usage['reasoning_tokens']:,}"
        )
        _kinds = _live_usage.get("by_response_kind") or {}
        _kind_names = {"speech": "发言/行动", "vote": "投票", "memory_summary": "记忆摘要"}
        if _kinds:
            st.caption("　·　".join(
                f"{_kind_names.get(kind, kind)} {int(values.get('total_tokens', 0)):,}"
                for kind, values in sorted(_kinds.items())
            ))
        st.caption(
            f"已收到 {_live_usage['api_returns']} 次 API 用量回执；"
            "正在思考的请求会在返回后计入，读档后继续累计。"
        )
        if _live_usage.get("unmetered_returns"):
            st.warning(f"有 {_live_usage['unmetered_returns']} 次响应未返回 usage，实际消耗可能略高。")
        if _live_usage.get("unknown_requests"):
            st.warning(f"有 {_live_usage['unknown_requests']} 次超时请求用量待确认；不能视为零消耗。")

    if engine and hasattr(engine.manager, "get_health_snapshot"):
        health = project_api_health(engine.manager.get_health_snapshot(), ss.viewer_seat if single_view else None)
        with st.expander("API 诊断与恢复", expanded=bool(ss.error)):
            st.caption(f"接口策略：{health.get('provider', '待应用新设置')} · 连续失败行动：{health.get('consecutive_failures', 0)}")
            last = health.get("last_diagnostic")
            if last:
                st.write(ERROR_LABELS.get(last.get("category"), "最近请求诊断"))
                st.json(last)
            else:
                st.caption("尚无失败诊断。旧对局可先暂停并应用模型设置，以启用新请求策略。")
            if health.get("circuit_open") or ss.error.startswith("API已暂停"):
                st.warning("最近失败响应已记为系统兜底。继续会按现有流程向后推进，不重做该响应；只有有效的完整行动成功才清零失败计数。")
            if pending_model:
                st.caption("尚有后台请求或待收集结果。完成后用单步收集，或跳过当前玩家，再应用设置。")
            if not ss.running:
                st.download_button("下载 API 诊断（不含密钥与提示词）",
                    data=json.dumps({"health": health, "usage": _live_usage}, ensure_ascii=False, indent=2),
                    file_name=f"api-diagnostic-{engine.game_id}.json", mime="application/json")

    if engine and not single_view:
        with st.expander("📊 本局可核验统计"):
            for metric in game_statistics(engine)["observed_metrics"].values():
                value = metric["value"]
                st.metric(metric["label"], "N/A" if value is None else f"{value:.1%}")
                st.caption(f"{metric['numerator']} / {metric['denominator']} · {metric['definition']}")
            st.caption("仅统计放逐投票，每票按人数计数，不按警长权重加权；无样本显示 N/A。比例不能直接代表识谎、欺骗或综合能力。")

    # ===== 📜 复盘与导出 =====
    with st.expander("📜 复盘与导出"):
        if single_view:
            st.caption("全知复盘包含所有身份和秘密行动。切回全知视角后可导出。")
        else:
            if engine:
                if auto_running:
                    st.info("自动推演期间暂停生成临时下载文件；暂停或终局后即可导出。")
                else:
                    st.download_button(
                        label="📥 导出本局复盘 (MD格式)",
                        data=engine.export_replay_text(),
                        file_name="赛博狼人杀复盘.md",
                        mime="text/markdown",
                        use_container_width=True,
                        key=f"replay_download_{getattr(engine, 'game_id', 'current')}",
                        on_click="ignore",
                    )
                st.caption("只含真身配置 + 每夜/每天关键动作，剥离冗长发言。")
                _archive = ss.get("last_archive")
                if _archive:
                    st.success(f"已自动归档：{os.path.basename(_archive['directory'])}")
                    st.caption(f"目录：{REPLAY_DIR}")
                if ss.get("archive_error"):
                    st.error(ss.archive_error)
                try:
                    _recent = sorted(
                        (
                            f for f in os.listdir(REPLAY_DIR)
                            if os.path.isdir(os.path.join(REPLAY_DIR, f))
                            and os.path.exists(os.path.join(REPLAY_DIR, f, "manifest.json"))
                        ),
                        reverse=True,
                    )[:5]
                except OSError:
                    _recent = []
                if _recent:
                    st.caption("最近自动档案：" + "、".join(_recent))
                if os.path.exists(os.path.join(REPLAY_DIR, "index.csv")):
                    with open(os.path.join(REPLAY_DIR, "index.csv"), "rb") as _idx:
                        st.download_button(
                            "📊 导出跨局索引 (CSV)", data=_idx.read(),
                            file_name="赛博狼人杀对局索引.csv", mime="text/csv",
                            use_container_width=True, on_click="ignore",
                        )
            else:
                st.caption("开始游戏后可导出本局复盘。")

    # ===== 💾 存档 / 读档（V6.0）=====
    with st.expander("💾 存档 / 读档"):
        st.caption("先暂停再保存；若仍在等待玩家，单步收集已完成的响应或跳过当前玩家后再保存。")
        if engine:
            _busy = bool(auto_running or engine.has_pending_action() or engine.manager.has_pending())
            if st.button("💾 保存当前进度", use_container_width=True, disabled=_busy):
                try:
                    nm = ss.game_service.save(SAVE_DIR)
                    ss.last_saved_filename = nm
                    st.success(f"已存档：{nm}")
                    st.toast(f"💾 已保存 {nm}")
                except Exception as e:
                    st.error(f"存档失败：{type(e).__name__}: {e}")
        else:
            st.caption("开始或读取游戏后可存档。")

        _saves = list_saves()
        st.caption("进度存档可下载到本地，并在另一个浏览器会话中导入。存档包含真实身份、AI 私密记忆与局势信息，仅应作为私密备份保管；不包含 API Key。")
        uploaded_save = st.file_uploader(
            "导入私密进度存档（JSON）", type=["json"], key="portable_save_upload",
            disabled=auto_running,
            help="仅接受本应用 v6 进度存档；最大 16 MiB。导入后仍需填写自己的 API Key 才能继续。",
        )
        if st.button("📥 导入存档到本会话", use_container_width=True,
                     disabled=auto_running or uploaded_save is None):
            try:
                imported_name = store_uploaded_save(uploaded_save.getvalue(), SAVE_DIR)
                st.success(f"已安全导入：{imported_name}。选择该存档后点击“读取存档”即可恢复。")
                st.rerun()
            except (ValueError, OSError) as _import_error:
                st.error(f"导入失败：{_import_error}")
        if _saves:
            pick = st.selectbox("选择存档读取", _saves, key="load_pick")
            try:
                with open(os.path.join(SAVE_DIR, pick), "rb") as _save_file:
                    _save_bytes = _save_file.read(MAX_SAVE_PACKAGE_BYTES + 1)
                if len(_save_bytes) <= MAX_SAVE_PACKAGE_BYTES:
                    st.download_button(
                        "📤 下载所选私密进度存档", data=_save_bytes, file_name=pick,
                        mime="application/json", use_container_width=True,
                        key=f"download_save_{pick}",
                    )
                else:
                    st.error("该存档超过可导出大小上限，未提供下载。")
            except OSError as _save_error:
                st.error(f"读取存档失败：{type(_save_error).__name__}")
            st.caption("⚠️ 读档会销毁当前对局，并从存档当天的【下一夜】继续推演"
                       "（生成器无法原位恢复）；建议在白天阶段存档以减少跳过。")
            if st.button("📂 读取存档", use_container_width=True, disabled=auto_running):
                if not api_key or api_key.startswith("sk-请"):
                    st.error("请先填入有效的 API Key！")
                else:
                    try:
                        eng = load_game(pick, api_key, base_url, good_model, wolf_model,
                                        temperature, agent_timeout, provider_mode=provider_mode,
                                        thinking=thinking, reasoning_effort=reasoning_effort)
                        install_game(eng, automatic=auto, delay=delay, turbo=is_turbo)
                        ss.agent_timeout = int(agent_timeout)
                        config.get_logger().info(
                            f"==== 读档恢复 {pick} day={eng.day} phase={eng.phase} winner={eng.winner} ====")
                        st.rerun()                    # ★重启前端循环
                    except Exception as e:
                        st.error(f"读档失败：{type(e).__name__}: {e}")
        else:
            st.caption("（暂无存档）")

    # ===== ⚡ 上帝干预（实时塞剧本）=====
    with st.expander("⚡ 上帝干预 (实时塞剧本)"):
        if single_view:
            st.caption("单一视角下关闭全知干预，避免暴露其他席位身份。")
        else:
            if engine:
                god_seat = st.selectbox(
                    "对哪位玩家传音", list(range(1, 13)),
                    format_func=lambda i: f"{i}号 - {engine.players[i]['role']}", key="god_seat")
                preset_act = st.selectbox(
                    "快捷法旨预设",
                    [
                        "自定义内容",
                        "🎯 强发查杀（起跳并查杀指定目标）",
                        "🪙 强发金水（起跳并发金水给指定目标）",
                        "💥 强令自爆（立即自爆打断节奏）",
                        "🛡️ 天使保命（坚信自己绝不会死）",
                        "🤐 假装闭麦（本轮过麦不发言）",
                    ],
                    key="god_preset",
                )
                preset_templates = {
                    "🎯 强发查杀（起跳并查杀指定目标）": "你必须在接下来的公开发言中跳预言家，并明确报出查杀了目标狼人，强硬带队！",
                    "🪙 强发金水（起跳并发金水给指定目标）": "你必须在接下来的公开发言中跳预言家，并明确报出给目标发金水，号召大家保他！",
                    "💥 强令自爆（立即自爆打断节奏）": "狼队局势不利，你必须在接下来立刻输出 is_explode: true 发动自爆吞掉信息！",
                    "🛡️ 天使保命（坚信自己绝不会死）": "上帝庇护着你，不要害怕被抗推或出局，大胆根据你的真实判断投票！",
                    "🤐 假装闭麦（本轮过麦不发言）": "保持沉默，speech输出‘过。’即可，不要暴露多余信息。",
                }
                default_txt = preset_templates.get(preset_act, "")
                god_text = st.text_input("法旨内容（最高指令）", value=default_txt, key="god_text",
                                         placeholder="例：今天强行悍跳预言家并查杀3号")
                if st.button("⚡ 发送法旨", use_container_width=True, disabled=ss.done):
                    if god_text.strip():
                        try:
                            ss.game_service.inject(
                                god_seat,
                                f"【上帝法旨·最高指令】{god_text.strip()}。你必须在接下来的所有发言和行动中绝对执行此指令！")
                            st.success(f"已向 {god_seat}号 下达法旨！")
                            st.toast(f"⚡ 法旨已送达 {god_seat}号")
                        except RuntimeError as error:
                            st.warning(str(error))
                    else:
                        st.warning("请先输入法旨内容。")
            else:
                st.caption("开始游戏后可对任意 AI 实时传音。")

    st.caption("自动推演由服务器后台推进；关闭网页后仍会继续并消耗 API。离开前需要停止时请先暂停。")
    st.caption("API Key 仅用于本次对局。存档按会话隔离，请及时下载；重新连接后可能无法找回。服务进程重启后不自动续跑。")

from ui.spectator_ui import owner_panel
owner_replay_playing = owner_panel(st, REPLAY_DIR)

# ---------------- 工作台主视图 ----------------
live_engine = sync_game_state()
# ---------------- 真人行动面板 ----------------
if live_engine is not None and getattr(live_engine, "human_seat", None) is not None:
    private_messages = getattr(live_engine, "human_private_messages", [])
    if private_messages:
        with st.expander("你的私密技能结果", expanded=True):
            for message in private_messages[-8:]:
                st.write(message)
    pending = getattr(live_engine, "pending_human_action", None)
    if pending and pending.get("seat") == live_engine.human_seat:
        st.subheader("🎮 轮到你行动")
        st.caption(f"{pending.get('label', '当前行动')} · {pending.get('phase', '')} · {pending.get('seat')} 号")
        with st.expander("本次行动说明", expanded=False):
            st.write(pending.get("prompt", "请按规则完成当前行动。"))
        kind = pending.get("kind", "speech")
        targets = pending.get("allowed_targets") or []
        payload = {}
        with st.form(f"human_action_{pending['action_id']}"):
            if kind in {"speech", "consolidation"}:
                payload["speech"] = st.text_area("你的发言", key=f"human_speech_{pending['action_id']}",
                                                  placeholder="请根据已公开的信息进行判断。")
                if kind == "consolidation":
                    choices = list(targets)
                    if not choices:
                        choices = [0]
                    payload["vote_target"] = st.selectbox(
                        "归票目标", choices, format_func=lambda seat: "不归票 / 弃票" if seat == 0 else f"{seat} 号",
                        key=f"human_target_{pending['action_id']}")
                if "is_explode" in (pending.get("allowed_flags") or []):
                    payload["is_explode"] = st.checkbox("发言后立即自爆", key=f"human_explode_{pending['action_id']}")
            elif kind in {"vote", "check", "guard", "poison", "hunt"}:
                choices = list(targets) or [0]
                labels = {"vote": "投票目标", "check": "查验目标", "guard": "守护目标",
                          "poison": "毒药目标", "hunt": "开枪目标"}
                payload["vote_target"] = st.selectbox(
                    labels.get(kind, "行动目标"), choices,
                    format_func=lambda seat: "弃票 / 不执行" if seat == 0 else f"{seat} 号",
                    key=f"human_target_{pending['action_id']}")
            elif kind == "antidote":
                payload["use_antidote"] = st.checkbox("使用解药", key=f"human_antidote_{pending['action_id']}")
            elif kind in {"join_police", "withdraw"}:
                payload[kind] = st.checkbox("确认上警" if kind == "join_police" else "确认退水",
                                            key=f"human_flag_{pending['action_id']}")
            elif kind == "order":
                payload["speech"] = st.selectbox("发言顺序", ["警左", "警右", "死左", "死右"],
                                                  key=f"human_order_{pending['action_id']}")
            elif kind == "badge_target":
                choices = list(targets) or [0]
                payload["badge_target"] = st.selectbox(
                    "警徽处理", choices, format_func=lambda seat: "撕毁警徽" if seat == 0 else f"移交给 {seat} 号",
                    key=f"human_badge_{pending['action_id']}")
            elif kind == "react_explode":
                payload["is_explode"] = st.checkbox("立即自爆", key=f"human_react_{pending['action_id']}")
            else:
                st.warning(f"暂不支持的真人行动类型：{kind}")
            submitted = st.form_submit_button("提交行动", type="primary", use_container_width=True)
        if submitted:
            try:
                ss.game_service.submit_human_action(pending["action_id"], payload)
                sync_game_state()
                st.rerun()
            except (ValueError, RuntimeError) as error:
                st.error(f"行动未提交：{error}")
    elif live_engine.players.get(live_engine.human_seat, {}).get("alive") is False and (
        live_engine.human_seat in (getattr(live_engine, "pending_wolf_night_seats", []) or [])
        or live_engine.human_seat in (getattr(live_engine, "active_last_stand_wolves", []) or [])
    ):
        st.info("你已自爆出局，但按本局规则仍可参与本夜最后一次狼人讨论与落刀；轮到你时会显示行动面板。")
    elif live_engine.players.get(live_engine.human_seat, {}).get("alive") is False:
        st.info("你已出局。可以继续观看本局后续进程；不会再向你请求行动。")
    elif not ss.done:
        if ss.running:
            st.info("真人模式已启用；当前是其他玩家的回合，轮到你时会显示行动输入面板。")
        else:
            st.info("真人模式已启用，但对局当前已暂停。点击“继续”推进到你的回合。")
    human_role = live_engine.roles.get(live_engine.human_seat) if live_engine.human_seat in live_engine.roles else None
    human_has_wolf_access = bool(
        human_role is not None
        and getattr(human_role, "camp", "") == "狼人"
        and (
            live_engine.players.get(live_engine.human_seat, {}).get("alive", False)
            or live_engine.human_seat in (getattr(live_engine, "pending_wolf_night_seats", []) or [])
            or live_engine.human_seat in (getattr(live_engine, "active_last_stand_wolves", []) or [])
        )
    )
    if human_has_wolf_access:
        live_wolf_talk = getattr(live_engine, "current_wolf_talk", [])
        if live_wolf_talk:
            st.subheader("🐺 狼队私密频道 · 本夜讨论")
            st.caption("仅真人狼人可见；这里会随队友完成讨论而更新。")
            for talk_seat, talk_text in live_wolf_talk:
                st.markdown(f"**{talk_seat} 号：**")
                st.write(talk_text)

with st.container(key="playback"):
    # 先预留位置，让头部和状态条位于操作按钮上方。
    heading_slot = st.container()
    overview_slot = st.empty()
    with st.container(key="view_controls"):
        mode_col, seat_col, theme_col = st.columns([1.3,1,1.2])
        if getattr(live_engine, "human_seat", None) is not None:
            mode_col.info(f"真人玩家：{live_engine.human_seat:02d} 号（受限玩家视角）")
            st.session_state.viewer_mode = "单一视角"
            st.session_state.viewer_seat = live_engine.human_seat
            seat_col.caption(f"当前玩家：{live_engine.human_seat:02d} 号")
        else:
            mode_col.radio("观战视角", ["全知视角", "单一视角"], horizontal=True, key="viewer_mode")
            seat_col.selectbox("代入席位", list(range(1,13)), key="viewer_seat",
                               format_func=lambda seat: f"{seat:02d} 号", disabled=not single_view)
        theme_col.markdown("**外观主题**")
        theme_col.caption("右上角 ⋮ → Theme；Light 浅色 / Dark 深色 / System 跟随系统")
        reveal_counts = st.checkbox("显示全知阵营人数", value=False, key="reveal_factions",
                                    help="显示真实剩余阵营人数，可能通过人数变化推断出局者阵营。") if single_view else False
    start_col, pause_col, step_col, skip_col, reset_col, demo_col = st.columns([1.3,1,1,1.4,1,1.4])
    if start_col.button("🎬 开始新游戏", use_container_width=True, type="primary"):
        if not api_key or api_key.startswith("sk-请"):
            st.error("请展开左上角侧栏，填写自己的 API Key 后开始；也可先查看零 API 示例。")
        else:
            try:
                start_game(api_key, base_url, good_model, wolf_model, temperature, agent_timeout, board,
                           automatic=auto, delay=delay, turbo=is_turbo,
                           provider_mode=provider_mode, thinking=thinking, reasoning_effort=reasoning_effort,
                           human_seat=human_seat if human_mode else None)
                st.rerun()
            except (ValueError, RuntimeError) as error:
                st.error(str(error))
    pause_label = "⏸️ 暂停" if ss.running else "▶️ 继续"
    if pause_col.button(pause_label, use_container_width=True, disabled=live_engine is None or ss.done):
        if ss.running:
            ss.game_service.pause()
        else:
            ss.game_service.play()
        st.rerun()
    if step_col.button("⏭️ 单步", use_container_width=True, disabled=live_engine is None or ss.done or auto_running):
        advance_step()
        st.rerun()
    can_skip = bool(live_engine and not ss.done and live_engine.has_pending_action())
    if skip_col.button("⏭️ 跳过当前玩家", use_container_width=True, disabled=not can_skip):
        ss.game_service.request_skip_current()
        st.rerun()
    if reset_col.button("🔄 重置", use_container_width=True, disabled=auto_running):
        if ss.game_service is not None:
            ss.game_service.close("manual_reset")
        for key in ("game_service","engine","running","done","status","error","advancing_since","last_event_type","runtime_diagnostics"):
            ss.pop(key, None)
        ss.preview_mode = False
        st.rerun()
    preview_label = "退出示例" if ss.preview_mode else "查看示例 · 零 API"
    if demo_col.button(preview_label, use_container_width=True, disabled=live_engine is not None):
        ss.preview_mode = not ss.preview_mode
        if ss.preview_mode:
            ss.sel_seat = 7
        st.rerun()

is_preview = bool(ss.preview_mode and live_engine is None)
source_engine = preview_engine() if is_preview else live_engine
engine = project_view(source_engine, ss.viewer_seat if single_view else None) if source_engine else None
if single_view:
    ss.sel_seat = ss.viewer_seat
players = engine.players if engine else {
    i: {"role":"未知","alive":True,"style":"等待入席","memory":""} for i in range(1,13)
}
current = engine.current_speaker if engine else None
sheriff = engine.sheriff if engine else None
with heading_slot:
    st.markdown(design.header(engine, demo=is_preview, running=ss.running and not is_preview), unsafe_allow_html=True)
    st.markdown('<div class="wv-intro"><div><h1>让推理，发生在圆桌之上。</h1><p>十二个 AI 的身份博弈，从每一句发言开始。</p></div></div>', unsafe_allow_html=True)
with overview_slot:
    st.markdown(design.overview(engine, current_token_usage(live_engine), demo=is_preview), unsafe_allow_html=True)

status_slot = st.empty()
lines = []
if is_preview:
    lines.append("示例预览 · 固定编写的第 2 天快照，用于体验界面，不代表真实对局结果，不调用模型。")
elif engine:
    state_label = "自动推进" if auto_running else "手动推进" if ss.running else "已暂停"
    visible_status = "夜间行动进行中" if single_view and "夜" in engine.phase and engine.current_speaker != ss.viewer_seat else ss.status
    lines.append(f"{state_label} · {visible_status} · {speed_mode}")
else:
    lines.append("首次使用：左上角展开对局设置 → 填写 API Key → 开始新游戏。也可先查看零 API 示例。")

if single_view:
    lines.append(f"当前代入 {ss.viewer_seat:02d} 号：展示自己的身份、技能与推理；狼人还可查看队友身份和作战板，其他私密信息隐藏。")

if live_engine and ss.running and ss.advancing_since:
    elapsed = max(0, int(time.time() - ss.advancing_since))
    lines.append(f"正在等待模型响应 · {elapsed} 秒；可暂停或跳过当前玩家。")

if ss.error:
    status_slot.error(ss.error)
elif lines:
    status_slot.caption("\n\n".join(lines))
else:
    status_slot.empty()

if live_engine and ss.game_service is not None:
    runtime_diag = ss.get("runtime_diagnostics") or {}
    wait_labels = {
        "closed": "服务已关闭",
        "finished": "对局已结束",
        "error_paused": "异常后已暂停",
        "human_input": "等待真人行动",
        "model_request": "等待模型请求",
        "automatic_progress": "自动推进中",
        "manual_progress": "手动推进模式",
        "paused_with_pending_request": "已暂停，但仍有待处理请求",
        "paused": "已暂停",
    }
    with st.expander(
        "🩺 对局运行诊断",
        expanded=bool(ss.error or (
            runtime_diag.get("worker_expected") and not runtime_diag.get("worker_alive")
        )),
    ):
        st.caption(
            f"运行状态：{wait_labels.get(runtime_diag.get('wait_kind'), '状态待确认')} · "
            f"最近事件：{runtime_diag.get('last_event_type') or '暂无'} · "
            f"状态修订号：{runtime_diag.get('revision', 0)}"
        )
        diag_cols = st.columns(4)
        worker_expected = bool(runtime_diag.get("worker_expected"))
        worker_alive = bool(runtime_diag.get("worker_alive"))
        worker_label = "存活" if worker_alive else "未存活" if worker_expected else "当前不需要"
        diag_cols[0].metric("后台线程", worker_label)
        progress_age = runtime_diag.get("last_progress_age_seconds")
        diag_cols[1].metric(
            "最近推进",
            "暂无" if progress_age is None else f"{progress_age:.1f} 秒前",
        )
        heartbeat_age = runtime_diag.get("worker_heartbeat_age_seconds")
        diag_cols[2].metric(
            "最近心跳",
            "暂无" if heartbeat_age is None else f"{heartbeat_age:.1f} 秒前",
        )
        pending = runtime_diag.get("pending") or {}
        diag_cols[3].metric("待处理请求", int(pending.get("pending_count", 0)))
        waiting_age = runtime_diag.get("waiting_age_seconds")
        if waiting_age is not None:
            st.caption(f"当前等待已持续 {waiting_age:.1f} 秒；等待时间本身不等于故障。")
        if worker_expected and not worker_alive:
            st.error("运行状态要求后台线程推进，但线程当前未存活。请下载诊断并检查最近错误；系统不会自动重启不确定状态的生成器。")
        elif runtime_diag.get("wait_kind") == "model_request":
            oldest = pending.get("oldest_pending_seconds")
            if oldest is not None:
                st.info(f"仍有模型请求或待收集结果，最早一项已持续 {oldest:.1f} 秒。请结合 API 诊断判断是正常等待还是请求超时。")
        elif runtime_diag.get("wait_kind") == "paused_with_pending_request":
            st.warning("对局已暂停，但后台仍有请求或待收集结果。不要直接重置；先查看请求状态并使用单步收集或跳过当前行动。")

        pending_requests = pending.get("pending_requests") or []
        if pending_requests and not single_view:
            st.caption("请求明细仅在非受限视角显示；不包含提示词或模型响应。")
            st.json(pending_requests)

        export_diag = dict(runtime_diag)
        export_pending = dict(pending)
        if single_view:
            export_pending.pop("pending_requests", None)
        export_diag["pending"] = export_pending
        st.download_button(
            "下载运行诊断（不含提示词与凭证）",
            data=json.dumps(export_diag, ensure_ascii=False, indent=2),
            file_name=f"runtime-diagnostic-{getattr(live_engine, 'game_id', 'game')}.json",
            mime="application/json",
            key=f"runtime-diagnostic-{getattr(live_engine, 'game_id', 'game')}",
        )

with st.container(key="workspace"):
    arena_col, feed_col = st.columns([1.45, 1], gap="large")
    with arena_col:
        st.markdown('<div class="wv-section">圆桌现场 <small>ARENA / 席位与行动</small></div>', unsafe_allow_html=True)
        st.markdown(design.stage(players, current, selected=ss.sel_seat, sheriff=sheriff, engine=engine), unsafe_allow_html=True)
        if engine:
            st.markdown(design.factions(engine, reveal_counts=faction_counts(source_engine) if single_view and reveal_counts else None), unsafe_allow_html=True)
        else:
            st.caption("每个席位显示身份、存活状态和当前行动；警长以 ♔ 标记。")
    with feed_col:
        st.markdown('<div class="wv-section">对局动态 <small>LIVE FEED / 已完成事件</small></div>', unsafe_allow_html=True)
        render_feed(st, engine, render_log_entry,
                    design.empty("每一句话，都是线索。", "开局后，这里会展示公开发言、法官播报与投票结果。"),
                    show_god=show_god, full_history=show_full_history,
                    limit=getattr(config, "PUBLIC_LOG_RENDER_LIMIT", 90))
        if engine and getattr(engine, "feed_issues", ()):
            st.warning("部分动态与当前对局状态不一致，已隔离显示。")
        st.caption("圆桌聚焦当前行动，动态保留已完成发言。示例中的私密记录也会标注来源。")

with st.container(key="insights_container"):
    st.markdown('<div class="wv-section">玩家观察室 <small>INSIGHTS / 模型自述与公开行动</small></div>', unsafe_allow_html=True)
    tab_single, tab_duel, tab_matrix = st.tabs(["玩家档案", "焦点对照", "全席概览"])
    with tab_single:
        profile_col, thought_col = st.columns([1, 2.3], gap="large")
        with profile_col:
            st.selectbox("选择要监控的玩家", [ss.viewer_seat] if single_view else list(range(1,13)), key="sel_seat",
                         format_func=lambda seat: f"{seat:02d} 号 · {players[seat]['role']}")
            selected = ss.sel_seat
            player = players[selected]
            badge = " · 警长" if sheriff == selected else ""
            dead_cls = " dead" if not player["alive"] else ""
            state_badge = '<span class="wv-pill alive">🟢 存活</span>' if player["alive"] else '<span class="wv-pill dead">💀 已出局</span>'
            st.markdown(f'<div class="wv-player{dead_cls}"><b>{selected:02d} 号 · {design.text(player["role"])}</b>'
                        f'<p>{state_badge}{badge} / {design.text(player["style"])}</p></div>', unsafe_allow_html=True)
            if engine:
                with st.expander("角色技能状态"):
                    state = engine.skill_states.get(selected, {})
                    st.json(state if state else {"说明":"该角色暂无可用的额外技能状态"})
                if engine.war_board:
                    wb_title = "狼队夜间作战板 · 狼人频道" if single_view else "狼队夜间作战板 · 全知"
                    with st.expander(wb_title):
                        st.write(f"第 {engine.war_board['day']} 夜 · {engine.war_board['note']}")
                        for seat, speech in engine.war_board["discussion"]:
                            st.write(f"{seat} 号：{speech}")
            st.caption("推理文本是模型输出的自述，不能直接视为经过验证的真实思考过程。")
        with thought_col:
            with st.container(height=300, border=True):
                logs = engine.reasoning_log.get(selected, []) if engine else []
                limit = len(logs) if show_full_history else int(getattr(config,"REASONING_RENDER_LIMIT",36))
                if logs:
                    st.markdown("".join(design.reasoning(row) for row in reversed(logs[-limit:])), unsafe_allow_html=True)
                else:
                    st.markdown(design.empty("先听他说，再看他如何解释。", "玩家完成一次行动后，这里会展示推理自述与对应的公开行动。"), unsafe_allow_html=True)
    with tab_duel:
        if single_view:
            st.caption("单一视角不显示其他玩家的推理。切回全知视角后可进行双方对照。")
        elif engine:
            default_left = engine.role_seats.get("预言家",1)
            default_right = next((seat for seat in engine.wolves if seat != default_left),2)
            left_col,right_col = st.columns(2, gap="large")
            for pane, key, default in [(left_col,"compare_left",default_left),(right_col,"compare_right",default_right)]:
                with pane:
                    seat = st.selectbox("对照席位", list(range(1,13)), index=default-1, key=key,
                                        format_func=lambda i:f"{i:02d} 号 · {players[i]['role']}")
                    logs = engine.reasoning_log.get(seat,[])
                    st.markdown(design.reasoning(logs[-1]) if logs else design.empty("暂无行动", "该玩家完成行动后显示最新记录。"), unsafe_allow_html=True)
        else:
            st.markdown(design.empty("并排比较，不急于下判断。", "开局后可自主选择两位玩家，对照他们的推理自述与公开发言。"), unsafe_allow_html=True)
    with tab_matrix:
        cards=[]
        for seat in range(1,13):
            p=players[seat]
            dead_cls = " dead" if not p["alive"] else ""
            state_badge = '<span class="wv-pill alive">🟢 存活</span>' if p["alive"] else '<span class="wv-pill dead">💀 已出局</span>'
            logs=engine.reasoning_log.get(seat,[]) if engine else []
            last=logs[-1]["reasoning"][:100] if logs else "此视角无法查看该席位的推理。" if single_view and seat != ss.viewer_seat else "等待该玩家完成第一次行动。"
            cards.append(f'<div class="wv-player{dead_cls}"><b>{seat:02d} 号 · {design.text(p["role"])}</b>'
                         f'<p>{state_badge} · {design.text(last)}</p></div>')
        st.markdown('<div class="wv-matrix">'+"".join(cards)+'</div>', unsafe_allow_html=True)

footer_slot = st.empty()
footer_slot.markdown('<div class="wv-footer"><span>SILICON WOLF VILLAGE · 让每一个判断都有迹可循</span><span>Python 裁决规则 / AI 演绎选择</span></div>', unsafe_allow_html=True)

# 每次完整页面正常结束，框架才能清除旧局节点。定时片段只安排下一次刷新。
# 保留完整页面刷新，让侧栏用量、诊断与主视图一起更新；片段自身不推进对局。
# 真人模式下持续轮询，直到快照确认真人待处理行动已出现；否则后台刚进入真人回合时，
# 若页面上一轮已看到 running=False，可能停止刷新，导致输入面板一直不显示。
human_waiting_for_snapshot = bool(
    live_engine is not None
    and getattr(live_engine, "human_seat", None) is not None
    and not ss.done
    and ss.game_service is not None
    and live_engine.players.get(live_engine.human_seat, {}).get("alive", False)
    and getattr(live_engine, "pending_human_action", None) is None
    and auto
)
schedule_page_refresh(
    st,
    bool(owner_replay_playing or (live_engine and ss.running and not ss.done and auto)
         or human_waiting_for_snapshot),
    0.75 if owner_replay_playing else AUTO_POLL_SECONDS,
)
