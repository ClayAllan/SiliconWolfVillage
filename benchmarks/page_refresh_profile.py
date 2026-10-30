#!/usr/bin/env python3
"""Profile the real active-game Streamlit refresh data path without a browser.

Run:
    python benchmarks/page_refresh_profile.py --output-dir benchmark-results

The harness uses the real GameService, the exact app.py human-action guard,
project_view(), and select_feed_window(). It models the current two-snapshot
rerun and compares it with the removed three-snapshot path in the same process;
it deliberately excludes Streamlit rendering,
browser transport, and paid model requests.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from benchmarks import snapshot_baseline as baseline
from benchmarks.immutable_log_poc import freeze_engine_logs
from service import game_service as service_module
from service.game_service import GameService
from service.live_feed import feed_identity, prepare_feed_state, select_feed_window
from service.view_projection import project_view


APP_PATH = ROOT / "app.py"
NORMAL_ACTIVE_REFRESH_SNAPSHOT_CALLS = 2


class _TimedCondition:
    """Transparent Condition proxy; records only the calling thread's snapshot lock."""

    def __init__(self, condition, local):
        self._condition = condition
        self._local = local

    def __enter__(self):
        started = time.perf_counter_ns()
        self._condition.acquire()
        acquired = time.perf_counter_ns()
        if getattr(self._local, "active", False):
            current = self._local.current
            current["lock_wait_ns"] += acquired - started
            self._local.lock_acquired_ns = acquired
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if getattr(self._local, "active", False):
            acquired = getattr(self._local, "lock_acquired_ns", None)
            if acquired is not None:
                self._local.current["lock_hold_ns"] += time.perf_counter_ns() - acquired
                self._local.lock_acquired_ns = None
        self._condition.release()
        return False

    def __getattr__(self, name):
        return getattr(self._condition, name)


class _TimedCopyModule:
    """Module-local proxy so only GameService.snapshot deepcopy calls are timed."""

    def __init__(self, original, local, *, profile_fields=False):
        self._original = original
        self._local = local
        self._profile_fields = profile_fields
        self._field_names_by_id = {}
        self.copy = original.copy

    def set_field_names(self, field_names_by_id):
        self._field_names_by_id = {
            object_id: list(names) for object_id, names in field_names_by_id.items()
        }

    def deepcopy(self, value, memo=None):
        if not getattr(self._local, "active", False):
            if memo is None:
                return self._original.deepcopy(value)
            return self._original.deepcopy(value, memo)

        started = time.perf_counter_ns()
        try:
            if memo is None:
                return self._original.deepcopy(value)
            return self._original.deepcopy(value, memo)
        finally:
            elapsed = time.perf_counter_ns() - started
            current = self._local.current
            current["deepcopy_ns"] += elapsed
            current["deepcopy_calls"] += 1
            if self._profile_fields:
                names = self._field_names_by_id.get(id(value))
                if names:
                    field_name = names.pop(0)
                    current["deepcopy_fields_ns"][field_name] = (
                        current["deepcopy_fields_ns"].get(field_name, 0) + elapsed
                    )

    def __getattr__(self, name):
        return getattr(self._original, name)


class SnapshotProbe:
    """Instrument a service instance without changing production source code."""

    def __init__(self, service, *, profile_fields=False):
        self.service = service
        self.profile_fields = profile_fields
        self._local = threading.local()
        self._original_condition = service._condition
        self._original_copy_module = service_module.copy
        self._timed_copy_module = _TimedCopyModule(
            self._original_copy_module, self._local, profile_fields=profile_fields
        )
        service._condition = _TimedCondition(self._original_condition, self._local)
        service_module.copy = self._timed_copy_module
        self._closed = False

    def take(self):
        field_names_by_id = {}
        if self.profile_fields:
            for field_name, value in vars(self.service._engine).items():
                if field_name in {"manager", "recorder"}:
                    continue
                field_names_by_id.setdefault(id(value), []).append(field_name)
            self._timed_copy_module.set_field_names(field_names_by_id)

        sample = {
            "total_ns": 0,
            "lock_wait_ns": 0,
            "lock_hold_ns": 0,
            "deepcopy_ns": 0,
            "deepcopy_calls": 0,
            "deepcopy_fields_ns": {},
        }
        self._local.current = sample
        self._local.active = True
        started = time.perf_counter_ns()
        try:
            snapshot = GameService.snapshot(self.service)
        finally:
            sample["total_ns"] = time.perf_counter_ns() - started
            self._local.active = False
            self._local.current = None
        field_total_ns = sum(sample["deepcopy_fields_ns"].values())
        return snapshot, {
            "total_ms": sample["total_ns"] / 1_000_000,
            "lock_wait_ms": sample["lock_wait_ns"] / 1_000_000,
            "lock_hold_ms": sample["lock_hold_ns"] / 1_000_000,
            "deepcopy_ms": sample["deepcopy_ns"] / 1_000_000,
            "deepcopy_calls": sample["deepcopy_calls"],
            "deepcopy_fields_ms": {
                name: elapsed / 1_000_000
                for name, elapsed in sample["deepcopy_fields_ns"].items()
            },
            "deepcopy_unattributed_ms": max(
                0, sample["deepcopy_ns"] - field_total_ns
            ) / 1_000_000,
        }

    def close(self):
        if self._closed:
            return
        self.service._condition = self._original_condition
        service_module.copy = self._original_copy_module
        self._closed = True


