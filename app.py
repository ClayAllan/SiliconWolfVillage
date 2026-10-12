# -*- coding: utf-8 -*-
"""
app.py —— 上帝视角监控大屏（Streamlit 前端，V3.0）
==================================================
运行：  pip install -r requirements.txt  →  streamlit run app.py

V3.0 UI 修复/增强：
  · 修复“点击头像导致游戏重置”：圆桌头像改回普通 <div>（无 href 跳转），不再用 URL 参数切座位，
    监控对象统一用右侧下拉菜单选择（选中头像仍高亮）。
  · 公开台面/上帝读心：上方加【⬆️顶部/⬇️沉底】手动按钮（只滚动对应容器、不触发 rerun）。
  · 圆桌新增 🎖️ 警长徽标。
其余（生成器步进、硬超时、错误暴露、阵营进度条）保持不变。
"""

import json
import math
import os
import threading
import time
import uuid

import streamlit as st

import config
from session_storage import storage_paths
from ai_agent import AgentManager
from game_archive import archive_game
from game_engine import GameEngine
from ui_runtime import AUTO_POLL_SECONDS, rerun_delay, SPEED_PRESETS

# 手动进度存档与自动终局档案分开保存。
_PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))


# ============================================================
# 页面基础设置（不强制背景色，跟随 Streamlit 主题，明/暗皆可）
# ============================================================
st.set_page_config(page_title="赛博狼人杀斗蛐蛐大厅", page_icon="⚔️", layout="wide")
if "_storage_id" not in st.session_state:
    st.session_state._storage_id = str(uuid.uuid4())
SAVE_DIR, REPLAY_DIR = storage_paths(st.session_state._storage_id)
st.markdown("<style>.block-container{padding-top:1.1rem;}</style>", unsafe_allow_html=True)

# ------------------------------------------------------------
# 圆桌 CSS —— 赛博暗黑毛玻璃主题（Cyber Neon & Glassmorphism）
# ------------------------------------------------------------
TABLE_CSS = """
<style>
.lobby-arena { position: relative; width: 100%; max-width: 520px; aspect-ratio: 1 / 1; height: auto;
  margin: 10px auto; border-radius: 50%;
  background: radial-gradient(circle at 50% 50%, rgba(20, 28, 48, 0.88) 0%, rgba(10, 14, 24, 0.96) 75%, rgba(6, 8, 15, 0.98) 100%);
  border: 2px solid rgba(76, 139, 245, 0.6);
  box-shadow: 0 0 25px rgba(76, 139, 245, 0.25), inset 0 0 40px rgba(0, 0, 0, 0.8);
  backdrop-filter: blur(12px); }
.lobby-center { position: absolute; left: 50%; top: 50%; transform: translate(-50%,-50%);
  text-align: center; color: #ffffff;
  font-weight: 900; font-size: 15px; letter-spacing: 2px; line-height: 1.5;
  text-shadow: 0 0 12px #0a84ff, 0 0 24px rgba(10, 132, 255, 0.8), 0 2px 6px rgba(0,0,0,0.9);
  pointer-events: none; z-index: 2; }
.lobby-svg-layer { position: absolute; left: 0; top: 0; width: 100%; height: 100%; pointer-events: none; z-index: 1; }
.seat { position: absolute; width: 80px; height: 80px; transform: translate(-50%,-50%);
  border-radius: 16px; background: rgba(18, 24, 38, 0.94); border: 2px solid rgba(76, 139, 245, 0.65);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.6); transition: transform .2s ease, box-shadow .2s ease, border-color .2s ease;
  cursor: default; backdrop-filter: blur(10px); z-index: 3; color: #ffffff; }
.seat:hover { transform: translate(-50%,-50%) scale(1.08); z-index: 10; box-shadow: 0 6px 22px rgba(76, 139, 245, 0.5); }
.seat.camp-wolf { border-color: #ff453a; background: rgba(58, 16, 22, 0.94); box-shadow: 0 0 12px rgba(255, 69, 58, 0.3); }
.seat.camp-god { border-color: #0a84ff; background: rgba(14, 34, 68, 0.94); box-shadow: 0 0 12px rgba(10, 132, 255, 0.3); }
.seat.camp-villager { border-color: #30d158; background: rgba(16, 46, 26, 0.94); box-shadow: 0 0 12px rgba(48, 209, 88, 0.3); }
.seat.selected { outline: 3px solid #bf5af2; outline-offset: 3px; box-shadow: 0 0 20px rgba(191, 90, 242, 0.8); }
.seat .emoji { font-size: 24px; line-height: 1; filter: drop-shadow(0 2px 4px rgba(0,0,0,0.8)); }
.seat .num { font-size: 13px; font-weight: 800; margin-top: 2px; color: #ffffff !important; text-shadow: 0 1px 4px rgba(0,0,0,0.9); }
.seat .role { font-size: 11px; font-weight: 700; margin-top: 1px; text-shadow: 0 1px 4px rgba(0,0,0,0.9); }
.seat.camp-wolf .role { color: #ffb3b3 !important; }
.seat.camp-god .role { color: #b3dcff !important; }
.seat.camp-villager .role { color: #b3ffcc !important; }
.seat .badge-sheriff { position: absolute; top: -10px; right: -8px; font-size: 18px;
  filter: drop-shadow(0 0 6px rgba(255, 215, 0, 0.9)); animation: float-badge 2s ease-in-out infinite alternate; }
.seat .badge-lover { position: absolute; top: -10px; left: -8px; font-size: 16px;
  filter: drop-shadow(0 0 6px rgba(255, 55, 95, 0.9)); }
.seat .badge-charm { position: absolute; bottom: -8px; right: -8px; font-size: 16px;
  filter: drop-shadow(0 0 6px rgba(255, 45, 85, 0.9)); }
.seat.dead { filter: grayscale(0.85); opacity: .45; background: rgba(20, 22, 28, 0.8) !important; border-color: #555 !important; }
.seat.dead .num, .seat.dead .role { color: #8e8e93 !important; text-shadow: none !important; }
.seat.dead .xmark { position: absolute; left: 50%; top: 50%; transform: translate(-50%,-50%);
  font-size: 44px; color: #ff3b30; font-weight: 900; text-shadow: 0 0 10px rgba(255, 59, 48, 0.9); }
.seat.speaking { border-color: #ffd60a !important;
  box-shadow: 0 0 22px 6px rgba(255, 214, 10, 0.85), inset 0 0 12px rgba(255, 214, 10, 0.5) !important; z-index: 8;
  animation: speakpulse 1.2s ease-in-out infinite alternate; }
@keyframes speakpulse { from { transform: translate(-50%,-50%) scale(1.02); box-shadow: 0 0 16px 4px rgba(255, 214, 10, 0.7); }
                        to   { transform: translate(-50%,-50%) scale(1.08); box-shadow: 0 0 30px 9px rgba(255, 214, 10, 0.95); } }
@keyframes float-badge { from { transform: translateY(0); } to { transform: translateY(-3px); } }
</style>
"""


