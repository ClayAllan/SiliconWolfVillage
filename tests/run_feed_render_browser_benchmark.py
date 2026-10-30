#!/usr/bin/env python3
"""Measure the production Streamlit feed component in a real Chromium browser.

The harness uses synthetic immutable rows and the real ui.feed_ui.render_feed()
plus ui.ui_design.event(). It does not import app.py, construct an AgentManager,
or call a paid model API.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "tests" / "fixtures" / "feed_render_benchmark_app.py"
BASE_URL = "http://127.0.0.1:8502"
HEALTH_URL = f"{BASE_URL}/_stcore/health"
READY_SELECTOR = "#swv-feed-benchmark-ready"
METRICS_SELECTOR = "#swv-feed-benchmark-metrics"


def wait_for_server(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Streamlit exited before becoming healthy")
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    raise TimeoutError("Streamlit feed fixture did not become healthy within 60 seconds")


def frame_size(frame) -> int:
    payload = getattr(frame, "payload", frame)
    if isinstance(payload, str):
        return len(payload.encode("utf-8"))
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return len(payload)
    return len(str(payload).encode("utf-8"))


def run_case(browser, event_count: int, mode: str, repeat: int) -> dict:
    expected_rows = event_count if mode == "full" else min(event_count, 90)
    websocket = {
        "connections": 0,
        "received_frames": 0,
        "received_bytes": 0,
        "sent_frames": 0,
        "sent_bytes": 0,
    }

    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = context.new_page()

    def on_websocket(socket):
        websocket["connections"] += 1

        def received(frame):
            websocket["received_frames"] += 1
            websocket["received_bytes"] += frame_size(frame)

        def sent(frame):
            websocket["sent_frames"] += 1
            websocket["sent_bytes"] += frame_size(frame)

        socket.on("framereceived", received)
        socket.on("framesent", sent)

    page.on("websocket", on_websocket)
    started = time.perf_counter()
    try:
        response = page.goto(
            f"{BASE_URL}?events={event_count}&mode={mode}",
            wait_until="domcontentloaded",
            timeout=90_000,
        )
        if response is not None and response.status != 200:
            raise AssertionError(f"Fixture returned HTTP {response.status}")

        page.locator(READY_SELECTOR).wait_for(state="visible", timeout=90_000)
        page.wait_for_function(
            """expected => {
                const root = document.querySelector('[class*="st-key-live_feed_"]');
                return !!root && root.querySelectorAll('.wv-event').length === expected;
            }""",
            arg=expected_rows,
            timeout=90_000,
        )
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        time.sleep(0.2)
        browser_ready_ms = (time.perf_counter() - started) * 1000.0

        metrics = json.loads(page.locator(METRICS_SELECTOR).inner_text())
        browser_metrics = page.evaluate(
            """() => {
                const root = document.querySelector('[class*="st-key-live_feed_"]');
                if (!root) return {feed_root_found: false};
                const html = root.innerHTML || '';
                return {
                    feed_root_found: true,
                    rendered_events: root.querySelectorAll('.wv-event').length,
                    dom_nodes: root.querySelectorAll('*').length + 1,
                    feed_html_bytes: new TextEncoder().encode(html).length,
                    feed_text_chars: (root.innerText || '').length,
                    feed_scroll_height: root.scrollHeight,
                    browser_heap_used_bytes: performance.memory
                        ? performance.memory.usedJSHeapSize : null
                };
            }"""
        )

        if metrics["rendered_rows"] != expected_rows:
            raise AssertionError(
                f"Python renderer emitted {metrics['rendered_rows']} rows; "
                f"expected {expected_rows}"
            )
        if browser_metrics.get("rendered_events") != expected_rows:
            raise AssertionError(
                f"Chromium rendered {browser_metrics.get('rendered_events')} events; "
                f"expected {expected_rows}; details={browser_metrics}"
            )

        return {
            "events_in_history": event_count,
            "mode": mode,
            "repeat": repeat,
            "browser_ready_ms": round(browser_ready_ms, 3),
            "fixture_build_ms": metrics["fixture_build_ms"],
            "python_render_ms": metrics["python_render_ms"],
            "python_rendered_rows": metrics["rendered_rows"],
            "python_html_bytes": metrics["rendered_html_bytes"],
            **browser_metrics,
            **websocket,
        }
    finally:
        context.close()


def median(values):
    return round(statistics.median(values), 3)


def percentile95(values):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3)


def summarize(samples: list[dict]) -> list[dict]:
    grouped = {}
    for sample in samples:
        grouped.setdefault((sample["events_in_history"], sample["mode"]), []).append(sample)

    rows = []
    for (events, mode), group in sorted(grouped.items()):
        keys = (
            "fixture_build_ms",
            "python_render_ms",
            "browser_ready_ms",
            "python_html_bytes",
            "feed_html_bytes",
            "dom_nodes",
            "feed_text_chars",
            "feed_scroll_height",
            "received_bytes",
            "received_frames",
            "browser_heap_used_bytes",
        )
        summary = {"events_in_history": events, "mode": mode, "samples": len(group)}
        for key in keys:
            values = [item[key] for item in group if item.get(key) is not None]
            if values:
                summary[key + "_median"] = median(values)
                summary[key + "_p95"] = percentile95(values)
        rows.append(summary)
    return rows


def render_markdown(payload: dict) -> str:
    lines = [
        "# SiliconWolfVillage · Feed browser-render baseline",
        "",
        f"- UTC: {payload['metadata']['timestamp_utc']}",
        f"- Python: {payload['metadata']['python']}",
        f"- Host: {payload['metadata']['platform']}; CPUs reported: {payload['metadata']['cpu_count']}",
        f"- Chromium: {payload['metadata']['browser']}",
        f"- Repeats per scenario: {payload['metadata']['repeats']}",
        "- Synthetic, recursively immutable public-log rows; no model API or paid requests.",
        "- Uses the production feed component and event HTML renderer inside a minimal Streamlit host.",
        "",
        "| History rows | Mode | Python render P50 (ms) | Browser ready P50 (ms) | Python HTML (KiB) | Feed DOM (KiB) | DOM nodes | WS received (KiB) |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["summary"]:
        lines.append(
            f"| {row['events_in_history']:,} | {row['mode']} "
            f"| {row['python_render_ms_median']:.2f} "
            f"| {row['browser_ready_ms_median']:.2f} "
            f"| {row['python_html_bytes_median'] / 1024:.1f} "
            f"| {row['feed_html_bytes_median'] / 1024:.1f} "
            f"| {row['dom_nodes_median']:.0f} "
            f"| {row['received_bytes_median'] / 1024:.1f} |"
        )
    lines.extend([
        "",
        "## Interpretation boundaries",
        "",
        "- Python render time measures the fixture's render_feed call, including production event HTML generation and Streamlit element construction; it excludes later browser work.",
        "- Browser ready time measures navigation start until the expected event DOM is present and two animation frames have elapsed. It includes local Streamlit session work, WebSocket delivery, browser parsing and rendering.",
        "- WebSocket byte counts include all frames observed for that fixture session, not only the feed delta. They are a session-level transport indicator, not an exact serialized feed payload size.",
        "- DOM counts and HTML bytes are scoped to the keyed feed container, not the entire application.",
        "- The fixture excludes the rest of app.py, model inference, external API latency, production network RTT, multi-viewer contention and long-lived memory retention.",
        "- Results are diagnostic measurements on a shared GitHub Actions runner, not production latency guarantees. Compare trends and same-run scenarios rather than treating absolute milliseconds as universal thresholds.",
        "",
    ])
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", default="100,1000,5000,15000")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    args = parser.parse_args()

    sizes = [int(value) for value in args.sizes.split(",")]
    if not sizes or any(value < 1 or value > 15_000 for value in sizes):
        parser.error("sizes must be between 1 and 15000")
    if args.repeats < 2:
        parser.error("repeats must be at least 2")

    env = os.environ.copy()
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    server_log = tempfile.TemporaryFile(mode="w+t")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP),
            "--server.address=127.0.0.1",
            "--server.port=8502",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ],
        cwd=ROOT,
        env=env,
        stdout=server_log,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        wait_for_server(process)
        from datetime import datetime, timezone
        import platform

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                # Warm Streamlit's first-session path; discard this sample.
                run_case(browser, min(sizes[0], 100), "bounded", 0)
                samples = []
                for mode in ("bounded", "full"):
                    for size in sizes:
                        for repeat in range(1, args.repeats + 1):
                            sample = run_case(browser, size, mode, repeat)
                            samples.append(sample)
                            print(
                                f"CASE events={size} mode={mode} repeat={repeat}: "
                                f"server_render={sample['python_render_ms']:.2f}ms, "
                                f"browser_ready={sample['browser_ready_ms']:.2f}ms, "
                                f"visible={sample['rendered_events']} rows, "
                                f"ws_rx={sample['received_bytes']} bytes, "
                                f"dom_nodes={sample['dom_nodes']}",
                                flush=True,
                            )
            finally:
                browser.close()

        payload = {
            "metadata": {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
                "browser": "Playwright Chromium",
                "repeats": args.repeats,
                "git_sha": os.getenv("GITHUB_SHA", "local"),
            },
            "samples": samples,
        }
        payload["summary"] = summarize(samples)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = args.output_dir / "feed-render-browser.json"
        markdown_path = args.output_dir / "feed-render-browser.md"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        markdown = render_markdown(payload)
        markdown_path.write_text(markdown, encoding="utf-8")
        print("\n" + markdown, flush=True)
        print(f"Artifacts: {json_path}, {markdown_path}", flush=True)
    except Exception:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        server_log.seek(0)
        print("Streamlit fixture server output (tail):\n" + server_log.read()[-8000:], file=sys.stderr)
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        server_log.close()


if __name__ == "__main__":
    main()
