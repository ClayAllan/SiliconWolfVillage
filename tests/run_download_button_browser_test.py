"""End-to-end browser test for Streamlit ZIP downloads."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "tests" / "fixtures" / "download_button_smoke_app.py"
BASE_URL = "http://127.0.0.1:8501"
HEALTH_URL = f"{BASE_URL}/_stcore/health"
EXPECTED_FILENAME = "private-archive-smoke.zip"


def wait_for_server(process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output, _ = process.communicate()
            raise RuntimeError(f"Streamlit exited before becoming healthy:\n{output[-8000:]}")
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    raise TimeoutError("Streamlit test app did not become healthy within 45 seconds")


def main() -> None:
    env = os.environ.copy()
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(APP),
            "--server.address=127.0.0.1",
            "--server.port=8501",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
        ],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_server(process)
        with tempfile.TemporaryDirectory(prefix="swv-download-smoke-") as temp_dir:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(accept_downloads=True)

                def log_media_response(response) -> None:
                    if "/media/" in response.url:
                        print(
                            f"Media response HTTP {response.status}: {response.url} "
                            f"({response.headers.get('content-type', 'unknown')})"
                        )

                page.on("response", log_media_response)
                page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
                button = page.get_by_role("button", name="Download ZIP fixture")
                button.wait_for(state="visible", timeout=30000)

                for index in range(2):
                    with page.expect_download(timeout=20000) as download_info:
                        button.click()
                    download = download_info.value
                    output = Path(temp_dir) / f"download-{index + 1}.zip"
                    download.save_as(output)
                    failure = download.failure()
                    if failure:
                        raise AssertionError(f"Download {index + 1} failed: {failure}")
                    if download.suggested_filename != EXPECTED_FILENAME:
                        raise AssertionError(
                            "Browser did not receive the intended filename: "
                            f"{download.suggested_filename!r}"
                        )
                    with zipfile.ZipFile(output) as archive:
                        if archive.testzip() is not None:
                            raise AssertionError(f"Download {index + 1} is a corrupt ZIP")
                        if "fixture/manifest.json" not in archive.namelist():
                            raise AssertionError(f"Download {index + 1} lacks manifest.json")
                        manifest = archive.read("fixture/manifest.json").decode("utf-8")
                        if "browser-download-smoke" not in manifest:
                            raise AssertionError(f"Download {index + 1} has unexpected content")
                    print(
                        f"Browser download {index + 1}/2 passed: "
                        f"{download.suggested_filename}, {output.stat().st_size} bytes, valid ZIP"
                    )
                browser.close()
    except Exception:
        if process.poll() is None:
            process.terminate()
        try:
            output, _ = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()
        print("Streamlit fixture server output (tail):\n" + output[-8000:], file=sys.stderr)
        raise
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    main()