def render_table_html(players: dict, current, selected=None, sheriff=None, engine=None) -> str:
    """生成 12 人圆桌：赛博毛玻璃沙盘、阵营霓虹光圈、情侣动态连线与状态角标。"""
    seats_html = ""
    lover_coords = []
    lovers = getattr(engine, "lovers", None) if engine else None
    charm_target = getattr(engine, "charm_target", None) if engine else None

    for i in range(1, 13):
        p = players[i]
        ang = math.radians(-90 + (i - 1) * 30)
        x_pct = 50 + 38.3 * math.cos(ang)
        y_pct = 50 + 38.3 * math.sin(ang)

        if lovers and i in lovers:
            lover_coords.append((x_pct, y_pct))

        role_name = p.get("role", "未知")
        if engine and i in getattr(engine, "roles", {}):
            camp = "wolf" if engine.roles[i].camp == "狼人" else ("god" if role_name in config.GOD_ROLES else "villager")
        elif role_name in ("狼人", "白狼王", "狼美人"):
            camp = "wolf"
        elif role_name in config.GOD_ROLES:
            camp = "god"
        else:
            camp = "villager"

        cls = f"seat camp-{camp}"
        if not p["alive"]:
            cls += " dead"
        if current == i:
            cls += " speaking"
        if selected == i:
            cls += " selected"

        badge_sh = '<div class="badge-sheriff">🎖️</div>' if sheriff == i else ""
        badge_lv = '<div class="badge-lover">❤️</div>' if (lovers and i in lovers) else ""
        badge_ch = '<div class="badge-charm">💋</div>' if (charm_target == i and p["alive"]) else ""
        xmark = '<div class="xmark">✕</div>' if not p["alive"] else ""
        emoji = config.ROLE_EMOJI.get(p["role"], "❓")
        role = p["role"]

        seats_html += (
            f'<div class="{cls}" style="left:{x_pct:.2f}%; top:{y_pct:.2f}%;">'
            f'{badge_sh}{badge_lv}{badge_ch}{xmark}'
            f'<div class="emoji">{emoji}</div>'
            f'<div class="num">{i}号</div>'
            f'<div class="role">{role}</div></div>'
        )

    svg_line = ""
    if len(lover_coords) == 2:
        x1, y1 = lover_coords[0]
        x2, y2 = lover_coords[1]
        svg_line = (
            f'<svg class="lobby-svg-layer" viewBox="0 0 100 100">'
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="#ff375f" stroke-width="1.2" stroke-dasharray="2,2" opacity="0.75" />'
            f'</svg>'
        )

    return (
        TABLE_CSS
        + '<div class="lobby-arena">'
        + svg_line
        + '<div class="lobby-center">⚔️<br>赛博狼人杀<br><span style="font-size:12px;opacity:0.8;font-weight:600;">斗蛐蛐大厅</span></div>'
        + seats_html
        + "</div>"
    )



def render_radar_chart_svg(radar: dict) -> str:
    """🎨 生成原生五维战术博弈能力量化雷达图 (SVG/HTML)。"""
    if not radar:
        return ""
    labels = ["欺骗度", "识谎率", "纠错韧性", "保真度", "Token效能"]
    keys = ["deception", "lie_detection", "anti_snowball", "fact_consistency", "token_roi"]
    values = [float(radar.get(k, 60)) for k in keys]
    
    # 5 边形极坐标计算 (中心 150, 150, 半径 100)
    import math
    cx, cy, r = 160, 150, 95
    points = []
    grid_rings = []
    
    for ring in [0.25, 0.5, 0.75, 1.0]:
        ring_pts = []
        for idx in range(5):
            angle = -math.pi / 2 + idx * (2 * math.pi / 5)
            rx = cx + r * ring * math.cos(angle)
            ry = cy + r * ring * math.sin(angle)
            ring_pts.append(f"{rx:.1f},{ry:.1f}")
        grid_rings.append(" ".join(ring_pts))

    val_pts = []
    label_svg = ""
    for idx, (val, lbl) in enumerate(zip(values, labels)):
        angle = -math.pi / 2 + idx * (2 * math.pi / 5)
        normalized = max(0.1, min(1.0, val / 100.0))
        vx = cx + r * normalized * math.cos(angle)
        vy = cy + r * normalized * math.sin(angle)
        val_pts.append(f"{vx:.1f},{vy:.1f}")
        
        # 标签文字坐标
        tx = cx + (r + 28) * math.cos(angle)
        ty = cy + (r + 28) * math.sin(angle) + 4
        label_svg += f'<text x="{tx:.1f}" y="{ty:.1f}" font-size="11" fill="rgba(255,255,255,0.85)" text-anchor="middle" font-weight="600">{lbl} {val:.0f}</text>'

    val_polygon = " ".join(val_pts)
    
    grids_svg = "".join(f'<polygon points="{g}" fill="none" stroke="rgba(255,255,255,0.12)" stroke-width="1"/>' for g in grid_rings)
    axes_svg = ""
    for idx in range(5):
        angle = -math.pi / 2 + idx * (2 * math.pi / 5)
        ax = cx + r * math.cos(angle)
        ay = cy + r * math.sin(angle)
        axes_svg += f'<line x1="{cx}" y1="{cy}" x2="{ax:.1f}" y2="{ay:.1f}" stroke="rgba(255,255,255,0.15)" stroke-width="1"/>'

    svg = f"""
    <div style="background:rgba(255,255,255,0.03);border:1px solid rgba(10,132,255,0.3);border-radius:12px;padding:12px;margin:12px 0;">
      <div style="font-size:13px;font-weight:700;color:#0a84ff;margin-bottom:8px;display:flex;justify-content:space-between;">
        <span>🎯 LLM 战术博弈能力量化雷达图</span>
        <span style="color:#30d158;font-weight:700;">综合评分: {radar.get('overall', 0)} 分</span>
      </div>
      <div style="text-align:center;">
        <svg viewBox="0 0 320 300" style="max-width:320px;height:auto;">
          {grids_svg}
          {axes_svg}
          <polygon points="{val_polygon}" fill="rgba(10,132,255,0.35)" stroke="#0a84ff" stroke-width="2.5"/>
          {label_svg}
        </svg>
      </div>
    </div>
    """
    return svg

