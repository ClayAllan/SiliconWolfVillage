"""Bounded, read-only review of a locally selected game archive.

The browser receives only curated public event fields, never the raw evidence stream.
"""
from __future__ import annotations

import json
from pathlib import Path

MAX_REVIEW_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_REVIEW_SUMMARY_BYTES = 2 * 1024 * 1024
MAX_REVIEW_EVENT_TAIL_BYTES = 2 * 1024 * 1024
MAX_REVIEW_EVENT_SCAN_LINES = 5000
MAX_REVIEW_EVENTS = 500
MAX_REVIEW_TEXT_CHARS = 4000
_PUBLIC_KINDS = {"player", "judge", "votechart", "win"}


def _read_json(path: Path, max_bytes: int) -> dict:
    if not path.is_file():
        raise ValueError(f"缺少档案文件：{path.name}")
    if path.stat().st_size > max_bytes:
        raise ValueError(f"{path.name} 超过读取上限")
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"{path.name} 无法读取") from error
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} 格式不正确")
    return value


def _bounded_text(value, limit=MAX_REVIEW_TEXT_CHARS) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…（已截断）"


def _seat(value, *, allow_zero=False):
    if type(value) is not int:
        return None
    return value if (0 if allow_zero else 1) <= value <= 12 else None


def _public_event(entry):
    """Return an allowlisted projection; discard all nested/internal fields."""
    if not isinstance(entry, dict) or entry.get("kind") not in _PUBLIC_KINDS:
        return None
    kind = entry["kind"]
    row = {
        "kind": kind,
        "phase": _bounded_text(entry.get("phase", ""), 100),
        "seat": _seat(entry.get("seat")),
    }
    if kind == "votechart":
        votes = entry.get("votes") if isinstance(entry.get("votes"), dict) else {}
        totals = entry.get("totals") if isinstance(entry.get("totals"), dict) else {}
        row["title"] = _bounded_text(entry.get("title", "票型记录"), 200)
        row["votes"] = {
            str(seat): target for seat, target in votes.items()
            if str(seat).isdigit() and 1 <= int(seat) <= 12 and _seat(target, allow_zero=True) is not None
        }
        row["totals"] = {
            str(seat): value for seat, value in totals.items()
            if str(seat).isdigit() and 1 <= int(seat) <= 12
            and type(value) in (int, float) and 0 <= value <= 1000
        }
        row["winner"] = _seat(entry.get("winner"))
        row["sheriff"] = _seat(entry.get("sheriff"))
    else:
        row["text"] = _bounded_text(entry.get("text", ""))
    return row


def _read_public_event_tail(path: Path):
    if not path.is_file():
        return [], False, "档案没有 events.jsonl，无法展示事件时间线。"
    size = path.stat().st_size
    truncated = size > MAX_REVIEW_EVENT_TAIL_BYTES
    with path.open("rb") as handle:
        if truncated:
            handle.seek(size - MAX_REVIEW_EVENT_TAIL_BYTES)
        raw = handle.read(MAX_REVIEW_EVENT_TAIL_BYTES + 1)
    if truncated:
        first_newline = raw.find(b"\n")
        raw = raw[first_newline + 1:] if first_newline >= 0 else b""
    try:
        lines = raw.decode("utf-8-sig", errors="replace").splitlines()
    except UnicodeError:
        return [], True, "事件文件编码异常，未展示事件时间线。"
    if len(lines) > MAX_REVIEW_EVENT_SCAN_LINES:
        lines = lines[-MAX_REVIEW_EVENT_SCAN_LINES:]
        truncated = True

    events = []
    malformed = False
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(item, dict) or item.get("event") != "public_log":
            continue
        public = _public_event(item.get("entry"))
        if public is None:
            continue
        seq = item.get("seq")
        public["seq"] = seq if type(seq) is int and seq > 0 else None
        public["timestamp"] = _bounded_text(item.get("timestamp", ""), 80)
        events.append(public)
    if len(events) > MAX_REVIEW_EVENTS:
        events = events[-MAX_REVIEW_EVENTS:]
        truncated = True
    issue = ""
    if malformed:
        issue = "部分事件行损坏，已跳过无法解析的行。"
    if truncated:
        issue = (issue + " " if issue else "") + "仅读取最近的有界事件窗口，较早事件未载入。"
    return events, truncated, issue


def load_archive_review(root, directory) -> dict:
    """Load a local archive with path confinement and strict per-file read bounds."""
    root_path = Path(root).resolve()
    archive_path = Path(directory).resolve()
    if archive_path.parent != root_path or archive_path == root_path:
        raise ValueError("只能读取本会话档案目录下的直接子目录")
    if not archive_path.is_dir():
        raise ValueError("档案目录不存在")

    manifest = _read_json(archive_path / "manifest.json", MAX_REVIEW_MANIFEST_BYTES)
    metadata_raw = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    api_raw = manifest.get("api_quality") if isinstance(manifest.get("api_quality"), dict) else {}
    analysis_raw = manifest.get("analysis") if isinstance(manifest.get("analysis"), dict) else {}

    summary_path = archive_path / "summary.md"
    summary = ""
    if summary_path.is_file():
        if summary_path.stat().st_size > MAX_REVIEW_SUMMARY_BYTES:
            raise ValueError("summary.md 超过读取上限")
        summary = summary_path.read_text(encoding="utf-8-sig")
    else:
        summary = "（该档案没有 summary.md）"

    observed = {}
    raw_observed = analysis_raw.get("observed_metrics")
    if isinstance(raw_observed, dict):
        for key, metric in list(raw_observed.items())[:32]:
            if not isinstance(metric, dict):
                continue
            value = metric.get("value")
            if value is not None and type(value) not in (int, float):
                value = None
            observed[str(key)[:80]] = {
                "label": _bounded_text(metric.get("label", key), 100),
                "value": value,
                "numerator": metric.get("numerator") if type(metric.get("numerator")) is int else None,
                "denominator": metric.get("denominator") if type(metric.get("denominator")) is int else None,
                "definition": _bounded_text(metric.get("definition", ""), 400),
            }

    metadata = {
        key: _bounded_text(metadata_raw.get(key, ""), 200)
        for key in ("game_id", "started_at", "archived_at", "status", "winner", "board_name", "good_model", "wolf_model")
    }
    day = metadata_raw.get("day")
    metadata["day"] = day if type(day) is int and 0 <= day <= 9999 else None
    api_quality = {}
    for key in ("requests", "success", "fallback", "canceled", "prompt_tokens", "completion_tokens", "total_tokens"):
        value = api_raw.get(key)
        api_quality[key] = value if type(value) is int and value >= 0 else 0
    fallback_rate = api_raw.get("fallback_rate")
    api_quality["fallback_rate"] = fallback_rate if type(fallback_rate) in (int, float) and 0 <= fallback_rate <= 1 else None

    events, truncated, issue = _read_public_event_tail(archive_path / "events.jsonl")
    return {
        "metadata": metadata,
        "api_quality": api_quality,
        "observed_metrics": observed,
        "prompt_tuning_flags": [
            _bounded_text(flag, 400) for flag in analysis_raw.get("prompt_tuning_flags", [])[:20]
        ] if isinstance(analysis_raw.get("prompt_tuning_flags"), list) else [],
        "summary": summary,
        "events": events,
        "events_truncated": truncated,
        "issue": issue,
    }
