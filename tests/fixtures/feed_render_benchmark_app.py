"""Minimal Streamlit host for measuring the production feed component in Chromium."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from benchmarks.feed_render_fixture import (
    build_feed_rows,
    empty_feed_html,
    render_feed_entry,
)
from ui import ui_design
from ui.feed_ui import render_feed


st.set_page_config(page_title="SWV Feed Render Benchmark", layout="wide")


@st.cache_resource(show_spinner=False)
def cached_feed_rows(event_count: int):
    """Share read-only synthetic rows across browser sessions and feed modes."""
    started = time.perf_counter()
    rows = build_feed_rows(
        event_count,
        game_id=f"swv-feed-render-{event_count}",
    )
    build_ms = (time.perf_counter() - started) * 1000.0
    return rows, round(build_ms, 3)


params = st.query_params
try:
    event_count = int(params.get("events", "1000"))
except (TypeError, ValueError):
    event_count = 1000
event_count = max(1, min(15_000, event_count))
mode = str(params.get("mode", "bounded"))
if mode not in {"bounded", "full"}:
    mode = "bounded"
full_history = mode == "full"
game_id = f"swv-feed-render-{event_count}"

st.markdown(ui_design.stylesheet(), unsafe_allow_html=True)
st.title("硅基狼村 · Feed 浏览器渲染基准")
st.caption("合成数据 / 不调用模型 / 使用生产 render_feed 与事件 HTML")

fixture_access_started = time.perf_counter()
rows, fixture_build_ms = cached_feed_rows(event_count)
fixture_access_ms = (time.perf_counter() - fixture_access_started) * 1000.0
engine = SimpleNamespace(game_id=game_id, public_log=rows)

st.session_state.viewer_mode = "全知视角"
st.session_state.viewer_seat = 1

render_metrics = {"rows": 0, "html_bytes": 0}


def measured_render_entry(row):
    rendered = render_feed_entry(row)
    render_metrics["rows"] += 1
    render_metrics["html_bytes"] += len(rendered.encode("utf-8"))
    return rendered


render_started = time.perf_counter()
feed_identity = render_feed(
    st,
    engine,
    measured_render_entry,
    empty_feed_html(),
    show_god=True,
    full_history=full_history,
    limit=90,
)
python_render_ms = (time.perf_counter() - render_started) * 1000.0

ready_marker = f"SWV_FEED_RENDER_READY::{event_count}::{mode}"
metrics = {
    "events_in_history": event_count,
    "mode": mode,
    "expected_visible_rows": event_count if full_history else min(event_count, 90),
    "rendered_rows": render_metrics["rows"],
    "rendered_html_bytes": render_metrics["html_bytes"],
    "fixture_build_ms": fixture_build_ms,
    "fixture_access_ms": round(fixture_access_ms, 3),
    "python_render_ms": round(python_render_ms, 3),
    "feed_identity": feed_identity,
}
st.markdown(
    f'<div id="swv-feed-benchmark-ready">{ready_marker}</div>',
    unsafe_allow_html=True,
)
st.markdown(
    f'<div id="swv-feed-benchmark-metrics">{json.dumps(metrics, ensure_ascii=False)}</div>',
    unsafe_allow_html=True,
)