def mark_human_rows(engine, human_seat):
    """Make the synthetic human seat pass the app's real anti-AI-takeover guard."""
    for row in engine.reasoning_log.get(human_seat, []):
        row["actor_source"] = "human"
        response = row.setdefault("response", {})
        response["_human_action"] = True
        response["decision_source"] = "human"


def load_integrity_guard():
    """Compile the exact self-contained guard function from app.py, without importing Streamlit."""
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), filename=str(APP_PATH))
    function = next(
        (
            node for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_human_action_integrity_error"
        ),
        None,
    )
    if function is None:
        raise RuntimeError("app.py no longer defines _human_action_integrity_error")
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {}
    exec(compile(ast.fix_missing_locations(module), str(APP_PATH), "exec"), namespace)
    return namespace["_human_action_integrity_error"]


def _collect_sync_calls(node, *, skip_nested_definitions):
    calls = []

    def visit(current):
        if skip_nested_definitions and isinstance(
            current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
        ):
            return
        if (
            isinstance(current, ast.Call)
            and isinstance(current.func, ast.Name)
            and current.func.id == "sync_game_state"
        ):
            calls.append(current.lineno)
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return sorted(calls)


def inspect_sync_call_sites():
    tree = ast.parse(APP_PATH.read_text(encoding="utf-8"), filename=str(APP_PATH))
    all_calls = _collect_sync_calls(tree, skip_nested_definitions=False)
    top_level_calls = []
    for statement in tree.body:
        top_level_calls.extend(
            _collect_sync_calls(statement, skip_nested_definitions=True)
        )
    return sorted(all_calls), sorted(top_level_calls)


def profile_page_refresh(service, probe, human_seat, integrity_guard, *, snapshot_count=NORMAL_ACTIVE_REFRESH_SNAPSHOT_CALLS):
    """Model the active rerun with two snapshots or its legacy three-snapshot control."""
    if snapshot_count not in (2, 3):
        raise ValueError("snapshot_count must be 2 (current) or 3 (legacy control)")
    refresh_started = time.perf_counter_ns()

    snapshot_1, metrics_1 = probe.take()
    initial_engine = snapshot_1.engine
    del snapshot_1  # app.py retains the projected engine, not the GameSnapshot wrapper.
    guard_started = time.perf_counter_ns()
    guard_error = integrity_guard(initial_engine)
    guard_1_ms = (time.perf_counter_ns() - guard_started) / 1_000_000
    if guard_error:
        raise AssertionError(f"synthetic human guard failed on first sync: {guard_error}")

    configure_started = time.perf_counter_ns()
    service.configure(automatic=True, delay=0.6, turbo=False,
                      react_budget=getattr(initial_engine, "react_budget", None))
    configure_ms = (time.perf_counter_ns() - configure_started) / 1_000_000

    legacy_sidebar_metrics = []
    legacy_sidebar_guard_ms = []
    if snapshot_count == 3:
        # live_for_mode still retains initial_engine after engine/ss.engine are
        # replaced by this legacy sidebar sync, matching the old app's lifetime.
        sidebar_snapshot, sidebar_metrics = probe.take()
        sidebar_engine = sidebar_snapshot.engine
        del sidebar_snapshot
        guard_started = time.perf_counter_ns()
        guard_error = integrity_guard(sidebar_engine)
        legacy_guard_ms = (time.perf_counter_ns() - guard_started) / 1_000_000
        if guard_error:
            raise AssertionError(f"synthetic human guard failed after legacy sidebar sync: {guard_error}")
        legacy_sidebar_metrics.append(sidebar_metrics)
        legacy_sidebar_guard_ms.append(legacy_guard_ms)

    # The main-view sync occurs after sidebar actions in both variants.
    main_snapshot, main_metrics = probe.take()
    main_engine = main_snapshot.engine
    del main_snapshot
    guard_started = time.perf_counter_ns()
    guard_error = integrity_guard(main_engine)
    main_guard_ms = (time.perf_counter_ns() - guard_started) / 1_000_000
    if guard_error:
        raise AssertionError(f"synthetic human guard failed before main view: {guard_error}")

    projection_started = time.perf_counter_ns()
    view = project_view(main_engine, viewer_seat=human_seat)
    projection_ms = (time.perf_counter_ns() - projection_started) / 1_000_000

    feed_started = time.perf_counter_ns()
    channel = "全部动态"
    feed_state = {"viewer_mode": "单一视角", "viewer_seat": human_seat}
    prepare_feed_state(feed_state, (view.game_id, "单一视角", human_seat))
    identity = feed_identity(view.game_id, "单一视角", human_seat, channel)
    if feed_state.get("_feed_channel_identity") != identity:
        feed_state["_feed_channel_identity"] = identity
        feed_state["_feed_scroll_action"] = None
        feed_state["_feed_seen_seq"] = 0
        feed_state["_feed_mark_seen"] = True
    feed = select_feed_window(
        feed_state,
        view.public_log,
        show_god=False,
        channel=channel,
        full_history=False,
        limit=90,
    )
    feed_ms = (time.perf_counter_ns() - feed_started) / 1_000_000

    return {
        "total_ms": (time.perf_counter_ns() - refresh_started) / 1_000_000,
        "configure_ms": configure_ms,
        "integrity_guard_ms": [guard_1_ms, *legacy_sidebar_guard_ms, main_guard_ms],
        "projection_ms": projection_ms,
        "feed_selection_ms": feed_ms,
        "snapshots": [metrics_1, *legacy_sidebar_metrics, main_metrics],
        "visible_feed_rows": len(feed[0]),
        "unseen_rows": feed[1],
        "feed_truncated": feed[2],
    }


