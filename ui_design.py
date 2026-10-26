"""观战工作台的视觉与纯 HTML 视图，不推进引擎、不调用模型。"""
import math
from html import escape

import config
from ui_theme import theme_variables


def text(value):
    return escape(str(value), quote=True)


def icon(name="moon", size=22):
    paths = {
        "moon": '<path d="M20.8 13A9 9 0 0 1 11 3.2 9 9 0 1 0 20.8 13Z"/>',
        "wolf": '<path d="m4 3 6 4h4l6-4-1 12-7 6-7-6Z"/><path d="m8 12 2 1m6-1-2 1m-4 3 2 2 2-2"/>',
        "shield": '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
        "star": '<path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1L3.2 9.4l6.1-.9Z"/>',
        "person": '<circle cx="12" cy="8" r="4"/><path d="M5 21v-3a7 7 0 0 1 14 0v3"/>',
        "eye": '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
    }
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="currentColor" stroke-width="1.45" stroke-linecap="round" '
            f'stroke-linejoin="round" aria-hidden="true">{paths.get(name, paths["moon"])}</svg>')


STYLE = """
<style>
/* 原生组件的颜色交给 Streamlit ThemeProvider。这里只控制布局。 */
[data-testid="stMainBlockContainer"]{max-width:1550px;padding:2.7rem 2.2rem 2rem}
[data-testid="stVerticalBlock"]{gap:1rem}
[data-testid="stSidebarUserContent"]{padding-top:1.5rem}
[data-testid="stSidebar"] h2{font-size:20px!important;letter-spacing:.05em}
[data-testid="stSidebar"] [data-testid="stVerticalBlock"]{gap:.8rem}
[data-testid="stWidgetLabel"] p{font-size:14px}
[data-testid="stCaptionContainer"]{font-size:14px;opacity:1}
.stButton button,.stDownloadButton button{border-radius:8px;min-height:38px;font-size:14px}
.stButton button p,.stDownloadButton button p{font-size:14px;white-space:nowrap}
/* 原生主按钮默认白字；深色主题的浅金底需要深字。只覆盖主按钮前景。 */
.stButton button[kind="primary"]{color:var(--on-accent)}
.stButton button[kind="primary"] p{color:inherit}
.stButton button[kind="primary"]:hover{background:var(--accent-fill);color:var(--on-accent)}
[data-testid="stMetric"]{border:1px solid var(--line);padding:12px;border-radius:8px}
[data-testid="stMetricValue"]{font-size:25px}
[data-testid="stTabs"] [data-baseweb="tab-list"]{gap:24px}
[class^="wv-"],[class*=" wv-"]{font-family:Inter,"Segoe UI","Microsoft YaHei",sans-serif}
.wv-brand{display:flex;gap:12px;align-items:center}
.wv-emblem{display:grid;place-items:center;width:42px;height:46px;border:1px solid var(--line);border-radius:12px;color:var(--gold);background:var(--surface2)}
.wv-brand-name{font-size:25px;font-weight:650;letter-spacing:.12em;line-height:1.4}
.wv-eyebrow{color:var(--muted);font-size:11px;letter-spacing:.2em;text-transform:uppercase;margin-top:2px}
.wv-header{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:3px 0 20px;border-bottom:1px solid var(--line)}
.wv-header-right{text-align:right;color:var(--muted);font-size:13px;line-height:2}
.wv-badge{display:inline-flex;gap:7px;align-items:center;border:1px solid var(--line);border-radius:20px;padding:3px 11px;color:var(--body);font-size:13px;white-space:nowrap}
.wv-badge:before{content:"";height:5px;width:5px;background:var(--muted);border-radius:50%}
.wv-badge.live{color:var(--positive);border-color:var(--line)}.wv-badge.live:before{background:var(--positive)}
.wv-badge.demo{color:var(--gold);border-color:var(--line)}.wv-badge.demo:before{background:var(--gold)}
.wv-intro{display:flex;justify-content:space-between;align-items:end;margin:5px 0 14px}
.wv-intro h1{font-size:24px;font-weight:500;letter-spacing:.03em;margin:0;padding:0}
.wv-intro p{color:var(--muted);font-size:14px;margin:7px 0 0;line-height:1.7}
.wv-overview{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);border-radius:12px;overflow:hidden;background:var(--surface)}
.wv-stat{padding:16px 20px;border-right:1px solid var(--line)}.wv-stat:last-child{border:0}
.wv-stat-label{font-size:12px;letter-spacing:.08em;color:var(--muted);margin-bottom:7px}
.wv-stat-value{font-family:"Segoe UI",sans-serif;font-size:23px;line-height:1.3;color:var(--ink);font-weight:500}
.wv-stat-value small{font-size:14px;color:var(--muted);margin-left:5px}.wv-stat-sub{color:var(--muted);font-size:12px;margin-top:5px;overflow-wrap:anywhere}
.wv-section{display:flex;justify-content:space-between;align-items:center;margin:7px 0 8px;font-size:14px;font-weight:600;letter-spacing:.04em}
.wv-section small{color:var(--muted);font-size:12px;font-weight:400;letter-spacing:.02em}
.wv-stage{position:relative;width:100%;aspect-ratio:1.15;min-height:480px;border:1px solid var(--line);border-radius:14px;background:radial-gradient(ellipse at 50% 47%,var(--stage1) 0,var(--stage2) 34%,var(--stage3) 65%,var(--stage4) 100%);overflow:hidden;isolation:isolate}
.wv-stage:before{content:"";position:absolute;inset:15% 13%;border:1px solid #6b665646;border-radius:50%;box-shadow:0 0 0 11px #c6b27a05,0 0 0 12px #c6b27a13,0 0 0 30px #c6b27a04}
.wv-stage:after{content:"";position:absolute;inset:21% 19%;border:1px dashed #6c7c8c2b;border-radius:50%;z-index:-1}
.wv-stage-top{position:absolute;top:17px;left:20px;font-size:11px;letter-spacing:.17em;color:var(--muted)}
.wv-stage-mode{position:absolute;top:15px;right:17px;color:var(--muted);font-size:12px;display:flex;gap:5px;align-items:center}
.wv-seat{position:absolute;transform:translate(-50%,-50%);width:68px;text-align:center;z-index:2}
.wv-avatar{position:relative;display:grid;place-items:center;width:53px;height:57px;margin:auto;background:var(--avatar);border:1px solid var(--line);border-radius:13px;color:var(--body);box-shadow:0 3px 14px #0003}
.wv-seat.wolf .wv-avatar{background:var(--wolf-bg);border-color:var(--line);color:var(--wolf)}.wv-seat.god .wv-avatar{background:var(--god-bg);border-color:var(--line);color:var(--god)}
.wv-seat.selected .wv-avatar{outline:2px solid var(--gold);outline-offset:4px}
.wv-seat.speaking .wv-avatar{background:var(--thinking-bg);border-color:var(--thinking);color:var(--thinking);box-shadow:0 0 22px #d9b77b30}
.wv-seat.dead .wv-avatar{filter:grayscale(1) opacity(0.38);border-color:rgba(120,120,120,0.3)}
.wv-seat.dead:after{content:"💀";position:absolute;top:14px;left:50%;transform:translateX(-50%);font-size:22px;z-index:5;text-shadow:0 0 8px rgba(0,0,0,0.9);pointer-events:none}
.wv-seat.dead .wv-seat-role{text-decoration:line-through;opacity:.55}
.wv-seat.dead .wv-seat-state{color:#ff453a!important;font-weight:700}
.wv-pill{display:inline-flex;align-items:center;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600}
.wv-pill.alive{background:rgba(48,209,88,0.15);color:#30d158;border:1px solid rgba(48,209,88,0.3)}
.wv-pill.dead{background:rgba(255,69,58,0.15);color:#ff453a;border:1px solid rgba(255,69,58,0.3)}
.wv-seat-num{position:absolute;top:-7px;left:-6px;background:var(--surface2);border:1px solid var(--line);color:var(--muted);width:20px;height:20px;line-height:19px;font-size:11px;border-radius:6px}
.wv-seat-role{font-size:12px;color:var(--body);margin-top:7px;white-space:nowrap}
.wv-seat-state{font-size:10px;color:var(--muted);margin-top:2px;white-space:nowrap}.speaking .wv-seat-state{color:var(--gold)}
.wv-sheriff{position:absolute;right:-7px;top:-8px;color:var(--gold);font-size:14px;background:var(--surface2);border:1px solid var(--line);border-radius:6px;padding:0 4px}
.wv-stage-center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:38%;text-align:center}
.wv-stage-center .wv-moon{color:var(--body);opacity:.8;margin-bottom:9px}.wv-stage-center h2{font-size:22px;font-weight:400;letter-spacing:.1em;padding:0;margin:8px 0 12px;color:var(--gold)}
.wv-stage-center p{font-size:13px;line-height:1.9;color:var(--body);margin:0;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:5;-webkit-box-orient:vertical;overflow:hidden}
.wv-stage-center .wv-eyebrow{font-size:10px;color:var(--body);line-height:1.8}
.wv-stage-bottom{position:absolute;bottom:15px;left:0;width:100%;text-align:center;font-size:11px;letter-spacing:.1em;color:var(--muted)}
.wv-factions{display:flex;gap:18px;padding:14px 4px;flex-wrap:wrap}.wv-faction{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--body)}.wv-dot{height:6px;width:6px;border-radius:50%;background:var(--muted)}.wv-faction b{color:var(--ink);font-weight:500;margin-left:5px}
.wv-event{padding:13px 0;border-bottom:1px solid var(--line);line-height:1.8;overflow-wrap:anywhere}
.wv-event:last-child{border:0}.wv-event-head{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted);margin-bottom:6px}
.wv-event-head b{color:var(--ink);font-size:14px;font-weight:550}.wv-event-head small{font-size:11px;color:var(--muted);margin-left:auto}
.wv-event-icon{display:grid;place-items:center;width:24px;height:24px;border-radius:7px;background:var(--surface2);color:var(--body);font-size:11px;flex-shrink:0}
.wv-event p{font-size:14px;color:var(--body);margin:0;line-height:1.85;white-space:pre-wrap}
.wv-event.judge{background:#b5a07707;padding:12px;border-radius:7px;margin:7px 0}.wv-event.judge p{color:var(--body);font-size:13px}.wv-event.god p{color:var(--body);font-size:13px}.wv-event.win p{color:var(--gold);font-size:16px}
.wv-empty{padding:45px 25px;text-align:center;border:1px dashed var(--line);border-radius:10px;color:var(--muted);font-size:14px;line-height:2}
.wv-empty strong{display:block;color:var(--ink);font-weight:500;font-size:15px;margin:10px 0}
.wv-reasoning{padding:16px;border:1px solid var(--line);border-radius:10px;background:var(--surface2);margin:10px 0}
.wv-reasoning .wv-label{font-size:12px;color:var(--body);letter-spacing:.05em}.wv-reasoning p{font-size:14px;line-height:1.9;color:var(--body);white-space:pre-wrap;margin:8px 0}
.wv-reasoning .wv-action{border-top:1px solid var(--line);padding-top:10px;color:var(--positive)}
.wv-player{border:1px solid var(--line);border-radius:10px;padding:14px 17px;margin:10px 0;background:var(--surface2);position:relative}
.wv-player b{color:var(--gold);font-size:16px;font-weight:500}
.wv-player p{font-size:13px;color:var(--muted);margin:7px 0 0}
.wv-player.dead{opacity:.58;border-color:rgba(255,69,58,0.25)}
.wv-player.dead:after{content:"OUT";position:absolute;top:12px;right:14px;font-size:11px;font-weight:700;color:#ff453a;border:1px solid rgba(255,69,58,0.4);border-radius:4px;padding:0 5px;background:rgba(255,69,58,0.08)}
.wv-matrix{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}.wv-matrix .wv-player{margin:0}.wv-matrix p{line-height:1.8}
.wv-footer{margin-top:22px;padding-top:16px;border-top:1px solid var(--line);display:flex;justify-content:space-between;font-size:11px;color:var(--muted);letter-spacing:.06em}
.st-key-playback [data-testid="stHorizontalBlock"]{gap:.5rem;flex-wrap:wrap}.st-key-playback [data-testid="stColumn"]{min-width:116px!important;flex:1 1 116px!important}
.st-key-workspace [data-testid="stHorizontalBlock"]{align-items:start}
@media(max-width:1100px){[data-testid="stMainBlockContainer"]{padding:2.5rem 1rem 1.5rem}.st-key-workspace>[data-testid="stLayoutWrapper"]>[data-testid="stHorizontalBlock"]{flex-wrap:wrap}.st-key-workspace>[data-testid="stLayoutWrapper"]>[data-testid="stHorizontalBlock"]>[data-testid="stColumn"]{flex:1 1 100%!important;width:100%!important;min-width:100%!important}.wv-stat{padding:13px}.wv-matrix{grid-template-columns:repeat(3,1fr)}}
@media(max-width:600px){.wv-header-right .wv-session{display:none}.wv-brand-name{font-size:21px}.wv-header{padding-bottom:14px}.wv-overview{grid-template-columns:repeat(2,1fr)}.wv-stat:nth-child(2){border-right:0}.wv-stat:nth-child(-n+2){border-bottom:1px solid var(--line)}.wv-stage{min-height:480px}.wv-avatar{width:44px;height:48px;border-radius:11px}.wv-seat-role{font-size:11px}.wv-seat{width:55px}.wv-stage-center h2{font-size:18px}.wv-stage-center p{font-size:12px}.wv-stage-mode{font-size:11px}.wv-matrix{grid-template-columns:repeat(2,1fr)}.wv-intro h1{font-size:21px}.wv-footer span:last-child{display:none}}
@media(max-width:760px){.st-key-playback [data-testid="stColumn"]{flex:1 1 calc(33.333% - .5rem)!important;min-width:0!important}}
@media(max-width:430px){.st-key-playback [data-testid="stColumn"]{flex:1 1 calc(50% - .5rem)!important}}
@media(prefers-reduced-motion:reduce){*,*:before,*:after{animation:none!important;transition:none!important;scroll-behavior:auto!important}}

.wv-progress{display:flex;width:100%;height:31px;border-radius:8px;overflow:hidden;background:var(--surface2);margin:13px 0;border:1px solid var(--line)}
.wv-segment{display:flex;align-items:center;justify-content:center;color:#fff;font-size:13px;font-weight:650;white-space:nowrap;overflow:hidden}
.wv-counts{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-bottom:8px}
.wv-count{padding:10px 11px;border:1px solid var(--line);border-radius:8px;background:var(--surface);font-size:12px;color:var(--body)}
.wv-count strong{display:block;color:var(--ink);font-size:22px;line-height:1.4;margin-top:3px}.wv-count small{font-size:11px;color:var(--muted)}
.wv-count-label{display:flex;align-items:center;gap:6px}.wv-count-note{font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:12px}
.st-key-view_controls [data-testid="stHorizontalBlock"]{flex-wrap:wrap;gap:1rem}.st-key-view_controls [data-testid="stColumn"]{min-width:175px!important;flex:1 1 175px!important}

</style>
"""


