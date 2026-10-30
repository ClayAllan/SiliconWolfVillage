"""Deterministic synthetic rows for the real Streamlit feed-render benchmark.

The fixture mirrors the production public-log row shape and freezes rows at the
same boundary as GameEngine. It never constructs an AgentManager or calls a
model API.
"""
from __future__ import annotations

from storage.immutable_log import freeze_log_row
from ui import ui_design


BASE_TEXT = (
    "我结合当前票型、警徽流向和公开发言重新检查这一轮判断；"
    "目前仍保留两种解释，下一轮需要用新的公开信息交叉验证。"
)


def build_feed_rows(count: int, *, game_id: str = "swv-feed-render-benchmark"):
    """Build deterministic, recursively immutable public-event rows."""
    count = int(count)
    if not 1 <= count <= 15_000:
        raise ValueError("feed benchmark event count must be between 1 and 15000")

    kinds = ("judge", "player", "god", "player")
    rows = []
    for index in range(count):
        seat = index % 12 + 1
        kind = kinds[index % len(kinds)]
        day = index // 120 + 1
        rows.append(freeze_log_row({
            "game_id": game_id,
            "event_seq": index + 1,
            "kind": kind,
            "seat": seat,
            "role": "平民",
            "phase": f"第{day}天",
            "text": f"[{index + 1:05d}] {BASE_TEXT}",
        }))
    return rows


def render_feed_entry(row) -> str:
    """Use the production event HTML renderer, including its escaping rules."""
    return ui_design.event(row)


def empty_feed_html() -> str:
    return ui_design.empty("暂无动态", "这是浏览器渲染基准的空状态。")