def render_vote_chart(e: dict) -> str:
    """📊 横向 HTML/CSS 票型柱状图：支持加权票型渲染、票王皇冠与投票来源标签。"""
    totals = {int(k): float(v) for k, v in (e.get("totals") or {}).items()}
    votes = {int(k): int(v) for k, v in (e.get("votes") or {}).items()}
    winner = e.get("winner")
    winner = int(winner) if winner not in (None, "", 0) else None
    sheriff = e.get("sheriff")
    sheriff = int(sheriff) if sheriff not in (None, "", 0) else None
    title = e.get("title", "票型")

    by_target, abstain = {}, []
    for v, t in votes.items():
        if t and t in totals:
            by_target.setdefault(t, []).append(v)
        elif not t:
            abstain.append(v)

    if not totals:
        inner = '<div style="opacity:.6;font-size:12px;">无人投票 / 全场弃票。</div>'
    else:
        maxw = max(totals.values()) or 1.0
        inner = ""
        for target, w in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0])):
            pct = max(8.0, w / maxw * 100.0)
            is_king = (winner is not None and target == winner)
            bar_color = "linear-gradient(90deg, #ff453a, #ff9f0a)" if is_king else "linear-gradient(90deg, #0a84ff, #5e5ce6)"
            crown = " 👑" if is_king else ""
            vlist = sorted(by_target.get(target, []))
            vtxt = "、".join(f"{x}号" + ("🎖️" if x == sheriff else "") for x in vlist) or "—"
            inner += (
                '<div style="margin:6px 0;">'
                '<div style="display:flex;justify-content:space-between;font-size:12px;color:var(--text-color);">'
                f'<b>{target}号{crown}</b><span style="font-weight:700;color:#5b9bff;">{w:g} 票</span></div>'
                '<div style="background:rgba(130,140,165,0.15);border-radius:6px;overflow:hidden;height:18px;margin-top:3px;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">'
                f'<div style="width:{pct:.1f}%;height:100%;background:{bar_color};border-radius:6px;box-shadow:0 0 8px rgba(10,132,255,0.4);"></div></div>'
                f'<div style="font-size:11px;opacity:.75;margin-top:2px;">投给此座位的玩家：{vtxt}</div>'
                '</div>'
            )
        if abstain:
            inner += (f'<div style="font-size:11px;opacity:.65;margin-top:4px;border-top:1px dashed rgba(255,255,255,0.1);padding-top:3px;">'
                      f'弃票/空票玩家：{"、".join(f"{x}号" for x in sorted(abstain))}</div>')
    return (
        '<div style="border:1px solid rgba(10,132,255,0.35);border-radius:10px;padding:10px 12px;'
        'background:rgba(10,132,255,0.06);margin:10px 0;box-shadow:0 2px 10px rgba(0,0,0,0.15);">'
        f'<div style="color:#5b9bff;font-weight:800;font-size:13px;margin-bottom:6px;">📊 {title}</div>'
        f'{inner}</div>'
    )


def render_log_entry(e: dict) -> str:
    kind = e["kind"]
    phase = e.get("phase", "")
    text = e.get("text", "")
    if kind == "votechart":
        return render_vote_chart(e)
    if kind == "judge":
        return (
            f'<div style="margin:8px 0;padding:6px 12px;background:rgba(255,59,48,0.1);'
            f'border-left:3px solid #ff453a;border-radius:6px;font-size:13px;color:var(--text-color);">'
            f'<b style="color:#ff453a;">🎙️ 法官播报</b> <span style="opacity:0.6;font-size:11px;">[{phase}]</span><br>{text}</div>'
        )
    if kind == "player":
        seat = e.get("seat", "")
        return (
            f'<div style="margin:8px 0;padding:8px 12px;background:rgba(10,132,255,0.08);'
            f'border-left:3px solid #0a84ff;border-radius:6px;font-size:13px;color:var(--text-color);">'
            f'<div style="display:flex;justify-content:space-between;margin-bottom:3px;">'
            f'<b style="color:#0a84ff;">🗣️ {seat}号 发言</b>'
            f'<span style="opacity:0.55;font-size:11px;">[{phase}]</span></div>'
            f'<div style="line-height:1.45;opacity:0.95;">{text}</div></div>'
        )
    if kind == "god":
        return (
            f'<div style="margin:5px 0;padding:5px 10px;background:rgba(191,90,242,0.08);'
            f'border-left:3px solid #bf5af2;border-radius:6px;color:#bf5af2;font-size:12px;opacity:0.92;">'
            f'👁️ <b>上帝情报</b> <span style="opacity:0.6;font-size:11px;">[{phase}]</span>：{text}</div>'
        )
    if kind == "win":
        return (
            f'<div style="margin:14px 0;padding:12px;background:radial-gradient(circle, rgba(255,214,10,0.15) 0%, rgba(255,149,0,0.05) 100%);'
            f'border:2px solid #ffd60a;border-radius:10px;color:#ffd60a;font-size:18px;font-weight:800;'
            f'text-align:center;box-shadow:0 0 20px rgba(255,214,10,0.25);">'
            f'🏆 {text}</div>'
        )
    return f'<div style="color:var(--text-color);">{text}</div>'


def render_faction_bar(engine) -> str:
    """阵营存活条：存活的 狼/神/民 + 已出局，渐变分段展示。"""
    alive = engine.alive_seats()
    w = sum(1 for s in alive if engine.roles[s].camp == "狼人")
    g = sum(1 for s in alive if engine.players[s]["role"] in config.GOD_ROLES)
    v = sum(1 for s in alive if engine.players[s]["role"] == "平民")
    d = 12 - len(alive)
    segs = [
        ("🐺 狼队", w, "linear-gradient(90deg, #ff453a, #d70015)"),
        ("🛡️ 神职", g, "linear-gradient(90deg, #0a84ff, #0056b3)"),
        ("👤 平民", v, "linear-gradient(90deg, #30d158, #1b8a38)"),
        ("💀 出局", d, "linear-gradient(90deg, #636366, #3a3a3c)")
    ]
    cells = ""
    for label, cnt, grad in segs:
        if cnt <= 0:
            continue
        cells += (f'<div style="flex:{cnt};background:{grad};color:#fff;text-align:center;'
                  f'padding:7px 2px;font-size:12px;font-weight:700;white-space:nowrap;'
                  f'box-shadow:inset 0 1px 0 rgba(255,255,255,0.2);">{label} {cnt}</div>')
    return (f'<div style="display:flex;width:100%;border-radius:10px;overflow:hidden;'
            f'box-shadow:0 3px 10px rgba(0,0,0,.35);margin:4px 0 10px 0;border:1px solid rgba(255,255,255,0.1);">{cells}</div>')

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


