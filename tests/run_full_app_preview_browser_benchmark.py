#!/usr/bin/env python3
"""Measure the full Streamlit application using its built-in zero-API preview."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"
BASE_URL = "http://127.0.0.1:8503"
HEALTH_URL = f"{BASE_URL}/_stcore/health"


def wait_for_server(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Streamlit app exited before becoming healthy")
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    raise TimeoutError("Full app did not become healthy within 60 seconds")


def frame_size(frame) -> int:
    payload = getattr(frame, "payload", frame)
    if isinstance(payload, str):
        return len(payload.encode("utf-8"))
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return len(payload)
    return len(str(payload).encode("utf-8"))


def browser_metrics(page) -> dict:
    return page.evaluate(
        """() => {
            const body = document.body;
            const feed = document.querySelector('[class*="st-key-live_feed_"]');
            const html = body ? body.innerHTML : '';
            const feedHtml = feed ? feed.innerHTML : '';
            return {
                body_dom_nodes: body ? body.querySelectorAll('*').length + 1 : 0,
                body_html_bytes: new TextEncoder().encode(html).length,
                body_text_chars: body ? (body.innerText || '').length : 0,
                feed_found: !!feed,
                feed_events: feed ? feed.querySelectorAll('.wv-event').length : 0,
                feed_dom_nodes: feed ? feed.querySelectorAll('*').length + 1 : 0,
                feed_html_bytes: new TextEncoder().encode(feedHtml).length,
                browser_heap_used_bytes: performance.memory
                    ? performance.memory.usedJSHeapSize : null
            };
        }"""
    )


def run_case(browser, repeat: int) -> dict:
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
    try:
        started = time.perf_counter()
        response = page.goto(BASE_URL, wait_until="domcontentloaded", timeout=90_000)
        if response is not None and response.status != 200:
            raise AssertionError(f"App returned HTTP {response.status}")

        demo_button = page.get_by_role("button", name="查看示例 · 零 API")
        demo_button.wait_for(state="visible", timeout=90_000)
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        initial_ready_ms = (time.perf_counter() - started) * 1000.0
        time.sleep(0.2)
        initial_metrics = browser_metrics(page)
        initial_ws = dict(websocket)

        preview_started = time.perf_counter()
        demo_button.click()
        exit_button = page.get_by_role("button", name="退出示例")
        exit_button.wait_for(state="visible", timeout=90_000)
        page.wait_for_function(
            """() => {
                const feed = document.querySelector('[class*="st-key-live_feed_"]');
                return !!feed && feed.querySelectorAll('.wv-event').length >= 3;
            }""",
            timeout=90_000,
        )
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        preview_ready_ms = (time.perf_counter() - preview_started) * 1000.0
        time.sleep(0.2)
        preview_metrics = browser_metrics(page)

        if not preview_metrics["feed_found"] or preview_metrics["feed_events"] < 3:
            raise AssertionError(f"Preview did not render the expected feed: {preview_metrics}")
        if websocket["connections"] < 1 or websocket["received_frames"] < 1:
            raise AssertionError(f"No Streamlit WebSocket traffic was observed: {websocket}")

        return {
            "repeat": repeat,
            "initial_ready_ms": round(initial_ready_ms, 3),
            "preview_ready_ms": round(preview_ready_ms, 3),
            "initial_metrics": initial_metrics,
            "preview_metrics": preview_metrics,
            "initial_websocket": initial_ws,
            "preview_websocket_delta": {
                "received_frames": websocket["received_frames"] - initial_ws["received_frames"],
                "received_bytes": websocket["received_bytes"] - initial_ws["received_bytes"],
                "sent_frames": websocket["sent_frames"] - initial_ws["sent_frames"],
                "sent_bytes": websocket["sent_bytes"] - initial_ws["sent_bytes"],
            },
            "websocket_total": websocket,
        }
    finally:
        context.close()


def median(values):
    return round(statistics.median(values), 3)


def percentile95(values):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 3)


def summarize(samples: list[dict]) -> dict:
    summary = {}
    for key in ("initial_ready_ms", "preview_ready_ms"):
        values = [sample[key] for sample in samples]
        summary[key + "_median"] = median(values)
        summary[key + "_p95"] = percentile95(values)

    for phase in ("initial_metrics", "preview_metrics"):
        for key in (
            "body_dom_nodes",
            "body_html_bytes",
            "body_text_chars",
            "feed_events",
            "feed_dom_nodes",
            "feed_html_bytes",
            "browser_heap_used_bytes",
        ):
            values = [
                sample[phase][key]
                for sample in samples
                if sample[phase].get(key) is not None
            ]
            if values:
                summary[f"{phase}_{key}_median"] = median(values)
                summary[f"{phase}_{key}_p95"] = percentile95(values)

    for key in ("received_frames", "received_bytes", "sent_frames", "sent_bytes"):
        values = [sample["preview_websocket_delta"][key] for sample in samples]
        summary["preview_ws_" + key + "_median"] = median(values)
        summary["preview_ws_" + key + "_p95"] = percentile95(values)
    return summary


def render_markdown(payload: dict) -> str:
    s = payload["summary"]
    lines = [
        "# SiliconWolfVillage · Full-app zero-API preview browser baseline",
        "",
        f"- UTC: {payload['metadata']['timestamp_utc']}",
        f"- Git SHA: {payload['metadata']['git_sha']}",
        f"- Python: {payload['metadata']['python']}",
        f"- Host: {payload['metadata']['platform']}; CPUs reported: {payload['metadata']['cpu_count']}",
        f"- Browser: {payload['metadata']['browser']}",
        f"- Repeats: {payload['metadata']['repeats']}",
        "- Starts the actual app.py and uses its built-in zero-API preview button.",
        "- No API key, AgentManager, GameService, paid model call or production data is used by this benchmark path.",
        "",
        "| Metric | Initial app shell | Preview page |",
        "|---|---:|---:|",
        f"| Browser ready (ms, P50) | {s['initial_ready_ms_median']:.1f} | {s['preview_ready_ms_median']:.1f} after click |",
        f"| Body DOM nodes (P50) | {s['initial_metrics_body_dom_nodes_median']:.0f} | {s['preview_metrics_body_dom_nodes_median']:.0f} |",
        f"| Body HTML (KiB, P50) | {s['initial_metrics_body_html_bytes_median'] / 1024:.1f} | {s['preview_metrics_body_html_bytes_median'] / 1024:.1f} |",
        f"| Feed events (P50) | {s['initial_metrics_feed_events_median']:.0f} | {s['preview_metrics_feed_events_median']:.0f} |",
        f"| Feed DOM nodes (P50) | {s['initial_metrics_feed_dom_nodes_median']:.0f} | {s['preview_metrics_feed_dom_nodes_median']:.0f} |",
        f"| Feed HTML (KiB, P50) | {s['initial_metrics_feed_html_bytes_median'] / 1024:.1f} | {s['preview_metrics_feed_html_bytes_median'] / 1024:.1f} |",
        "",
        f"- Preview WebSocket delta received: {s['preview_ws_received_bytes_median'] / 1024:.1f} KiB (P50).",
        f"- Preview WebSocket delta sent: {s['preview_ws_sent_bytes_median'] / 1024:.1f} KiB (P50).",
        "",
        "## Interpretation boundaries",
        "",
        "- This is a real-browser measurement of the full Streamlit app in its built-in static preview mode, not an active AI game.",
        "- Initial ready time ends when the zero-API preview button is visible and two animation frames have elapsed. Preview ready time ends when the exit button and preview feed are present and two animation frames have elapsed.",
        "- A separate 200 ms settling interval is excluded from both readiness times and is used only to collect late WebSocket frames and browser heap metrics.",
        "- WebSocket deltas include all frames sent/received during the preview transition, not only feed content.",
        "- The preview contains a fixed small example log; large-history scaling is measured separately by the feed component benchmark.",
        "- Results are diagnostic measurements on a shared GitHub Actions runner, not production latency guarantees or real-user network measurements.",
        "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-results"))
    args = parser.parse_args()
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
            "--server.port=8503",
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

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                samples = []
                for repeat in range(1, args.repeats + 1):
                    sample = run_case(browser, repeat)
                    samples.append(sample)
                    print(
                        f"CASE repeat={repeat}: initial={sample['initial_ready_ms']:.1f}ms, "
                        f"preview={sample['preview_ready_ms']:.1f}ms, "
                        f"preview_dom={sample['preview_metrics']['body_dom_nodes']} nodes, "
                        f"preview_ws_rx={sample['preview_websocket_delta']['received_bytes']} bytes",
                        flush=True,
                    )
            finally:
                browser.close()

        payload = {
            "metadata": {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "git_sha": os.getenv("GITHUB_SHA", "local"),
                "python": sys.version.split()[0],
                "platform": platform.platform(),
                "cpu_count": os.cpu_count(),
                "browser": "Playwright Chromium",
                "repeats": args.repeats,
            },
            "samples": samples,
        }
        payload["summary"] = summarize(samples)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = args.output_dir / "full-app-preview-browser.json"
        markdown_path = args.output_dir / "full-app-preview-browser.md"
        json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                             encoding="utf-8")
        markdown = render_markdown(payload)
        markdown_path.write_text(markdown, encoding="utf-8")
        print("\n" + markdown, flush=True)
        print(f"Artifacts: {json_path}, {markdown_path}", flush=True)
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        server_log.seek(0)
        print("Streamlit app output (tail):\n" + server_log.read()[-8000:], file=sys.stderr)
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
