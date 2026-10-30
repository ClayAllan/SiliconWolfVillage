#!/usr/bin/env python3
"""Reproducible, offline baseline for game snapshot and spectator-view costs.

Run:
    python benchmarks/snapshot_baseline.py --output-dir benchmark-results

Uses deterministic synthetic history, no paid API, no application changes.
Numbers are machine-specific diagnostic measurements, not fixed CI thresholds.
"""
from __future__ import annotations

import argparse
import gc
import json
import math
import os
from pathlib import Path
import platform
import random
import statistics
import sys
import tempfile
import time
import tracemalloc
from unittest.mock import patch

# Invoked as "python benchmarks/snapshot_baseline.py" from the repository root.
# Ensure project modules resolve independently of runner PYTHONPATH.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine import config
from engine.game_engine import GameEngine
from service.game_service import GameService
from service.live_feed import select_feed_window
from service.view_projection import project_view


BASE_TEXT = "第2天公开局势讨论：发言者分别辨析警徽流向、反向金水、票型和夜间信息。" * 4
REASONING_TEXT = "结合已公开票型与发言，检验悍跳、代跳、警徽交接及阵营一致性。" * 8


class OfflineManager:
    """Only implements methods called by setup and snapshot; never contacts a model."""

    def __init__(self):
        self.memory: dict[int, list] = {}

    def clear(self):
        self.memory.clear()

    def init_agent(self, seat, role, style, private_memory, roster=None):
        self.memory[seat] = []

    def has_pending(self):
        return False

    def cancel_all_pending(self, reason=""):
        return None

    def get_usage_snapshot(self):
        return {"total_tokens": 0, "api_returns": 0}

    def get_health_snapshot(self):
        return {}