def scroll_buttons(anchor_id: str, suffix: str):
    """容器上方的【⬆️顶部 / ⬇️沉底】按钮：onclick 只滚动该容器(不动整页)，不触发 Streamlit rerun。"""
    tpl = """
    <style>
      .ccd-bar { display:flex; gap:8px; justify-content:flex-end; font-family:sans-serif; margin:0; }
      .ccd-bar button { font-size:12px; padding:3px 10px; border-radius:6px;
        border:1px solid #4c8bf5; background:rgba(76,139,245,.16); color:#4c8bf5; cursor:pointer; }
      .ccd-bar button:hover { background:rgba(76,139,245,.32); }
    </style>
    <div class="ccd-bar">
      <button onclick="ccdScroll_SUFFIX('top')">⬆️ 顶部</button>
      <button onclick="ccdScroll_SUFFIX('bottom')">⬇️ 沉底</button>
    </div>
    <script>
    function ccdScroll_SUFFIX(where) {
      const doc = window.parent.document;
      const el = doc.getElementById('ANCHOR');
      if (!el) return;
      let box = el.closest('[data-testid="stVerticalBlockBorderWrapper"]');
      if (!box) {
        let n = el.parentElement;
        while (n && n !== doc.body) {
          const oy = window.parent.getComputedStyle(n).overflowY;
          if ((oy === 'auto' || oy === 'scroll') && n.scrollHeight > n.clientHeight) { box = n; break; }
          n = n.parentElement;
        }
      }
      if (box) box.scrollTop = (where === 'top') ? 0 : box.scrollHeight;
    }
    </script>
    """
    st.iframe(tpl.replace("SUFFIX", suffix).replace("ANCHOR", anchor_id), height=40)


def render_war_board(engine) -> str:
    """🐺 狼队夜间作战板：展示最近一夜狼队的分工讨论与最终刀法（仅上帝视角）。"""
    wb = engine.war_board
    if not wb:
        return '<div style="opacity:.55;font-size:12px;padding:4px 2px;">🐺 狼队作战板：今夜尚未行动。</div>'
    rows = ""
    for seat, text in wb["discussion"]:
        rows += (f'<div style="margin:3px 0;font-size:12px;line-height:1.4;">'
                 f'<b style="color:#fa5252;">{seat}号狼</b>'
                 f'<span style="color:var(--text-color);opacity:.9;">：{text}</span></div>')
    return (
        '<div style="border:1px solid #fa5252;border-radius:9px;padding:8px 11px;'
        'background:rgba(250,82,82,.08);margin-top:6px;">'
        f'<div style="color:#fa5252;font-weight:700;margin-bottom:3px;">🐺 第{wb["day"]}夜 · 狼队夜间作战板</div>'
        f'<div style="color:#fa5252;font-size:13px;margin-bottom:5px;">🔪 今夜决定：<b>{wb["note"]}</b></div>'
        f'{rows}</div>'
    )


# ============================================================
# 会话状态
# ============================================================
def init_state():
    ss = st.session_state
    ss.setdefault("engine", None)
    ss.setdefault("gen", None)
    ss.setdefault("running", False)
    ss.setdefault("done", False)
    ss.setdefault("status", "等待开始")
    ss.setdefault("error", "")
    ss.setdefault("sel_seat", 1)
    ss.setdefault("advancing", False)        # UI 可见的推进状态
    ss.setdefault("advancing_since", 0.0)
    ss.setdefault("advance_lock", threading.Lock())  # 真正互斥：任何线程都不能并发 next(gen)
    ss.setdefault("last_event_type", "")
    ss.setdefault("last_archive", None)
    ss.setdefault("archive_error", "")
    ss.setdefault("agent_timeout", config.AGENT_TIMEOUT)   # 本局单步思考上限


def archive_engine(engine, reason: str, force: bool = False):
    """自动归档包装：写盘失败只报告，不得打断状态机。"""
    if engine is None:
        return None
    try:
        result = archive_game(engine, REPLAY_DIR, reason=reason, force=force)
        if result:
            st.session_state.last_archive = result
            st.session_state.archive_error = ""
            config.get_logger().info(
                f"ARCHIVE✓ game={result['game_id']} completed={result['completed']} dir={result['directory']}"
            )
        return result
    except Exception as exc:
        message = f"自动归档失败: {type(exc).__name__}: {exc}"
        st.session_state.archive_error = message
        config.get_logger().exception(message)
        return None


def start_game(api_key, base_url, good_model, wolf_model, temperature, agent_timeout, board=None):
    old_engine = st.session_state.get("engine")
    if old_engine is not None:
        old_engine.manager.cancel_all_pending("开始新游戏")
        archive_engine(old_engine, "replaced_by_new_game")
    board_roles = config.GAME_BOARDS.get(board) or config.GAME_BOARDS[config.DEFAULT_BOARD]
    config.get_logger().info(f"==== 新游戏开始 板子={board} 好营={good_model} 狼营={wolf_model} "
                             f"单步超时={agent_timeout}s temp={temperature} ====")
    manager = AgentManager(api_key=api_key, base_url=base_url,
                           good_model=good_model, wolf_model=wolf_model,
                           temperature=temperature, agent_timeout=int(agent_timeout))
    engine = GameEngine(manager)
    engine.board_name = board or config.DEFAULT_BOARD
    # 必须在 setup 前挂载记录器，才能保留发牌、完整 System Prompt 和私人底牌初始化。
    engine.enable_recording(REPLAY_DIR)
    engine.setup(board_roles)
    st.session_state.engine = engine
    st.session_state.gen = engine.run()
    st.session_state.agent_timeout = int(agent_timeout)
    st.session_state.running = True
    st.session_state.done = False
    st.session_state.error = ""
    st.session_state.advancing = False
    st.session_state.advancing_since = 0.0
    st.session_state.last_event_type = ""
    st.session_state.status = "游戏开始"


# ============================================================
# 存档 / 读档（V6.0）—— ⚠️生成器陷阱：读档必须【重建】引擎，由 run() 的 _resume 分支
#   静默快进到“下一夜”再 st.rerun() 释放推进锁；绝不在原生成器上恢复(必报 generator already executing)。
# ============================================================
def list_saves() -> list:
    try:
        os.makedirs(SAVE_DIR, exist_ok=True)
        return sorted((f for f in os.listdir(SAVE_DIR) if f.endswith(".json")), reverse=True)
    except Exception:
        return []


