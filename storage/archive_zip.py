"""Safely package an existing V2 game archive for *private owner-only* download.

This ZIP is forensic/private data, not a spectator/public replay export.
Only finalized standard archive files are included and no API key is added.
"""
from __future__ import annotations

import io
import json
import logging
import os
import time
import re
import stat
import zipfile
from heapq import nlargest
from pathlib import Path

# Keep packaging predictable in a Streamlit rerun; reject oversized exports
# instead of building arbitrarily large ZIPs in server memory.
MAX_ARCHIVE_FILE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ZIP_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_CHOICES = 50

_LOGGER = logging.getLogger(__name__)

STANDARD_ARCHIVE_FILES = (
    "manifest.json",
    "summary.md",
    "final_state.json",
    "events.jsonl",
    "api_calls.jsonl",
    "memory.jsonl",
    "runtime.log",
    "replay_frames.jsonl",
    "replay_items.jsonl",
    "replay_manifest.json",
    "session.json",
)
_ARCHIVE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}\Z")


def recent_archive_directories(replay_root, limit=MAX_ARCHIVE_CHOICES) -> list[Path]:
    """List only recent finalized archives within this browser's isolated root."""
    root = Path(replay_root)
    try:
        return nlargest(
            max(1, min(int(limit), MAX_ARCHIVE_CHOICES)),
            (
                path for path in root.iterdir()
                if path.is_dir() and not path.is_symlink()
                and _ARCHIVE_NAME.fullmatch(path.name)
                and (path / "manifest.json").is_file()
                and not (path / "manifest.json").is_symlink()
            ),
            key=lambda path: path.name,
        )
    except (OSError, ValueError, TypeError):
        return []


def _validate_archive_directory(replay_root, archive_directory) -> Path:
    root = Path(replay_root).resolve(strict=True)
    selected = Path(archive_directory)
    if selected.is_symlink() or not _ARCHIVE_NAME.fullmatch(selected.name):
        raise ValueError("档案目录名称无效或为符号链接")
    selected = selected.resolve(strict=True)
    if selected.parent != root or not selected.is_dir():
        raise ValueError("只能导出当前会话的直接子目录档案")
    return selected


def build_private_archive_zip(replay_root, archive_directory) -> bytes:
    """Build a bounded, one-game ZIP; no recursive traversal or arbitrary files."""
    started = time.monotonic()
    try:
        selected = _validate_archive_directory(replay_root, archive_directory)
    except (OSError, RuntimeError) as error:
        raise ValueError("无法定位当前会话的历史档案") from error

    manifest_path = selected / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("不是完整的 V2 历史档案：缺少 manifest.json")
    if manifest_path.stat().st_size > MAX_ARCHIVE_FILE_BYTES:
        raise ValueError("档案文件超过单文件大小上限")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("manifest.json 损坏，无法打包") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("metadata"), dict):
        raise ValueError("manifest.json 不是有效的结构化归档")

    files: list[tuple[str, Path, int]] = []
    total = 0
    for name in STANDARD_ARCHIVE_FILES:
        path = selected / name
        if path.is_symlink():
            raise ValueError(f"档案中存在不允许导出的符号链接：{name}")
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f"档案包含非普通文件：{name}")
        size = path.stat().st_size
        if size > MAX_ARCHIVE_FILE_BYTES:
            raise ValueError(f"{name} 超过 32 MiB 单文件上限")
        total += size
        if total > MAX_ARCHIVE_TOTAL_BYTES:
            raise ValueError("档案总大小超过 64 MiB 上限")
        files.append((name, path, size))
    if not any(name == "manifest.json" for name, _, _ in files):
        raise ValueError("历史档案缺少 manifest.json")

    output = io.BytesIO()
    _LOGGER.info(
        "Private archive ZIP generation started: archive=%s files=%d source_bytes=%d",
        selected.name, len(files), total,
    )
    try:
        # Level 1 is much faster for long JSONL transcripts while retaining compression.
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as dest:
            for name, path, expected_size in files:
                # O_NOFOLLOW and fstat protect against last-second symlink substitution.
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(path, flags)
                with os.fdopen(fd, "rb") as source:
                    current = os.fstat(source.fileno())
                    if not stat.S_ISREG(current.st_mode) or current.st_size != expected_size:
                        raise ValueError(f"{name} 在导出时发生变化，请重试")
                    with dest.open(f"{selected.name}/{name}", "w") as target:
                        remaining = expected_size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError(f"{name} 在导出时被截断")
                            target.write(chunk)
                            remaining -= len(chunk)
            dest.writestr(
                f"{selected.name}/PRIVATE_ARCHIVE_NOTICE.txt",
                "硅基狼村私密历史对局归档\n"
                "此文件包含真实身份、AI 记忆及可能的私密对局记录，"
                "仅供对局所有者保存和授权分析；不得作为公开观战包传播。\n"
                "仅包含该局标准归档文件，不包含其他局的 index.csv 或 API Key。\n",
            )
    except Exception as error:
        _LOGGER.exception(
            "Private archive ZIP generation failed: archive=%s elapsed_seconds=%.3f",
            selected.name, time.monotonic() - started,
        )
        if isinstance(error, (OSError, zipfile.BadZipFile)):
            raise ValueError("生成归档 ZIP 失败，请检查文件是否仍可读取") from error
        raise
    data = output.getvalue()
    if len(data) > MAX_ARCHIVE_ZIP_BYTES:
        raise ValueError("压缩后的档案超过 64 MiB 内存下载上限")
    _LOGGER.info(
        "Private archive ZIP generation completed: archive=%s zip_bytes=%d elapsed_seconds=%.3f",
        selected.name, len(data), time.monotonic() - started,
    )
    return data

def private_archive_zip_stream(replay_root, archive_directory) -> io.BytesIO:
    """Return a seekable stream for Streamlit's deferred-download API.

    The stream is created only when the user clicks Download, not during every
    page rerun. This also lets Streamlit surface packaging errors in the widget.
    """
    return io.BytesIO(build_private_archive_zip(replay_root, archive_directory))