def _stats(values):
    return baseline.stats([float(value) for value in values])


def _field_item_count(name, value):
    if name == "reasoning_log" and isinstance(value, dict):
        return sum(len(rows or []) for rows in value.values())
    if isinstance(value, (dict, list, tuple, set, str, bytes)):
        return len(value)
    return None


def profile_snapshot_fields(public_rows, repeats):
    """Measure per-attribute deepcopy cost inside the real GameService.snapshot call."""
    engine, _human_seat = baseline.fixture(public_rows)
    field_metadata = {}
    for name, value in vars(engine).items():
        if name in {"manager", "recorder"}:
            continue
        field_metadata[name] = {
            "type": type(value).__name__,
            "items": _field_item_count(name, value),
        }

    with tempfile.TemporaryDirectory(prefix="swv-snapshot-field-profile-") as directory:
        service = GameService(engine, directory, automatic=False)
        probe = SnapshotProbe(service, profile_fields=True)
        try:
            for _ in range(2):
                snapshot, _ = probe.take()
                del snapshot

            samples = []
            for _ in range(repeats):
                snapshot, metrics = probe.take()
                del snapshot
                samples.append(metrics)

            field_values = {}
            for sample in samples:
                for name, elapsed in sample["deepcopy_fields_ms"].items():
                    field_values.setdefault(name, []).append(elapsed)
            fields = {
                name: {
                    **field_metadata[name],
                    **_stats(values),
                }
                for name, values in field_values.items()
            }
            total_field_median = sum(
                item["median_ms"] for item in fields.values()
            )
            return {
                "public_rows": public_rows,
                "snapshot_total": _stats([sample["total_ms"] for sample in samples]),
                "snapshot_deepcopy": _stats([sample["deepcopy_ms"] for sample in samples]),
                "unattributed_deepcopy": _stats([
                    sample["deepcopy_unattributed_ms"] for sample in samples
                ]),
                "field_median_sum_ms": round(total_field_median, 3),
                "fields": fields,
            }
        finally:
            probe.close()
            with patch("service.game_service.archive_game", return_value=None):
                service.close()


def _profile_view_and_feed(engine, human_seat, repeats):
    projection_samples = []
    feed_samples = []
    final_view = None
    final_feed = None
    for _ in range(2):
        project_view(engine, viewer_seat=human_seat)
    for _ in range(repeats):
        started = time.perf_counter_ns()
        view = project_view(engine, viewer_seat=human_seat)
        projection_samples.append((time.perf_counter_ns() - started) / 1_000_000)

        state = {"feed_follow_latest": True, "_feed_seen_seq": 0}
        started = time.perf_counter_ns()
        feed = select_feed_window(
            state,
            view.public_log,
            show_god=False,
            channel="全部动态",
            full_history=False,
            limit=90,
        )
        feed_samples.append((time.perf_counter_ns() - started) / 1_000_000)
        final_view, final_feed = view, feed
    return _stats(projection_samples), _stats(feed_samples), final_view, final_feed