def save_game(engine) -> str:
    os.makedirs(SAVE_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"save_D{engine.day}_{ts}.json"
    payload = {
        "version": "v6",
        "saved_at": ts,
        "meta": {"day": engine.day, "phase": engine.phase,
                 "winner": engine.winner, "alive": len(engine.alive_seats())},
        "engine": engine.export_state(),
        "manager": engine.manager.export_state(),
    }
    with open(os.path.join(SAVE_DIR, name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return name


def load_game(filename, api_key, base_url, good_model, wolf_model, temperature, agent_timeout):
    """读档：重建 AgentManager+GameEngine 并注入状态。返回 engine(其 _resume=True，run() 会从下一夜续跑)。"""
    with open(os.path.join(SAVE_DIR, filename), "r", encoding="utf-8") as f:
        payload = json.load(f)
    manager = AgentManager(api_key=api_key, base_url=base_url,
                           good_model=good_model, wolf_model=wolf_model,
                           temperature=temperature, agent_timeout=int(agent_timeout))
    manager.load_state(payload.get("manager", {}))
    engine = GameEngine(manager)
    engine.load_state(payload.get("engine", {}))   # 内部置 _resume=True
    engine.enable_recording(REPLAY_DIR)
    return engine


def advance_step() -> bool:
    """推进一个短步骤；同一会话在任意时刻最多允许一个线程调用 next(gen)。"""
    ss = st.session_state
    _log = config.get_logger()
    if ss.gen is None:
        return True

    lock = ss.get("advance_lock")
    if lock is None:
        lock = threading.Lock()
        ss.advance_lock = lock
    if not lock.acquire(blocking=False):
        _log.warning("STEP⊘ 另一个线程正在推进，拒绝并发 next(gen)")
        return False

    ss.advancing = True
    ss.advancing_since = time.time()
    advanced = True
    t0 = time.monotonic()
    try:
        ev = next(ss.gen)
        event_type = ev.get("type", "")
        ss.last_event_type = event_type
        ss.status = ev.get("status", "")
        # waiting 每 0.25 秒左右轮询一次，不逐条写 INFO，避免长思考制造数百行无效日志。
        if event_type != "waiting":
            _log.info(f"STEP✓ {event_type} '{ss.status}' 用时{time.monotonic() - t0:.1f}s")
        if event_type == "end":
            archive_engine(ss.engine, "game_end")
        fatal = getattr(ss.engine, "fatal_error", "") if ss.engine else ""
        if fatal:
            ss.engine.fatal_error = ""
            ss.running = False
            ss.error = f"API已暂停: {fatal}"
            ss.status = "API 已暂停（处理后点击继续）"
            _log.error(ss.error)
            st.error(ss.error)
    except StopIteration:
        ss.done = True
        ss.running = False
        ss.last_event_type = "game_over"
        ss.status = "游戏结束"
        archive_engine(ss.engine, "stop_iteration")
        _log.info("STEP■ 游戏结束(StopIteration)")
    except ValueError as e:
        if "generator already executing" in str(e):
            # 仍保留最后防线，但不会再次强制推进；下轮由同一把互斥锁正常接管。
            advanced = False
            _log.exception("STEP⊘ 捕获 generator already executing，已拒绝本次推进")
        else:
            ss.running = False
            ss.error = f"引擎崩溃: {e}"
            ss.status = "引擎崩溃"
            archive_engine(ss.engine, "engine_error")
            _log.error(f"STEP✗ 引擎崩溃(ValueError): {e}")
            st.error(ss.error)
    except Exception as e:
        ss.running = False
        ss.error = f"引擎崩溃: {e}"
        ss.status = "引擎崩溃"
        archive_engine(ss.engine, "engine_error")
        _log.exception(f"STEP✗ 引擎崩溃: {e}")
        st.error(ss.error)
    finally:
        ss.advancing = False
        lock.release()
    return advanced


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

def test_connection(api_key, base_url, model):
    """最小请求探活，暴露真实错误（模型名错 / Key 错 / 网络超时）。"""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": '只回复JSON：{"ok":1}'}],
            response_format={"type": "json_object"},
            timeout=20,
        )
        return True, f"✅ 连接成功，模型「{model}」可用。"
    except Exception as e:
        return False, f"❌ 失败：{type(e).__name__}: {e}"


# ============================================================
# 主流程
# ============================================================
init_state()
engine = st.session_state.engine
ss = st.session_state

# 热更新兼容：若修改代码前已有一局刚结束但尚未落盘，在当前会话下一次 rerun 时补归档。
if engine is not None and getattr(engine, "winner", None) and (ss.done or engine.phase == "游戏结束"):
    archive_engine(engine, "terminal_session_backfill")

# ---------------- 侧边栏：配置 + 操作 ----------------
with st.sidebar:
    st.header("⚙️ 控制台")
    # 🎲 游戏板子选择(V6.4)：开新局/读档前切换，决定本局发什么身份(如含白狼王)。
    _boards = list(config.GAME_BOARDS.keys())
    board = st.selectbox("🎲 游戏板子", _boards,
                         index=_boards.index(config.DEFAULT_BOARD) if config.DEFAULT_BOARD in _boards else 0,
                         help="开新局点【开始新游戏】时生效；不同板子=不同身份配置。")
    default_key = ""  # Public visitors always supply their own key.
    api_key = st.text_input("DeepSeek API Key", value=default_key, type="password",
                            help="填写你自己的 API Key；请求由托管服务器转发给所选模型服务。")
    base_url = st.text_input("Base URL", value=config.BASE_URL)
    # 🎭 阵营多模型对战(V6.0)：好人/狼人各选一个模型，测试不同 LLM 的撒谎/识谎能力。
    _mopts = list(dict.fromkeys(list(config.MODEL_OPTIONS) + [config.GOOD_CAMP_MODEL, config.WOLF_CAMP_MODEL]))
    good_model = st.selectbox("🛡️ 好人阵营模型", _mopts, index=_mopts.index(config.GOOD_CAMP_MODEL),
                              help="神职(预/女/猎/守)与平民发往该模型")
    wolf_model = st.selectbox("🐺 狼人阵营模型", _mopts, index=_mopts.index(config.WOLF_CAMP_MODEL),
                              help="4 名狼人发往该模型，可与好人不同以对比撒谎能力")
    temperature = st.slider("Temperature", 0.0, 1.5, float(config.TEMPERATURE), 0.1)
    agent_timeout = st.slider("单步思考上限（秒）", 15, 240, int(config.AGENT_TIMEOUT), 5,
                              help="深度思考模型可调大；嫌每步太慢可调小（到点强制兜底继续）。开新游戏生效。")
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
    if engine is not None:
        engine.react_budget = int(react_budget)   # ★实时改写引擎当日预算(无需重开局)
    auto = st.checkbox("🔁 自动推演", value=True)
    show_god = st.checkbox("👁️ 公屏显示上帝视角(夜间)情报", value=True)
    show_full_history = st.checkbox(
        "📚 渲染完整频道历史",
        value=False,
        help="默认只渲染最新记录以减小 WebSocket 消息；完整原始记录仍会写入本局归档。暂停后可开启回看。",
    )

    if st.button("🔌 测试连接", use_container_width=True):
        ok, msg = test_connection(api_key, base_url, good_model)
        (st.success if ok else st.error)(msg)

    auto_running = bool(auto and ss.running)
    st.markdown("**🎮 操作**")
    b1, b2 = st.columns(2)
    if b1.button("🎬 开始新游戏", use_container_width=True):
        if not api_key or api_key.startswith("sk-请"):
            st.error("请先填入有效的 API Key！")
        else:
            start_game(api_key, base_url, good_model, wolf_model, temperature, agent_timeout, board)
            st.rerun()
    pause_label = "⏸️ 暂停" if ss.running else "▶️ 继续"
    if b2.button(pause_label, use_container_width=True, disabled=(engine is None or ss.done)):
        if not ss.running and str(ss.get("error", "")).startswith("API已暂停"):
            ss.error = ""
        ss.running = not ss.running
        st.rerun()
    b3, b4 = st.columns(2)
    if b3.button("⏭️ 单步", use_container_width=True, disabled=(engine is None or ss.done or auto_running)):
        advance_step()
        st.rerun()
    if b4.button("🔄 重置", use_container_width=True, disabled=auto_running):
        if engine is not None:
            engine.manager.cancel_all_pending("对局重置")
            archive_engine(engine, "manual_reset")
        for k in ("engine", "gen", "running", "done", "status", "error", "advancing", "advancing_since", "last_event_type"):
            st.session_state.pop(k, None)
        st.rerun()

    # 真正跳过当前玩家：只标记当前 request_id 失效，由 _ask 下一次轮询写入安全兜底。
    # 不清推进锁、不直接 next(gen)，因此不会制造 generator already executing。
    can_skip = bool(engine and not ss.done and engine.has_pending_action())
    if st.button("⏭️ 跳过当前玩家", use_container_width=True, disabled=not can_skip):
        if engine.request_skip_current():
            ss.status = f"正在跳过 {engine.current_speaker}号…"
        st.rerun()

    st.divider()

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

    # ===== 📜 复盘与导出 =====
    with st.expander("📜 复盘与导出"):
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
        if engine:
            _busy = bool(ss.get("advancing", False) or engine.has_pending_action()
                         or engine.manager.has_pending())
            if st.button("💾 保存当前进度", use_container_width=True, disabled=_busy):
                try:
                    nm = save_game(engine)
                    st.success(f"已存档：{nm}")
                    st.toast(f"💾 已保存 {nm}")
                except Exception as e:
                    st.error(f"存档失败：{type(e).__name__}: {e}")
        else:
            st.caption("开始或读取游戏后可存档。")

        _saves = list_saves()
        if _saves:
            pick = st.selectbox("选择存档读取", _saves, key="load_pick")
            st.caption("⚠️ 读档会销毁当前对局，并从存档当天的【下一夜】继续推演"
                       "（生成器无法原位恢复）；建议在白天阶段存档以减少跳过。")
            if st.button("📂 读取存档", use_container_width=True, disabled=auto_running):
                if not api_key or api_key.startswith("sk-请"):
                    st.error("请先填入有效的 API Key！")
                else:
                    try:
                        if engine is not None:
                            engine.manager.cancel_all_pending("读取其他存档")
                        eng = load_game(pick, api_key, base_url, good_model, wolf_model,
                                        temperature, agent_timeout)
                        ss.engine = eng
                        ss.agent_timeout = int(agent_timeout)
                        ss.error = ""
                        ss.advancing = False          # ★释放推进锁
                        ss.advancing_since = 0.0
                        if eng.winner:                # 存档已是终局：只展示，不再续跑
                            ss.gen = None
                            ss.running = False
                            ss.done = True
                            ss.status = f"已读档：{eng.winner}阵营已获胜（终局）"
                        else:
                            ss.gen = eng.run()        # 新生成器；run() 内 _resume 分支会跳过开场、续到下一夜
                            ss.running = False        # 默认暂停，由用户点 ▶️/⏭️ 续跑
                            ss.done = False
                            ss.status = f"已读档（将从第{eng.day + 1}夜继续，点 ▶️继续 或 ⏭️单步）"
                        config.get_logger().info(
                            f"==== 读档恢复 {pick} day={eng.day} phase={eng.phase} winner={eng.winner} ====")
                        st.rerun()                    # ★重启前端循环
                    except Exception as e:
                        st.error(f"读档失败：{type(e).__name__}: {e}")
        else:
            st.caption("（暂无存档）")

    # ===== ⚡ 上帝干预（实时塞剧本）=====
    with st.expander("⚡ 上帝干预 (实时塞剧本)"):
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
            if st.button("⚡ 发送法旨", use_container_width=True):
                if god_text.strip():
                    engine.manager.inject(
                        god_seat,
                        f"【上帝法旨·最高指令】{god_text.strip()}。你必须在接下来的所有发言和行动中绝对执行此指令！")
                    st.success(f"已向 {god_seat}号 下达法旨！")
                    st.toast(f"⚡ 法旨已送达 {god_seat}号")
                else:
                    st.warning("请先输入法旨内容。")
        else:
            st.caption("开始游戏后可对任意 AI 实时传音。")

    st.caption("API Key 仅用于本次会话。存档按会话隔离，请及时下载复盘；重新连接后可能无法找回。")

    st.divider()
    st.caption("4狼/4民/预言家/女巫/猎人/守卫｜屠边局：神灭或民灭则狼胜，狼灭则好人胜。\n\n"
               "Day1 白天先竞选警长(票×1.5)；狼人可自爆打断竞选/白天。")

# ---------------- 顶部标题 + 战况可视化 ----------------
st.markdown("<h1 style='text-align:center;margin-bottom:0;'>⚔️ 赛博狼人杀 · 斗蛐蛐大厅 🐺</h1>", unsafe_allow_html=True)
if engine:
    day_v = f"第 {engine.day} 天" if engine.day else "准备中"
    phase_v, alive_v, winner_v = engine.phase, f"{len(engine.alive_seats())} / 12", (engine.winner or "进行中")
    sheriff_v = f"{engine.sheriff}号" if engine.sheriff else "无"
else:
    day_v, phase_v, alive_v, winner_v, sheriff_v = "—", "未开始", "— / 12", "—", "无"
m1, m2, m3, m4, m5, m6 = st.columns(6)
m1.metric("📅 天数", day_v)
m2.metric("🎬 阶段", phase_v)
m3.metric("❤️ 存活", alive_v)
m4.metric("🎖️ 警长", sheriff_v)
m5.metric("🏆 胜者", winner_v)
_header_usage = current_token_usage(engine)
m6.metric("🧮 Tokens", compact_token_count(_header_usage["total_tokens"]))
if engine:
    st.markdown(render_faction_bar(engine), unsafe_allow_html=True)
st.markdown(
    f"<div style='text-align:center;color:var(--text-color);opacity:.7;margin:2px 0 6px 0;'>"
    f"当前状态：<b>{ss.status}</b></div>",
    unsafe_allow_html=True,
)
# 不再创建每 100ms 更新一次的 components iframe 计时器。长局中反复销毁 iframe
# 会显著增加浏览器与 Streamlit WebSocket 的消息压力；状态文本和后台日志仍保留耗时。
if engine and ss.running and not ss.done:
    elapsed = max(0, int(time.time() - float(ss.get("advancing_since", 0) or time.time())))
    st.caption(f"⏱️ 当前步骤正在推进；后台请求有硬超时保护。已等待约 {elapsed} 秒。")
if ss.error:
    st.error(ss.error)
st.divider()

# ============================================================
# 三分屏主体
# ============================================================
col_left, col_mid, col_right = st.columns([1.2, 1, 1])

if engine:
    players = engine.players
    current = engine.current_speaker
    sheriff = engine.sheriff
else:
    players = {i: {"role": "未知", "alive": True, "style": "-", "memory": ""} for i in range(1, 13)}
    current = None
    sheriff = None

# ---------------- 区域 A：圆桌沙盘 ----------------
with col_left:
    st.markdown("#### 🎴 上帝视角圆桌")
    st.markdown(render_table_html(players, current, selected=ss.sel_seat, sheriff=sheriff, engine=engine), unsafe_allow_html=True)
    st.caption("💡 请在右侧下拉菜单选择要监控的玩家（选中座位会高亮，🎖️ 为警长）。")
    if engine:
        st.caption(f"存活 {len(engine.alive_seats())} / 12　·　🎖️ 警长：{sheriff_v}")
        st.markdown(render_war_board(engine), unsafe_allow_html=True)
    else:
        st.info("👈 在左侧填入 API Key，然后点击「🎬 开始新游戏」。")

# ---------------- 区域 B：公开台面（公共发言）----------------
with col_mid:
    st.markdown("#### 📢 公开台面（明面发言）")
    scroll_buttons("ccd-public-end", "pub")
    with st.container(height=560):
        if engine and engine.public_log:
            entries = [e for e in engine.public_log if show_god or e["kind"] != "god"]
            omitted = 0
            if not show_full_history:
                limit = int(getattr(config, "PUBLIC_LOG_RENDER_LIMIT", 90))
                omitted = max(0, len(entries) - limit)
                entries = entries[-limit:]
            body = "".join(render_log_entry(e) for e in entries)
            if omitted:
                body = (f'<div style="opacity:.62;font-size:12px;margin-bottom:8px;">'
                        f'为保持长局连接稳定，已省略前 {omitted} 条界面渲染；完整内容已写入对局归档。'
                        f'</div>' + body)
        else:
            body = '<div style="opacity:.6">暂无播报，开始游戏后这里会实时滚动法官播报与玩家发言。</div>'
        st.markdown(body + '<span id="ccd-public-end"></span>', unsafe_allow_html=True)

# ---------------- 区域 C：上帝读心（内心独白）----------------
with col_right:
    st.markdown("#### 🧠 上帝读心枢纽")
    tab_single, tab_duel, tab_matrix = st.tabs(["🎯 单人监控", "⚔️ 预言家对决", "🌐 12人矩阵"])

    with tab_single:
        scroll_buttons("ccd-inner-end", "inner")
        st.selectbox(
            "选择要监控的玩家",
            list(range(1, 13)),
            key="sel_seat",
            format_func=lambda i: f"{i}号 — {config.ROLE_EMOJI.get(players[i]['role'],'❓')} {players[i]['role']}",
        )
        sel = ss.sel_seat
        p = players[sel]
        status = "🟢 存活" if p["alive"] else "💀 出局"
        badge_txt = "　🎖️ 警长" if sheriff == sel else ""
        st.markdown(
            f"**真身：** {config.ROLE_EMOJI.get(p['role'],'❓')} {p['role']}　**状态：** {status}{badge_txt}<br>"
            f"**风格：** {p['style']}",
            unsafe_allow_html=True,
        )
        if engine:
            role = p["role"]
            if role == "女巫":
                w = engine.skills["witch"]
                skill_txt = f"解药：{'✅ 可用' if w['antidote'] else '❌ 已用'}　|　毒药：{'✅ 可用' if w['poison'] else '❌ 已用'}"
            elif role == "预言家":
                checks = engine.skills["seer"]["checks"]
                skill_txt = "验人记录：" + ("，".join(f"{t}号={r}" for t, r in checks) if checks else "暂无")
            elif role == "守卫":
                skill_txt = f"上次守护：{engine.skills['guard']['last'] or '无'}"
            elif role == "猎人":
                h = engine.skills["hunter"]
                skill_txt = f"开枪：{'可开枪' if h['can_shoot'] else '已失效'}　|　带走：{h['shot'] or '无'}"
            elif role == "守墓人":
                _le = f"{engine.last_exiled_seat}号={engine.last_exiled_camp}" if engine.last_exiled_seat else "暂无"
                skill_txt = f"🪦 守墓人　|　最近墓地信息：{_le}"
            elif role == "骑士":
                skill_txt = f"🐎 决斗：{'已用掉' if engine.roles[sel].has_dueled else '可用(一次)'}"
            elif role == "丘比特":
                _lv = engine.lovers
                skill_txt = f"💘 情侣：{_lv[0]}号 ❤️ {_lv[1]}号" if _lv else "💘 丘比特（开局连定一对情侣）"
            elif role == "摄梦人":
                _dt = getattr(engine.roles[sel], "dream_target", None)
                skill_txt = f"🌙 摄梦人　|　今夜梦游者：{_dt or '未摄梦'}（连摄两晚暴毙，摄梦人死则连带）"
            elif role == "恶灵骑士":
                _ref = getattr(engine.roles[sel], "has_reflected", False)
                skill_txt = f"💀 恶灵骑士　|　狼队特权（不可自爆/夜死免疫）| 反伤盾：{'❌ 已触发' if _ref else '🛡️ 完好可用(反伤验/毒神职)'}"
            elif role == "魔术师":
                _sw = getattr(engine.roles[sel], "current_swap", None)
                skill_txt = f"🎩 魔术师　|　今夜换牌：{str(_sw[0]) + '号 ↔ ' + str(_sw[1]) + '号' if _sw else '无交换'}"
            elif role == "狼美人":
                _ct = engine.roles[sel].charm_target
                skill_txt = f"💋 狼队座位：{engine.wolves}　|　当前魅惑：{(str(_ct) + '号') if _ct else '无'}（她死则殉情）"
            elif role == "白狼王":
                skill_txt = f"狼队座位：{engine.wolves}　|　👑 白天可自爆带走一人"
            elif role == "狼人":
                skill_txt = f"狼队座位：{engine.wolves}"
            else:
                skill_txt = "平民：无特殊技能（只有一张投票牌）"
            st.caption(skill_txt)

            st.markdown("**🧠 内心 OS 时间线（reasoning）**")
            with st.container(height=320):
                logs = engine.reasoning_log[sel]
                if not logs:
                    html = '<div style="opacity:.6">暂无记录。</div>'
                else:
                    html = ""
                    visible_logs = logs
                    omitted = 0
                    if not show_full_history:
                        limit = int(getattr(config, "REASONING_RENDER_LIMIT", 36))
                        omitted = max(0, len(logs) - limit)
                        visible_logs = logs[-limit:]
                    if omitted:
                        html += (f'<div style="opacity:.62;font-size:12px;margin-bottom:8px;">'
                                 f'已省略更早的 {omitted} 条界面渲染，完整内容保存在对局归档中。</div>')
                    for r in reversed(visible_logs):
                        html += (
                            '<div style="border-left:3px solid #bf5af2;padding:8px 12px;margin:8px 0;'
                            'background:rgba(191,90,242,0.06);border-radius:8px;box-shadow:0 2px 8px rgba(0,0,0,0.15);">'
                            f'<div style="display:flex;justify-content:space-between;margin-bottom:4px;">'
                            f'<span style="color:#bf5af2;font-size:11px;font-weight:700;background:rgba(191,90,242,0.15);padding:1px 6px;border-radius:4px;">{r["label"]}</span>'
                            f'</div>'
                            f'<div style="color:#ff9f0a;font-size:12px;line-height:1.45;margin-top:4px;"><b>🧠 内心 OS：</b>{r["reasoning"]}</div>'
                            f'<div style="color:var(--text-color);opacity:.9;font-size:12px;line-height:1.45;margin-top:4px;border-top:1px dashed rgba(255,255,255,0.08);padding-top:4px;"><b>🗣️ 明面行动：</b>{r["speech"]}</div>'
                            "</div>"
                        )
                st.markdown(html + '<span id="ccd-inner-end"></span>', unsafe_allow_html=True)
        else:
            st.caption("开始游戏后，这里显示该玩家的真实身份、技能状态与内心 OS。")

    with tab_duel:
        st.markdown("**⚔️ 预言家 / 焦点对决分屏**")
        if engine:
            seer_claims = [c["seat"] for c in getattr(engine, "public_claims", []) if c.get("claimed_role") == "预言家"]
            real_seer = engine.role_seats.get("预言家")
            candidates = list(dict.fromkeys(([real_seer] if real_seer else []) + seer_claims))
            if len(candidates) < 2:
                for w in engine.wolves:
                    if w not in candidates:
                        candidates.append(w)
                        if len(candidates) >= 2:
                            break
            if len(candidates) < 2:
                candidates = [1, 2]

            d_c1, d_c2 = st.columns(2)
            for idx, (col_d, c_seat) in enumerate([(d_c1, candidates[0]), (d_c2, candidates[1])]):
                with col_d:
                    cp = players[c_seat]
                    c_role = cp["role"]
                    c_camp = getattr(engine.roles.get(c_seat), "camp", "未知")
                    c_tag = "🔮 真预言家" if c_role == "预言家" else ("🐺 悍跳狼" if c_camp == "狼人" else f"🎭 {c_role}")
                    st.markdown(
                        f"<div style='border:1px solid rgba(10,132,255,0.35);border-radius:8px;padding:6px 8px;background:rgba(10,132,255,0.06);'>"
                        f"<b>{c_seat}号</b> ({c_tag})<br>"
                        f"<span style='font-size:11px;opacity:0.8;'>状态: {'🟢 存活' if cp['alive'] else '💀 出局'}</span>"
                        f"</div>",
                        unsafe_allow_html=True,
                    )
                    c_logs = engine.reasoning_log[c_seat]
                    if c_logs:
                        latest = c_logs[-1]
                        st.markdown(
                            f"<div style='font-size:11px;margin-top:6px;padding:6px;background:rgba(255,159,10,0.08);border-radius:6px;border-left:2px solid #ff9f0a;'>"
                            f"<b style='color:#ff9f0a;'>最新思维链 ({latest['label']}):</b><br>{latest['reasoning'][:180]}..."
                            f"</div>"
                            f"<div style='font-size:11px;margin-top:4px;padding:6px;background:rgba(10,132,255,0.08);border-radius:6px;border-left:2px solid #0a84ff;'>"
                            f"<b style='color:#0a84ff;'>最新发言:</b><br>{latest['speech'][:100]}"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
                    else:
                        st.caption("暂无记录")
        else:
            st.caption("游戏开始后，自动并排对比对跳双方的思维链与发言。")

    with tab_matrix:
        st.markdown("**🌐 12 席位全景心声矩阵**")
        if engine:
            with st.container(height=390):
                m_cols = st.columns(3)
                for i in range(1, 13):
                    m_col = m_cols[(i - 1) % 3]
                    p_i = players[i]
                    p_alive = p_i["alive"]
                    p_logs = engine.reasoning_log[i]
                    last_os = (p_logs[-1]["reasoning"][:40] + "…") if p_logs else "（尚无记录）"
                    c_border = "rgba(48,209,88,0.45)" if p_alive else "rgba(120,120,120,0.3)"
                    with m_col:
                        st.markdown(
                            f"<div style='border:1px solid {c_border};border-radius:8px;padding:5px 7px;margin-bottom:6px;background:rgba(255,255,255,0.03);font-size:11px;'>"
                            f"<b>{i}号</b> {config.ROLE_EMOJI.get(p_i['role'],'')} <span style='opacity:0.8;'>{p_i['role']}</span> "
                            f"{'🟢' if p_alive else '💀'}<br>"
                            f"<span style='color:#ff9f0a;line-height:1.3;'>{last_os}</span>"
                            f"</div>",
                            unsafe_allow_html=True,
                        )
        else:
            st.caption("游戏开始后，展示 12 位玩家的最新心声快照。")

# ============================================================
# 自动推演驱动：只保留一个整页刷新通道，禁止 fragment 与 full rerun 交叉竞态。
#   · waiting/compress_waiting：至少间隔 1.5 秒轮询后台 Future。
#   · 可见事件：尊重界面延迟，但保留 0.6 秒下限，避免零延迟 rerun 风暴。
# advance_step 内的线程锁仍是 generator 防重入的最后防线。
# ============================================================
if engine and ss.running and not ss.done and auto:
    advanced = advance_step()
    if not advanced:
        time.sleep(AUTO_POLL_SECONDS)
    elif ss.running and not ss.error:
        time.sleep(rerun_delay(ss.get("last_event_type", ""), float(delay), turbo=is_turbo))
    if not ss.error:
        st.rerun()
