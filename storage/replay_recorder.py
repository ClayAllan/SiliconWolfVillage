# -*- coding: utf-8 -*-
"""V2 对局取证记录器：实时追加 JSONL，终局原子写入快照与摘要。"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path


SCHEMA_VERSION = 2


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def safe_stamp(value: str) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits[:14] or datetime.now().strftime("%Y%m%d%H%M%S")


class GameRecorder:
    """每局独立目录；JSONL 每写一条就 flush，尽量保住异常退出前的数据。"""

    STREAMS = {
        "events": "events.jsonl",
        "api_calls": "api_calls.jsonl",
        "memory": "memory.jsonl",
    }

    def __init__(self, root, game_id: str, started_at: str, basename: str | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.game_id = str(game_id)
        self.started_at = str(started_at)
        self.basename = basename or f"{safe_stamp(started_at)}_{game_id}"
        self.directory = self.root / self.basename
        self.directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._seq = {name: self._recover_seq(filename) for name, filename in self.STREAMS.items()}

    def _recover_seq(self, filename: str) -> int:
        path = self.directory / filename
        if not path.exists():
            return 0
        last = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        last = int(json.loads(line).get("seq", last))
        except Exception:
            # 尾行若因断电损坏，不妨碍继续追加；复盘读取器会逐行容错。
            pass
        return last

    def _append(self, stream: str, payload: dict) -> dict:
        if stream not in self.STREAMS:
            raise ValueError(f"未知记录流: {stream}")
        with self._lock:
            self._seq[stream] += 1
            row = {
                "schema_version": SCHEMA_VERSION,
                "seq": self._seq[stream],
                "timestamp": now_iso(),
                "game_id": self.game_id,
            }
            row.update(payload)
            path = self.directory / self.STREAMS[stream]
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=str))
                handle.write("\n")
                handle.flush()
            return row

    def record_event(self, event: str, **payload) -> dict:
        return self._append("events", {"event": event, **payload})

    def record_api(self, **payload) -> dict:
        return self._append("api_calls", payload)

    def record_memory(self, event: str, **payload) -> dict:
        return self._append("memory", {"event": event, **payload})

    def log(self, level: str, message: str) -> None:
        line = f"{now_iso()} [{level.upper()}] game={self.game_id} {message}\n"
        with self._lock:
            with (self.directory / "runtime.log").open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(line)
                handle.flush()

    def read_jsonl(self, stream: str) -> list[dict]:
        path = self.directory / self.STREAMS[stream]
        rows = []
        if not path.exists():
            return rows
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    if line.strip():
                        rows.append(json.loads(line))
                except json.JSONDecodeError:
                    rows.append({"corrupt_line": line.rstrip("\r\n")})
        return rows

    def write_json(self, filename: str, payload: dict) -> Path:
        path = self.directory / filename
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        with self._lock:
            with tmp.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
                handle.write("\n")
            tmp.replace(path)
        return path

    def write_text(self, filename: str, text: str) -> Path:
        path = self.directory / filename
        tmp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
        with self._lock:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
        return path

    def paths(self) -> dict[str, str]:
        return {
            "directory": str(self.directory),
            "summary": str(self.directory / "summary.md"),
            "manifest": str(self.directory / "manifest.json"),
            "session": str(self.directory / "session.json"),
            "final_state": str(self.directory / "final_state.json"),
            **{name: str(self.directory / filename) for name, filename in self.STREAMS.items()},
            "runtime": str(self.directory / "runtime.log"),
        }