def profile_immutable_log_snapshot(public_rows, repeats):
    """Benchmark-only POC: recursively frozen log rows may be safely shared by snapshots."""
    engine, human_seat = baseline.fixture(public_rows)
    mark_human_rows(engine, human_seat)

    with tempfile.TemporaryDirectory(prefix="swv-immutable-log-poc-") as directory:
        service = GameService(engine, directory, automatic=False)
        probe = SnapshotProbe(service, profile_fields=True)
        try:
            def collect_snapshot_samples():
                for _ in range(2):
                    snapshot, _ = probe.take()
                    del snapshot
                samples = []
                for _ in range(repeats):
                    snapshot, metrics = probe.take()
                    del snapshot
                    samples.append(metrics)
                return samples

            normal_samples = collect_snapshot_samples()
            normal_snapshot, _ = probe.take()
            normal_projection, normal_feed, normal_view, normal_feed_result = (
                _profile_view_and_feed(normal_snapshot.engine, human_seat, repeats)
            )
            normal_export = json.dumps(
                normal_snapshot.engine.export_state(),
                ensure_ascii=False,
                default=str,
            )

            # No worker is running. Replace only log rows; outer containers remain
            # ordinary mutable list/dict objects so append semantics can be preserved.
            freeze_engine_logs(service._engine)
            frozen_samples = collect_snapshot_samples()
            frozen_snapshot, _ = probe.take()
            frozen_projection, frozen_feed, frozen_view, frozen_feed_result = (
                _profile_view_and_feed(frozen_snapshot.engine, human_seat, repeats)
            )
            frozen_export = json.dumps(
                frozen_snapshot.engine.export_state(),
                ensure_ascii=False,
                default=str,
            )

            first_reasoning_seat = next(
                seat for seat, rows in service._engine.reasoning_log.items() if rows
            )
            view_parity = vars(normal_view) == vars(frozen_view)
            feed_parity = normal_feed_result == frozen_feed_result
            export_parity = json.loads(normal_export) == json.loads(frozen_export)
            shared_public_row = (
                bool(frozen_snapshot.engine.public_log)
                and frozen_snapshot.engine.public_log[0] is service._engine.public_log[0]
            )
            shared_reasoning_row = (
                frozen_snapshot.engine.reasoning_log[first_reasoning_seat][0]
                is service._engine.reasoning_log[first_reasoning_seat][0]
            )

            def field_stat(samples, name):
                return _stats([
                    sample["deepcopy_fields_ms"].get(name, 0.0)
                    for sample in samples
                ])

            return {
                "public_rows": public_rows,
                "reasoning_rows": sum(len(rows) for rows in engine.reasoning_log.values()),
                "normal_snapshot_total": _stats([
                    sample["total_ms"] for sample in normal_samples
                ]),
                "frozen_snapshot_total": _stats([
                    sample["total_ms"] for sample in frozen_samples
                ]),
                "normal_snapshot_deepcopy": _stats([
                    sample["deepcopy_ms"] for sample in normal_samples
                ]),
                "frozen_snapshot_deepcopy": _stats([
                    sample["deepcopy_ms"] for sample in frozen_samples
                ]),
                "normal_public_log_copy": field_stat(normal_samples, "public_log"),
                "frozen_public_log_copy": field_stat(frozen_samples, "public_log"),
                "normal_reasoning_log_copy": field_stat(normal_samples, "reasoning_log"),
                "frozen_reasoning_log_copy": field_stat(frozen_samples, "reasoning_log"),
                "normal_projection": normal_projection,
                "frozen_projection": frozen_projection,
                "normal_feed_selection": normal_feed,
                "frozen_feed_selection": frozen_feed,
                "view_parity": view_parity,
                "feed_parity": feed_parity,
                "export_parity": export_parity,
                "shared_public_row": shared_public_row,
                "shared_reasoning_row": shared_reasoning_row,
            }
        finally:
            probe.close()
            with patch("service.game_service.archive_game", return_value=None):
                service.close()