def stylesheet(theme="light"):
    return "<style>" + theme_variables(theme) + "</style>" + STYLE


def header(engine, *, demo=False, running=False):
    badge, cls = ("示例预览 · 零 API", "demo") if demo else (
        ("正在推演", "live") if running else ("对局结束", "") if engine and engine.winner
        else ("已暂停", "") if engine else ("本地工作台", ""))
    session = f"SESSION / {engine.game_id[:8].upper()}" if engine and not demo else "12 AGENTS · ONE VILLAGE"
    return (f'<div class="wv-header"><div class="wv-brand"><div class="wv-emblem">{icon("wolf",28)}</div>'
            '<div><div class="wv-brand-name">硅基狼村</div><div class="wv-eyebrow">Silicon Wolf Village / AI Arena</div></div></div>'
            f'<div class="wv-header-right"><span class="wv-badge {cls}">{badge}</span>'
            f'<div class="wv-session">{text(session)}</div></div></div>')


def overview(engine, usage, *, demo=False):
    alive = len(engine.alive_seats()) if engine else 12
    phase = engine.phase if engine else "等待入席"
    day = f"第 {engine.day} 天" if engine and engine.day else "准备阶段"
    values = [
        ("ROUND / 当前阶段", day, phase),
        ("PLAYERS / 场上席位", f'{alive}<small>/ 12 存活</small>', "四狼 · 四神 · 四民" if not engine else engine.board_name),
        ("SHERIFF / 当前警长", f"{engine.sheriff:02d} 号" if engine and engine.sheriff else "尚未当选", "警长投票权重 × 1.5"),
        ("USAGE / 已结算用量", "0<small>Token</small>" if demo else f'{int(usage.get("total_tokens",0)):,}<small>Token</small>', "示例数据，不调用模型" if demo else "模型响应返回后计入"),
    ]
    return '<div class="wv-overview">' + ''.join(
        f'<div class="wv-stat"><div class="wv-stat-label">{label}</div><div class="wv-stat-value">{value}</div>'
        f'<div class="wv-stat-sub">{text(sub)}</div></div>' for label, value, sub in values) + '</div>'


