"""Append-only display recording, separate from engine saves and raw API evidence."""
import copy
import hashlib
import json
from pathlib import Path
import secrets
import threading

from service.room_view import project_room


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DisplayReplay:
    MAX_FRAMES = 20000
    MAX_BYTES = 128 * 1024 * 1024

    def __init__(self, game_id, directory=None):
        self.game_id = game_id
        self.directory = Path(directory) if directory else None
        self.items = {}
        self.timelines = {k: [] for k in ["public", "omniscient", *(f"seat:{s}" for s in range(1, 13))]}
        self._cursor_indexes = {key: {} for key in self.timelines}
        self.frames = 0
        self.bytes = 0
        self.issue = ""
        self._pending = None
        self._sequence_nodes = {}
        self._row_chain_cache = {}
        self._lock = threading.RLock()
        if self.directory:
            self.directory.mkdir(parents=True, exist_ok=True)
            if (self.directory / "replay_frames.jsonl").exists():
                raise ValueError("本局已有展示记录，请读取原记录，不能覆盖")

    def _append(self, name, value):
        if not self.directory:
            return
        data = (canonical(value) + "\n").encode("utf-8")
        if self.bytes + len(data) > self.MAX_BYTES:
            raise OSError("展示记录达到容量上限")
        path = self.directory / name
        with path.open("ab") as handle:
            offset = handle.tell()
            try:
                handle.write(data)
                handle.flush()
            except Exception:
                # Only discard this process's uncommitted partial append.
                handle.seek(offset)
                handle.truncate()
                raise
        self.bytes += len(data)

    def _intern(self, value):
        key = hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
        if key not in self.items:
            self._append("replay_items.jsonl", {"schema": 1, "game_id": self.game_id, "id": key, "value": value})
            self.items[key] = copy.deepcopy(value)
        return key

    def _pack(self, view, audience, engine):
        view = copy.deepcopy(view)
        view["public_log"] = self._pack_rows(
            view["public_log"], (audience, "public"), engine.public_log
        )
        reasoning_sources = getattr(engine, "reasoning_log", {}) or {}
        view["reasoning_log"] = {
            seat: self._pack_rows(
                rows, (audience, "reasoning", seat), reasoning_sources.get(int(seat), [])
            )
            for seat, rows in view["reasoning_log"].items()
        }
        return self._intern(view)

    def _pack_rows(self, rows, cache_key=None, source_id=None):
        # Reuse an immutable prefix for append-only engine logs. A replacement list
        # (for example after load_state) invalidates the cache and rebuilds safely.
        cached = self._row_chain_cache.get(cache_key) if cache_key is not None else None
        if cached and cached["source"] is source_id and cached["length"] <= len(rows):
            start, tail = cached["length"], cached["tail"]
        else:
            start, tail = 0, None
        for row in rows[start:]:
            entry = self._intern(row)
            pair = (tail, entry)
            if pair not in self._sequence_nodes:
                self._sequence_nodes[pair] = self._intern({"previous": tail, "entry": entry})
            tail = self._sequence_nodes[pair]
        if cache_key is not None:
            self._row_chain_cache[cache_key] = {
                "source": source_id, "length": len(rows), "tail": tail,
            }
        return tail

    def _unpack_rows(self, tail):
        rows, seen = [], set()
        while tail is not None:
            if tail in seen:
                raise ValueError("循环展示引用")
            seen.add(tail)
            node = self.items[tail]
            row = self.items[node["entry"]]
            if not isinstance(row, dict):
                raise ValueError("无效展示行")
            rows.append(copy.deepcopy(row))
            tail = node["previous"]
        return list(reversed(rows))

    def capture(self, engine):
        with self._lock:
            return self._capture_locked(engine)

    def _capture_locked(self, engine):
        if self.frames >= self.MAX_FRAMES:
            raise OSError("展示帧达到容量上限")
        if self._pending is None:
            self._pending = {key: project_room(engine, *(["seat", int(key.split(":")[1])] if key.startswith("seat:") else [key])) for key in self.timelines}
        changes = {}
        for audience, view in self._pending.items():
            ref = self._pack(view, audience, engine)
            old = self.timelines[audience]
            if not old or old[-1]["ref"] != ref:
                changes[audience] = {"cursor": secrets.token_urlsafe(18), "ref": ref}
        if changes:
            frame = {"schema": 1, "game_id": self.game_id, "frame_seq": self.frames + 1, "changes": changes}
            self._append("replay_frames.jsonl", frame)
            for audience, entry in changes.items():
                timeline = self.timelines[audience]
                timeline.append(entry)
                self._cursor_indexes[audience][entry["cursor"]] = len(timeline) - 1
            self.frames += 1
        if self.directory:
            manifest = {"replay_schema_version": 1, "game_id": self.game_id,
                        "frames": self.frames, "coverage": "publication_boundaries",
                        "files": ["replay_frames.jsonl", "replay_items.jsonl"]}
            target = self.directory / "replay_manifest.json"
            temporary = target.with_suffix(".json.tmp")
            temporary.write_text(canonical(manifest), encoding="utf-8")
            temporary.replace(target)
        self._pending = None
        self.issue = ""

    def read(self, audience, cursor=None):
        with self._lock:
            return self._read_locked(audience, cursor)

    def _read_locked(self, audience, cursor=None):
        timeline = self.timelines[audience]
        if not timeline:
            raise ValueError("暂无可读取的展示记录")
        if cursor is None:
            entry = timeline[-1]
        else:
            index = self._cursor_indexes[audience].get(cursor)
            if index is None:
                raise ValueError("该视角没有此回放位置")
            entry = timeline[index]
        result = copy.deepcopy(self.items[entry["ref"]])
        result["public_log"] = self._unpack_rows(result["public_log"])
        result["reasoning_log"] = {s: self._unpack_rows(tail) for s, tail in result["reasoning_log"].items()}
        result["cursor"] = entry["cursor"]
        return result

    def _timeline_since_locked(self, audience, start=0):
        timeline = self.timelines[audience]
        if type(start) is not int or start < 0 or start > len(timeline):
            start = 0
        return [{"cursor": row["cursor"], "day": self.items[row["ref"]]["day"],
                 "phase": self.items[row["ref"]]["phase"]} for row in timeline[start:]]

    def timeline(self, audience):
        with self._lock:
            return self._timeline_since_locked(audience, 0)

    def read_packet(self, audience, cursor=None, *, timeline_from=None, include_view=True):
        """Read an authorized view and only the timeline suffix the caller has not cached."""
        with self._lock:
            length = len(self.timelines[audience])
            start = 0 if timeline_from is None else timeline_from
            timeline = self._timeline_since_locked(audience, start)
            view = self._read_locked(audience, cursor) if include_view else None
            return {
                "view": view,
                "timeline": timeline,
                "timeline_revision": length,
                "issue": self.issue,
            }

    @classmethod
    def load(cls, directory):
        """Local, owner-selected path only. Stop at the first invalid commit."""
        directory = Path(directory)
        replay = cls("")
        for name in ("replay_items.jsonl", "replay_frames.jsonl"):
            path = directory / name
            if not path.is_file() or path.stat().st_size > cls.MAX_BYTES:
                raise ValueError("展示文件缺失或过大")
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                        if row["schema"] != 1:
                            raise ValueError("未知展示格式")
                        replay.game_id = replay.game_id or row["game_id"]
                        if row["game_id"] != replay.game_id:
                            raise ValueError("跨局展示数据")
                        if name == "replay_items.jsonl":
                            if hashlib.sha256(canonical(row["value"]).encode("utf-8")).hexdigest() != row["id"]:
                                raise ValueError("展示内容校验失败")
                            replay.items[row["id"]] = row["value"]
                        else:
                            if row["frame_seq"] != replay.frames + 1 or replay.frames >= cls.MAX_FRAMES:
                                raise ValueError("展示帧不连续")
                            for key, entry in row["changes"].items():
                                if key not in replay.timelines or not isinstance(entry["cursor"], str):
                                    raise ValueError("无效受众")
                                view = replay.items[entry["ref"]]
                                expected = f"seat:{view['viewer_seat']}" if view["view"] == "seat" else view["view"]
                                if expected != key or view["game_id"] != replay.game_id:
                                    raise ValueError("展示视角绑定不符")
                                replay._unpack_rows(view["public_log"])
                                for tail in view["reasoning_log"].values():
                                    replay._unpack_rows(tail)
                                if any(r["cursor"] == entry["cursor"] for r in replay.timelines[key]):
                                    raise ValueError("重复展示游标")
                            for key, entry in row["changes"].items():
                                replay.timelines[key].append(entry)
                            replay.frames += 1
                    except (ValueError, KeyError, TypeError):
                        replay.issue = "记录存在缺口，仅展示已校验的前缀。"
                        break
        return replay