def profile_case(public_rows, repeats, integrity_guard):
    engine, human_seat = baseline.fixture(public_rows)
    mark_human_rows(engine, human_seat)
    with tempfile.TemporaryDirectory(prefix="swv-page-refresh-profile-") as directory:
        service = GameService(engine, directory, automatic=False)
        probe = SnapshotProbe(service)
        try:
            for _ in range(2):
                profile_page_refresh(service, probe, human_seat, integrity_guard, snapshot_count=2)
                profile_page_refresh(service, probe, human_seat, integrity_guard, snapshot_count=3)

            profiles_two = []
            profiles_three = []
            for iteration in range(repeats):
                # Alternate order to reduce systematic warm-cache / runner drift bias.
                order = (2, 3) if iteration % 2 == 0 else (3, 2)
                measured = {}
                for count in order:
                    measured[count] = profile_page_refresh(
                        service, probe, human_seat, integrity_guard, snapshot_count=count
                    )
                profiles_two.append(measured[2])
                profiles_three.append(measured[3])

            snapshots = [sample for profile in profiles_two for sample in profile["snapshots"]]
            guards = [
                value for profile in profiles_two
                for value in profile["integrity_guard_ms"]
            ]
            paired_deltas = [
                legacy["total_ms"] - current["total_ms"]
                for current, legacy in zip(profiles_two, profiles_three)
            ]
            paired_savings = [
                100.0 * delta / legacy["total_ms"]
                for delta, legacy in zip(paired_deltas, profiles_three)
                if legacy["total_ms"] > 0
            ]
            return {
                "public_rows": public_rows,
                "reasoning_rows": sum(len(rows) for rows in engine.reasoning_log.values()),
                "snapshots_per_refresh": NORMAL_ACTIVE_REFRESH_SNAPSHOT_CALLS,
                "page_refresh": _stats([profile["total_ms"] for profile in profiles_two]),
                "legacy_three_snapshot_page_refresh": _stats([
                    profile["total_ms"] for profile in profiles_three
                ]),
                "paired_page_refresh_delta_ms": _stats(paired_deltas),
                "paired_page_refresh_saving_pct": _stats(paired_savings),
                "configure": _stats([profile["configure_ms"] for profile in profiles_two]),
                "snapshot_total": _stats([sample["total_ms"] for sample in snapshots]),
                "snapshot_lock_wait": _stats([sample["lock_wait_ms"] for sample in snapshots]),
                "snapshot_lock_hold": _stats([sample["lock_hold_ms"] for sample in snapshots]),
                "snapshot_deepcopy": _stats([sample["deepcopy_ms"] for sample in snapshots]),
                "deepcopy_calls_per_snapshot": round(
                    statistics.mean(sample["deepcopy_calls"] for sample in snapshots), 2
                ),
                "integrity_guard_per_call": _stats(guards),
                "integrity_guard_per_refresh": _stats([
                    sum(profile["integrity_guard_ms"]) for profile in profiles_two
                ]),
                "projection": _stats([profile["projection_ms"] for profile in profiles_two]),
                "feed_selection": _stats([
                    profile["feed_selection_ms"] for profile in profiles_two
                ]),
                "visible_feed_rows_median": round(statistics.median(
                    profile["visible_feed_rows"] for profile in profiles_two
                ), 1),
            }
        finally:
            probe.close()
            with patch("service.game_service.archive_game", return_value=None):
                service.close()


def page_refresh_contention(public_rows, duration, poll_interval, integrity_guard):
    """Compare worker throughput alone vs a two-snapshot data refresh every 1.5 s."""
    engine, human_seat = baseline.fixture(public_rows)
    mark_human_rows(engine, human_seat)
    with tempfile.TemporaryDirectory(prefix="swv-page-refresh-contention-") as directory:
        service = GameService(
            engine,
            directory,
            automatic=True,
            delay=0,
            turbo=True,
            generator=baseline.pulse_generator(engine),
        )
        probe = None
        try:
            with patch("service.game_service.rerun_delay", return_value=0.005):
                service.play()
                time.sleep(0.15)

                baseline_start = engine.day
                started = time.perf_counter()
                time.sleep(duration)
                baseline_seconds = time.perf_counter() - started
                baseline_steps = engine.day - baseline_start

                probe = SnapshotProbe(service)
                loaded_start = engine.day
                started = time.perf_counter()
                profiles = []
                while time.perf_counter() - started < duration:
                    cycle_started = time.perf_counter()
                    profiles.append(
                        profile_page_refresh(service, probe, human_seat, integrity_guard)
                    )
                    remaining = poll_interval - (time.perf_counter() - cycle_started)
                    if remaining > 0:
                        time.sleep(remaining)
                loaded_seconds = time.perf_counter() - started
                loaded_steps = engine.day - loaded_start

                if baseline_steps <= 0 or loaded_steps <= 0 or len(profiles) < 2:
                    raise RuntimeError(
                        "Invalid page-refresh contention run: "
                        f"baseline_steps={baseline_steps}, loaded_steps={loaded_steps}, "
                        f"refreshes={len(profiles)}"
                    )
                snapshot_samples = [
                    sample for profile in profiles for sample in profile["snapshots"]
                ]
                baseline_rate = baseline_steps / baseline_seconds
                loaded_rate = loaded_steps / loaded_seconds
                return {
                    "public_rows": public_rows,
                    "poll_interval_ms": round(poll_interval * 1000, 2),
                    "baseline_steps": baseline_steps,
                    "baseline_seconds": round(baseline_seconds, 3),
                    "loaded_steps": loaded_steps,
                    "loaded_seconds": round(loaded_seconds, 3),
                    "refreshes": len(profiles),
                    "snapshots": len(snapshot_samples),
                    "baseline_steps_per_second": round(baseline_rate, 2),
                    "loaded_steps_per_second": round(loaded_rate, 2),
                    "relative_throughput_pct": round(
                        100 * loaded_rate / baseline_rate, 1
                    ),
                    "page_refresh": _stats([profile["total_ms"] for profile in profiles]),
                    "snapshot_total": _stats([
                        sample["total_ms"] for sample in snapshot_samples
                    ]),
                    "snapshot_lock_wait": _stats([
                        sample["lock_wait_ms"] for sample in snapshot_samples
                    ]),
                    "snapshot_lock_hold": _stats([
                        sample["lock_hold_ms"] for sample in snapshot_samples
                    ]),
                    "snapshot_deepcopy": _stats([
                        sample["deepcopy_ms"] for sample in snapshot_samples
                    ]),
                }
        finally:
            if probe is not None:
                probe.close()
            with patch("service.game_service.archive_game", return_value=None):
                service.close()