def stage(players, current=None, selected=1, sheriff=None, engine=None):
    seats = []
    for seat in range(1, 13):
        player = players[seat]
        role = player.get("role", "未知")
        role_class = config.ROLE_CLASSES.get(role)
        camp = "wolf" if role_class and role_class.camp == "狼人" else "god" if role in config.GOD_ROLES else "villager"
        mark = "wolf" if camp == "wolf" else "shield" if camp == "god" else "person"
        classes = " ".join(["wv-seat", camp, "selected" if seat == selected else "",
                            "speaking" if seat == current else "", "dead" if not player["alive"] else ""])
        angle = math.radians(-90 + (seat - 1) * 30)
        x, y = 50 + 39 * math.cos(angle), 50 + 36 * math.sin(angle)
        state = "已出局" if not player["alive"] else "正在思考 / 发言" if seat == current else "等待入席" if engine is None else "存活"
        badge = '<span class="wv-sheriff">♔</span>' if sheriff == seat else ""
        seats.append(f'<div class="{classes}" style="left:{x:.2f}%;top:{y:.2f}%" aria-label="{seat}号 {text(role)} {state}">'
                     f'<div class="wv-avatar">{icon(mark,28)}<span class="wv-seat-num">{seat:02d}</span>{badge}</div>'
                     f'<div class="wv-seat-role">{text(role)}</div><div class="wv-seat-state">{state}</div></div>')
    latest = next((row for row in reversed(engine.public_log) if row.get("kind") == "player"), None) if engine else None
    title = f"{current:02d} 号的回合" if current else "夜幕将至"
    phase = engine.phase if engine else "十二席位，等待一场交锋"
    speech = (latest.get("text", "")[:130] if latest and latest.get("seat") == current else
              "正在斟酌局势。行动返回后，公开发言会呈现在这里。" if current else
              "选择一套板子，让十二个 AI 在同一张圆桌上推理、伪装与交锋。")
    if engine and engine.winner:
        title, speech = f"{engine.winner}获胜", "本局已结束，可在侧栏导出完整复盘。"
    return ('<div class="wv-stage"><div class="wv-stage-top">THE VILLAGE / 12 SEATS</div>'
            f'<div class="wv-stage-mode">{icon("eye",13)} {str(engine.viewer_seat) + " 号视角" if engine and getattr(engine,"limited",False) else "全知观战"}</div>' + ''.join(seats) +
            f'<div class="wv-stage-center"><div class="wv-moon">{icon("moon",36)}</div>'
            f'<div class="wv-eyebrow">{text(phase)}</div><h2>{text(title)}</h2><p>{text(speech)}</p></div>'
            '</div>')


