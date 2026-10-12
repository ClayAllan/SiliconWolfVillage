# -*- coding: utf-8 -*-
"""Streamlit 自动推演的节流与推演速率策略，便于独立回归测试。"""

AUTO_POLL_SECONDS = 1.5
VISIBLE_RERUN_FLOOR_SECONDS = 0.6
TURBO_FLOOR_SECONDS = 0.05
WAITING_EVENT_TYPES = frozenset({"waiting", "compress_waiting"})

SPEED_PRESETS = {
    "0.5x 慢速品读": 1.5,
    "1.0x 标准推演": 0.6,
    "2.0x 快速推演": 0.25,
    "Turbo 极速模式": 0.0,
}


def rerun_delay(event_type: str, configured_delay: float, turbo: bool = False) -> float:
    """统一计算整页刷新间隔，避免等待轮询或零延迟事件形成 rerun 风暴。"""
    delay = max(0.0, float(configured_delay))
    if event_type in WAITING_EVENT_TYPES:
        return max(AUTO_POLL_SECONDS, delay)
    if turbo:
        return TURBO_FLOOR_SECONDS
    return max(VISIBLE_RERUN_FLOOR_SECONDS, delay)