def render_markdown(payload):
    metadata = payload["metadata"]
    lines = [
        "# SiliconWolfVillage · Page refresh profiling",
        "",
        f"- UTC: {metadata['timestamp_utc']}",
        f"- Python: {metadata['python']}",
        f"- Host: {metadata['platform']}; CPUs reported: {metadata['cpu_count']}",
        f"- Git SHA: {metadata['git_sha']}",
        "- Synthetic workload; offline manager and pulse generator; no paid model requests.",
        f"- Normal active-game rerun models {NORMAL_ACTIVE_REFRESH_SNAPSHOT_CALLS} snapshots, "
        f"the app's exact integrity guard {NORMAL_ACTIVE_REFRESH_SNAPSHOT_CALLS} times, configure, one projection, and one bounded feed selection.",
        f"- All app.py sync_game_state call sites: {metadata['all_sync_game_state_call_sites']}",
        f"- Module-level rerun call sites: {metadata['top_level_sync_game_state_call_sites']}",
        "",
        "## Isolated normal page-refresh data path",
        "",
        "| Events | Current 2-snapshot path P50 / P95 (ms) | Legacy 3-snapshot path P50 / P95 (ms) | "
        "Paired delta P50 (ms) | Paired saving P50 (%) | Single snapshot P50 / P95 (ms) | "
        "Lock wait P50 (ms) | Lock hold P50 (ms) | deepcopy P50 (ms) | Guard/call P50 (ms) | Projection P50 (ms) | Feed P50 (ms) |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for case in payload["cases"]:
        lines.append(
            f"| {case['public_rows']:,} "
            f"| {case['page_refresh']['median_ms']:.2f} / {case['page_refresh']['p95_ms']:.2f} "
            f"| {case['legacy_three_snapshot_page_refresh']['median_ms']:.2f} / "
            f"{case['legacy_three_snapshot_page_refresh']['p95_ms']:.2f} "
            f"| {case['paired_page_refresh_delta_ms']['median_ms']:.2f} "
            f"| {case['paired_page_refresh_saving_pct']['median_ms']:.1f} "
            f"| {case['snapshot_total']['median_ms']:.2f} / {case['snapshot_total']['p95_ms']:.2f} "
            f"| {case['snapshot_lock_wait']['median_ms']:.3f} "
            f"| {case['snapshot_lock_hold']['median_ms']:.2f} "
            f"| {case['snapshot_deepcopy']['median_ms']:.2f} "
            f"| {case['integrity_guard_per_call']['median_ms']:.3f} "
            f"| {case['projection']['median_ms']:.2f} "
            f"| {case['feed_selection']['median_ms']:.3f} |"
        )
    field_profiles = payload.get("snapshot_field_profiles", [])
    if field_profiles:
        lines.extend([
            "",
            "## Engine-field deepcopy cost inside GameService.snapshot",
            "",
            "Per-field timings are attributed to the top-level engine attribute passed to deepcopy by GameService.snapshot. The table shows the eight largest fields at each workload size.",
            "",
        ])
        for field_profile in field_profiles:
            total_copy = field_profile["snapshot_deepcopy"]["median_ms"]
            lines.extend([
                f"### {field_profile['public_rows']:,} public events",
                "",
                "| Engine field | Type | Items / rows | Deepcopy P50 / P95 (ms) | P50 / total deepcopy P50 |",
                "|---|---|---:|---:|---:|",
            ])
            ranked = sorted(
                field_profile["fields"].items(),
                key=lambda item: item[1]["median_ms"],
                reverse=True,
            )[:8]
            for name, stats in ranked:
                share = 100.0 * stats["median_ms"] / total_copy if total_copy else 0.0
                items = stats["items"] if stats["items"] is not None else "—"
                lines.append(
                    f"| {name} | {stats['type']} | {items} "
                    f"| {stats['median_ms']:.2f} / {stats['p95_ms']:.2f} "
                    f"| {share:.1f}% |"
                )
            lines.extend([
                "",
                f"- Snapshot deepcopy P50: {total_copy:.2f} ms; unattributed manager/runtime/other deepcopy P50: {field_profile['unattributed_deepcopy']['median_ms']:.2f} ms.",
                f"- Sum of per-field medians: {field_profile['field_median_sum_ms']:.2f} ms. Per-field medians are independent statistics and are not expected to sum exactly to the total median.",
                "",
            ])

    immutable_profiles = payload.get("immutable_log_profiles", [])
    if immutable_profiles:
        lines.extend([
            "",
            "## Benchmark-only immutable log-row prototype",
            "",
            "The prototype recursively freezes JSON-like event rows while leaving outer log containers appendable. It is not used by production code. Snapshot deep copies can then reuse immutable row objects; view projection and feed selection are compared against ordinary mutable rows.",
            "",
            "| Events | Snapshot deepcopy normal / frozen P50 (ms) | public_log copy normal / frozen (ms) | reasoning_log copy normal / frozen (ms) | Projection normal / frozen (ms) | Feed normal / frozen (ms) |",
            "|---:|---:|---:|---:|---:|---:|",
        ])
        for item in immutable_profiles:
            lines.append(
                f"| {item['public_rows']:,} "
                f"| {item['normal_snapshot_deepcopy']['median_ms']:.2f} / {item['frozen_snapshot_deepcopy']['median_ms']:.2f} "
                f"| {item['normal_public_log_copy']['median_ms']:.2f} / {item['frozen_public_log_copy']['median_ms']:.2f} "
                f"| {item['normal_reasoning_log_copy']['median_ms']:.2f} / {item['frozen_reasoning_log_copy']['median_ms']:.2f} "
                f"| {item['normal_projection']['median_ms']:.2f} / {item['frozen_projection']['median_ms']:.2f} "
                f"| {item['normal_feed_selection']['median_ms']:.3f} / {item['frozen_feed_selection']['median_ms']:.3f} |"
            )
        lines.extend([
            "",
            "| Events | View parity | Feed parity | JSON export parity | Public row shared | Reasoning row shared |",
            "|---:|:---:|:---:|:---:|:---:|:---:|",
        ])
        for item in immutable_profiles:
            lines.append(
                f"| {item['public_rows']:,} "
                f"| {'PASS' if item['view_parity'] else 'FAIL'} "
                f"| {'PASS' if item['feed_parity'] else 'FAIL'} "
                f"| {'PASS' if item['export_parity'] else 'FAIL'} "
                f"| {'YES' if item['shared_public_row'] else 'NO'} "
                f"| {'YES' if item['shared_reasoning_row'] else 'NO'} |"
            )
        lines.extend([
            "",
            "- The prototype uses dict/list subclasses and returns self from deepcopy only after recursively freezing nested values.",
            "- It is a compatibility/performance experiment, not a production immutability guarantee; append, load_state, serialization, and all engine write paths still require dedicated regression coverage before integration.",
            "",
        ])

    contention = payload["page_refresh_contention"]
    lines.extend([
        "",
        "## Worker contention under an ordinary page refresh",
        "",
        f"- {contention['public_rows']:,} events; one page data refresh every "
        f"{contention['poll_interval_ms']:.0f} ms.",
        f"- Worker alone: {contention['baseline_steps_per_second']:.1f} steps/s; "
        f"with page refreshes: {contention['loaded_steps_per_second']:.1f} steps/s "
        f"({contention['relative_throughput_pct']:.1f}% retained).",
        f"- Completed {contention['refreshes']} page refreshes and "
        f"{contention['snapshots']} snapshots during the loaded interval.",
        f"- Page data path median / P95: {contention['page_refresh']['median_ms']:.2f} / "
        f"{contention['page_refresh']['p95_ms']:.2f} ms.",
        f"- Snapshot lock wait median / P95: {contention['snapshot_lock_wait']['median_ms']:.3f} / "
        f"{contention['snapshot_lock_wait']['p95_ms']:.3f} ms.",
        f"- Snapshot lock hold median / P95: {contention['snapshot_lock_hold']['median_ms']:.2f} / "
        f"{contention['snapshot_lock_hold']['p95_ms']:.2f} ms.",
        f"- Snapshot deepcopy median / P95: {contention['snapshot_deepcopy']['median_ms']:.2f} / "
        f"{contention['snapshot_deepcopy']['p95_ms']:.2f} ms.",
        "",
        "## Measurement boundaries",
        "",
        "- Snapshot lock wait measures only time waiting to enter GameService.snapshot's Condition.",
        "- Snapshot lock hold measures the Condition critical section, including engine deep copies and manager/runtime diagnostics.",
        "- Deepcopy time counts copy.deepcopy calls made by GameService.snapshot; it excludes project_view's separately imported deepcopy calls.",
        "- Optional field profiling maps each top-level engine attribute by object identity before snapshot and attributes only the corresponding outer deepcopy call; nested recursive copies remain within that field's elapsed time.",
        "- Unattributed deepcopy includes manager/runtime diagnostics and snapshot metadata not copied from a top-level engine field.",
        "- Integrity-guard timing runs the exact self-contained function extracted from app.py; synthetic human-seat records are marked as human actions.",
        "- The current two-snapshot path is compared in the same process with a legacy three-snapshot control; the paired delta is more useful than cross-run absolute timing.",
        "- The refresh harness reproduces the active-game state/data path, not Streamlit component rendering, websocket delivery, browser paint, or network latency.",
        "- The contention case uses a no-network pulse generator. It is a repeatable scheduling/lock stressor, not a production model-throughput estimate.",
        "- GitHub Actions runners are shared; compare medians and scaling, not absolute millisecond values across unrelated runs.",
        "- No production service, engine, UI, or lock behavior is modified by this profiler.",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="100,1000,5000,15000")
    parser.add_argument("--repeats", type=int, default=9)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    parser.add_argument("--contention-seconds", type=float, default=4.6)
    parser.add_argument("--poll-interval", type=float, default=1.5)
    args = parser.parse_args()
    sizes = [int(value) for value in args.sizes.split(",")]
    if not sizes or any(value < 10 for value in sizes) or args.repeats < 3:
        parser.error("Require sizes >= 10 and repeats >= 3")
    if args.contention_seconds <= 0 or args.poll_interval <= 0:
        parser.error("Contention duration and poll interval must be positive")

    from datetime import datetime, timezone

    integrity_guard = load_integrity_guard()
    all_sites, top_level_sites = inspect_sync_call_sites()
    if len(top_level_sites) < NORMAL_ACTIVE_REFRESH_SNAPSHOT_CALLS:
        raise RuntimeError(
            "app.py has fewer module-level sync_game_state calls than the modeled active path"
        )
    payload = {
        "metadata": {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "git_sha": os.getenv("GITHUB_SHA", "local"),
            "all_sync_game_state_call_sites": all_sites,
            "top_level_sync_game_state_call_sites": top_level_sites,
            "normal_active_refresh_snapshot_calls": NORMAL_ACTIVE_REFRESH_SNAPSHOT_CALLS,
            "workload": "balanced 12-seat synthetic history; legitimate human actions on human seat",
        },
        "cases": [],
        "snapshot_field_profiles": [],
        "immutable_log_profiles": [],
    }

    for size in sizes:
        case = profile_case(size, args.repeats, integrity_guard)
        payload["cases"].append(case)
        field_profile = profile_snapshot_fields(size, args.repeats)
        payload["snapshot_field_profiles"].append(field_profile)
        immutable_profile = profile_immutable_log_snapshot(size, args.repeats)
        payload["immutable_log_profiles"].append(immutable_profile)
        print(
            f"IMMUTABLE-LOG {size:6d} events: "
            f"snapshot deepcopy {immutable_profile['normal_snapshot_deepcopy']['median_ms']:.3f} -> "
            f"{immutable_profile['frozen_snapshot_deepcopy']['median_ms']:.3f}ms; "
            f"view/feed/export parity={immutable_profile['view_parity']}/"
            f"{immutable_profile['feed_parity']}/{immutable_profile['export_parity']}",
            flush=True,
        )
        top_fields = sorted(
            field_profile["fields"].items(),
            key=lambda item: item[1]["median_ms"],
            reverse=True,
        )[:3]
        print(
            f"SNAPSHOT-FIELDS {size:6d} events: "
            + "; ".join(f"{name}={stats['median_ms']:.3f}ms" for name, stats in top_fields),
            flush=True,
        )
        print(
            f"PAGE-REFRESH {size:6d} events: "
            f"total={case['page_refresh']['median_ms']:.3f}ms; "
            f"snapshot={case['snapshot_total']['median_ms']:.3f}ms; "
            f"lock={case['snapshot_lock_hold']['median_ms']:.3f}ms; "
            f"deepcopy={case['snapshot_deepcopy']['median_ms']:.3f}ms",
            flush=True,
        )
        print(
            f"PAGE-REFRESH-AB {size:6d} events: "
            f"2-snapshot={case['page_refresh']['median_ms']:.3f}ms; "
            f"legacy-3-snapshot={case['legacy_three_snapshot_page_refresh']['median_ms']:.3f}ms; "
            f"paired-delta={case['paired_page_refresh_delta_ms']['median_ms']:.3f}ms "
            f"({case['paired_page_refresh_saving_pct']['median_ms']:.1f}% of legacy)",
            flush=True,
        )

    contention = page_refresh_contention(
        sizes[-1], args.contention_seconds, args.poll_interval, integrity_guard
    )
    payload["page_refresh_contention"] = contention
    print(
        f"PAGE-REFRESH-STRESS {sizes[-1]:6d} events: "
        f"{contention['baseline_steps_per_second']:.2f} steps/s alone; "
        f"{contention['loaded_steps_per_second']:.2f} with refreshes; "
        f"{contention['relative_throughput_pct']}% retained; "
        f"{contention['refreshes']} refreshes / {contention['snapshots']} snapshots",
        flush=True,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "page-refresh-profile.json"
    markdown_path = args.output_dir / "page-refresh-profile.md"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    markdown_path.write_text(render_markdown(payload), encoding="utf-8")
    print("\n" + render_markdown(payload), flush=True)
    print(f"Artifacts: {json_path}, {markdown_path}", flush=True)


if __name__ == "__main__":
    main()