def factions(engine, *, reveal_counts=None):
    counts = reveal_counts if reveal_counts is not None else getattr(engine, "counts", None)
    if counts is None and not getattr(engine, "limited", False):
        from view_projection import faction_counts
        counts = faction_counts(engine)
    total = len(engine.players)
    dead = total-len(engine.alive_seats())
    colors = {"狼人":"#a24158","神职":"#246f73","平民":"#375f91","出局":"#505c6e","身份未公开":"#375f91"}
    parts = counts if counts is not None else {"身份未公开":total-dead,"出局":dead}
    segments=[]
    for label,count in parts.items():
        if count <= 0:
            continue
        width=100*count/total
        segments.append(f'<div class="wv-segment" role="progressbar" aria-label="{label}" aria-valuenow="{count}" '
                        f'aria-valuemin="0" aria-valuemax="{total}" style="width:{width:.4f}%;background:{colors[label]}">'
                        f'{label + " " if width >= 20 else ""}{count}</div>')
    cards=[]
    for label in ("狼人","神职","平民","出局"):
        value = counts[label] if counts is not None else dead if label == "出局" else "?"
        detail = "已出局" if label == "出局" else "当前存活" if counts is not None else f'开局 {engine.initial_counts.get(label,0)} 人 · 存活未知'
        cards.append(f'<div class="wv-count"><div class="wv-count-label"><i class="wv-dot" style="background:{colors[label]}"></i>{label}</div>'
                     f'<strong>{value}</strong><small>{detail}</small></div>')
    note = "按十二席位展示当前阵营存活与出局人数。" if counts is not None else "单一视角：条形只展示存活与出局；不从后台真实身份推断剩余阵营。"
    return ('<div class="wv-progress" aria-label="席位人数分布">'+"".join(segments)+'</div><div class="wv-counts">'
            +"".join(cards)+'</div><div class="wv-count-note">'+note+'</div>')


def event(row):
    kind = row.get("kind", "judge")
    label = f'{row.get("seat", "?"):02d} 号玩家' if kind == "player" and isinstance(row.get("seat"), int) else {"judge":"法官播报","god":"秘密行动 · 全知可见","win":"终局播报"}.get(kind,"对局事件")
    mark = str(row.get("seat", "•")).zfill(2) if kind == "player" else "◇"
    return (f'<article class="wv-event {text(kind)}"><div class="wv-event-head"><span class="wv-event-icon">{text(mark)}</span>'
            f'<b>{text(label)}</b><small>{text(row.get("phase", ""))}</small></div><p>{text(row.get("text", ""))}</p></article>')


def reasoning(row):
    return (f'<article class="wv-reasoning"><div class="wv-label">{text(row.get("label", "推理记录"))} / 模型自述</div>'
            f'<p>{text(row.get("reasoning", "暂无推理记录"))}</p>'
            f'<p class="wv-action">公开行动 · {text(row.get("speech", ""))}</p></article>')


def empty(title, detail):
    return f'<div class="wv-empty">{icon("moon",30)}<strong>{text(title)}</strong>{text(detail)}</div>'