def fixture(history_rows: int) -> tuple[GameEngine, int]:
    """Fixed content distribution: N public events and N//3 reasoning records."""
    engine = GameEngine(OfflineManager())
    engine.random_seed = 20261030
    engine.rng = random.Random(engine.random_seed)
    engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
    engine.game_id = "benchmark-synthetic-not-a-real-game"
    good_seat = next(s for s in range(1, 13) if engine.roles[s].camp != "狼人")
    engine.human_seat = good_seat
    engine.day = max(1, history_rows // 120)
    engine.phase = f"第{engine.day}天"
    engine.current_speaker = 2
    engine.public_log = []
    engine.reasoning_log = {s: [] for s in range(1, 13)}
    kinds = ("judge", "player", "god", "player")
    for index in range(history_rows):
        seat = index % 12 + 1
        engine.public_log.append({
            "kind": kinds[index % len(kinds)],
            "game_id": engine.game_id,
            "event_seq": index + 1,
            "phase": engine.phase,
            "seat": seat,
            "role": engine.players[seat]["role"],
            "text": f"[{index:05d}] {BASE_TEXT}",
        })
        if index % 3 == 0:
            engine.reasoning_log[seat].append({
                "label": f"D{engine.day} 思考",
                "reasoning": f"[{index:05d}] {REASONING_TEXT}",
                "speech": BASE_TEXT,
                "actor_source": "ai",
                "response": {"reasoning": REASONING_TEXT, "speech": BASE_TEXT,
                             "decision_source": "model", "vote_target": 0},
            })
    return engine, good_seat


def stats(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[math.ceil(0.95 * len(ordered)) - 1], 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "samples": len(ordered),
    }


def measure(callback, repeats: int) -> dict:
    for _ in range(2):
        callback()
    gc.collect()
    samples = []
    for _ in range(repeats):
        start = time.perf_counter_ns()
        callback()
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    return stats(samples)


def peak_allocated_mib(callback) -> float:
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        value = callback()
        _current, peak = tracemalloc.get_traced_memory()
        # Keep the returned value alive until the peak is sampled.
        assert value is not None
        return round(peak / (1024 * 1024), 3)
    finally:
        tracemalloc.stop()


def project_and_feed(service: GameService, human_seat: int | None,
                     full_history: bool = False):
    snapshot = service.snapshot()
    view = project_view(snapshot.engine, viewer_seat=human_seat)
    state = {"feed_follow_latest": True, "_feed_seen_seq": 0}
    feed = select_feed_window(
        state, view.public_log, show_god=human_seat is None,
        channel="全部动态", full_history=full_history, limit=90,
    )
    return view, feed


def run_case(history_rows: int, repeats: int) -> dict:
    engine, human_seat = fixture(history_rows)
    with tempfile.TemporaryDirectory(prefix="swv-snapshot-") as directory:
        service = GameService(engine, directory, automatic=False)
        try:
            snapshot = service.snapshot()
            full_view = project_view(snapshot.engine)
            human_view = project_view(snapshot.engine, viewer_seat=human_seat)
            assert len(full_view.public_log) == history_rows
            assert len(human_view.public_log) <= history_rows
            assert all(
                not rows for seat, rows in human_view.reasoning_log.items()
                if seat != human_seat
            ), "Restricted view leaked another seat's reasoning"
            assert all(
                player["role"] == "未知" for seat, player in human_view.players.items()
                if seat != human_seat
            ), "Restricted view leaked identities"

            def bounded_feed():
                return select_feed_window(
                    {"feed_follow_latest": True, "_feed_seen_seq": 0},
                    full_view.public_log, show_god=True,
                    channel="全部动态", full_history=False, limit=90,
                )

            def all_feed():
                return select_feed_window(
                    {"feed_follow_latest": True, "_feed_seen_seq": 0},
                    full_view.public_log, show_god=True,
                    channel="全部动态", full_history=True, limit=90,
                )

            case = {
                "public_rows": history_rows,
                "reasoning_rows": sum(len(x) for x in engine.reasoning_log.values()),
                "snapshot": measure(service.snapshot, repeats),
                "projection_full": measure(lambda: project_view(snapshot.engine), repeats),
                "projection_human": measure(
                    lambda: project_view(snapshot.engine, viewer_seat=human_seat), repeats,
                ),
                "feed_bounded": measure(bounded_feed, repeats),
                "feed_full_history": measure(all_feed, repeats),
                "pipeline_full": measure(
                    lambda: project_and_feed(service, None), repeats,
                ),
                "pipeline_human": measure(
                    lambda: project_and_feed(service, human_seat), repeats,
                ),
            }
            case["peak_allocated_mib"] = {
                "snapshot": peak_allocated_mib(service.snapshot),
                "pipeline_full": peak_allocated_mib(
                    lambda: project_and_feed(service, None)
                ),
                "pipeline_human": peak_allocated_mib(
                    lambda: project_and_feed(service, human_seat)
                ),
            }
            return case
        finally:
            with patch("service.game_service.archive_game", return_value=None):
                service.close()


def pulse_generator(engine):
    while True:
        engine.day += 1
        yield {"type": "night", "status": "synthetic progress"}


def worker_contention(public_rows: int, duration: float, poll_interval: float) -> dict:
    """Compare worker progress alone vs during snapshot polling under the service lock.

    This is a synthetic scheduling stressor, not an end-to-end browser FPS test.
    """
    engine, _ = fixture(public_rows)
    # Worker stress simulates an all-AI session. AI-authored synthetic records
    # on a human seat would correctly trip the production integrity guard.
    engine.human_seat = None
    # Preserve the fixture state while driving the real service lock and worker.
    with tempfile.TemporaryDirectory(prefix="swv-snapshot-worker-") as directory:
        service = GameService(engine, directory, automatic=True, turbo=True, delay=0,
                              generator=pulse_generator(engine))
        try:
            with patch("service.game_service.rerun_delay", return_value=0.005):
                service.play()
                time.sleep(0.15)
                baseline_start = engine.day
                t0 = time.perf_counter()
                time.sleep(duration)
                baseline_seconds = time.perf_counter() - t0
                baseline_steps = engine.day - baseline_start

                loaded_start = engine.day
                t1 = time.perf_counter()
                latencies = []
                while time.perf_counter() - t1 < duration:
                    cycle = time.perf_counter()
                    service.snapshot()
                    latencies.append((time.perf_counter() - cycle) * 1000.0)
                    remaining = poll_interval - (time.perf_counter() - cycle)
                    if remaining > 0:
                        time.sleep(remaining)
                loaded_seconds = time.perf_counter() - t1
                loaded_steps = engine.day - loaded_start

                if baseline_steps == 0 or loaded_steps == 0:
                    diagnosis = service.snapshot()
                    raise RuntimeError(
                        "Synthetic worker did not make progress; invalid stress run: "
                        f"baseline={baseline_steps}, loaded={loaded_steps}, "
                        f"event={diagnosis.last_event_type}, error={diagnosis.error}"
                    )
                baseline_rate = baseline_steps / baseline_seconds
                loaded_rate = loaded_steps / loaded_seconds
                return {
                    "public_rows": public_rows,
                    "requested_poll_interval_ms": round(poll_interval * 1000, 2),
                    "baseline_steps": baseline_steps,
                    "baseline_seconds": round(baseline_seconds, 3),
                    "loaded_steps": loaded_steps,
                    "loaded_seconds": round(loaded_seconds, 3),
                    "snapshots_during_loaded": len(latencies),
                    "baseline_steps_per_second": round(baseline_rate, 2),
                    "loaded_steps_per_second": round(loaded_rate, 2),
                    "relative_throughput_pct": round(100 * loaded_rate / baseline_rate, 1)
                    if baseline_rate else None,
                    "snapshot_latency_during_worker": stats(latencies),
                }
        finally:
            with patch("service.game_service.archive_game", return_value=None):
                service.close()


def render_markdown(payload: dict) -> str:
    lines = [
        "# SiliconWolfVillage · Snapshot performance baseline",
        "",
        f"- UTC: {payload['metadata']['timestamp_utc']}",
        f"- Python: {payload['metadata']['python']}",
        f"- Host: {payload['metadata']['platform']}; CPUs reported: {payload['metadata']['cpu_count']}",
        f"- Synthetic fixture; no paid model requests; {payload['metadata']['repeats']} "
        "timed iterations per operation, 2 warmups, median/p95 in ms.",
        "- Public rows are combined with one reasoning record per three public events, "
        "distributed among 12 seats. This is a scaling workload, not a measured "
        "distribution from a particular real game.",
        "- End-to-end pipeline here means snapshot → projection → bounded feed "
        "selection; **not** browser HTML rendering or websocket delivery.",
        "",
        "| Public events | Reasoning | Snapshot median / p95 (ms) | Full projection median (ms) | "
        "Human projection median (ms) | Full pipeline median / p95 (ms) | "
        "Human pipeline median / p95 (ms) | Peak full pipeline MiB |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for case in payload["cases"]:
        def medp(key):
            op = case[key]
            return f"{op['median_ms']:.2f} / {op['p95_ms']:.2f}"
        lines.append(
            f"| {case['public_rows']:,} | {case['reasoning_rows']:,} "
            f"| {medp('snapshot')} | {case['projection_full']['median_ms']:.2f} "
            f"| {case['projection_human']['median_ms']:.2f} | {medp('pipeline_full')} "
            f"| {medp('pipeline_human')} "
            f"| {case['peak_allocated_mib']['pipeline_full']:.2f} |"
        )
    lines.extend([
        "",
        "## Feed-only costs",
        "",
        "| Public events | Bounded feed median (ms) | Full-history feed median (ms) |",
        "|---:|---:|---:|",
    ])
    for case in payload["cases"]:
        lines.append(
            f"| {case['public_rows']:,} | {case['feed_bounded']['median_ms']:.3f} "
            f"| {case['feed_full_history']['median_ms']:.3f} |"
        )
    lines.extend([
        "",
        "## Worker progression under concurrent snapshots",
        "",
        "| Events | Worker alone (steps/s) | Worker with polling (steps/s) | "
        "Relative throughput | Polls completed |",
        "|---:|---:|---:|---:|---:|",
    ])
    for case in payload["contention"]:
        lines.append(
            f"| {case['public_rows']:,} | {case['baseline_steps_per_second']:.1f} "
            f"| {case['loaded_steps_per_second']:.1f} "
            f"| {case['relative_throughput_pct']:.1f}% | {case['snapshots_during_loaded']} |"
        )
    lines.extend([
        "",
        "## Interpretation limitations",
        "",
        "- Timing is runner-specific and may vary because GitHub Actions uses shared runners. "
        "Use medians and scaling, not universal millisecond thresholds.",
        "- Peak MiB is measured by Python tracemalloc for one call, not RSS or "
        "network payload. It excludes allocations by native extensions.",
        "- Progress throughput mixes CPU contention and serialization under the "
        "shared condition; it is not a pure lock-wait measurement.",
        "- Player view projection removes hidden data only after the service has "
        "copied the entire engine; the full pipeline intentionally measures that cost.",
        "- The test changes no production engine/service behavior.",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="100,1000,5000,15000")
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--stress-seconds", type=float, default=1.5)
    parser.add_argument("--poll-interval", type=float, default=0.05)
    args = parser.parse_args()
    sizes = [int(x) for x in args.sizes.split(",")]
    if not sizes or any(x < 10 for x in sizes) or args.repeats < 3:
        parser.error("Require sizes >= 10 and repeats >= 3")
    if args.stress_seconds <= 0 or args.poll_interval <= 0:
        parser.error("Stress duration and poll interval must be positive")

    from datetime import datetime, timezone
    payload = {
        "metadata": {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "repeats": args.repeats,
            "seed_policy": "fixed roster seed 20261030; fixed event/role distribution",
            "git_sha": os.getenv("GITHUB_SHA", "local"),
        },
        "cases": [],
        "contention": [],
    }
    for size in sizes:
        start = time.monotonic()
        case = run_case(size, args.repeats)
        payload["cases"].append(case)
        print(
            f"CASE {size:6d} events / {case['reasoning_rows']:5d} reasoning: "
            f"snapshot={case['snapshot']['median_ms']:.3f}ms, "
            f"projection(full)={case['projection_full']['median_ms']:.3f}ms, "
            f"pipeline(full)={case['pipeline_full']['median_ms']:.3f}ms, "
            f"pipeline(human)={case['pipeline_human']['median_ms']:.3f}ms, "
            f"peak(full)={case['peak_allocated_mib']['pipeline_full']:.3f} MiB, "
            f"run={time.monotonic()-start:.1f}s",
            flush=True,
        )
    for size in [sizes[0], sizes[-1]]:
        contention = worker_contention(size, args.stress_seconds, args.poll_interval)
        payload["contention"].append(contention)
        print(
            f"STRESS {size:6d} events: worker alone "
            f"{contention['baseline_steps_per_second']:.2f} steps/s; "
            f"with polling {contention['loaded_steps_per_second']:.2f} steps/s; "
            f"relative throughput {contention['relative_throughput_pct']}%; "
            f"{contention['snapshots_during_loaded']} polls",
            flush=True,
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "snapshot-baseline.json"
    markdown_path = args.output_dir / "snapshot-baseline.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print("\n" + render_markdown(payload), flush=True)
    print(f"Artifacts: {json_path}, {markdown_path}", flush=True)


if __name__ == "__main__":
    main()
