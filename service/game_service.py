"""进程内对局服务：独立推进生成器，界面只读取快照和提交控制命令。

不依赖 Streamlit，也不继承其会话上下文。服务进程退出后不自动恢复执行。
"""
from __future__ import annotations

import copy
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from engine import config
from storage.game_archive import archive_game
from ui.ui_runtime import rerun_delay


class _UsageSnapshot:
    def __init__(self, manager):
        self._usage = manager.get_usage_snapshot() if hasattr(manager, "get_usage_snapshot") else {}
        self._pending = bool(manager.has_pending())
        self._health = manager.get_health_snapshot() if hasattr(manager, "get_health_snapshot") else {}

    def get_usage_snapshot(self):
        return copy.deepcopy(self._usage)

    def has_pending(self):
        return self._pending

    def get_health_snapshot(self):
        return copy.deepcopy(self._health)


@dataclass(frozen=True)
class GameSnapshot:
    engine: object
    running: bool
    done: bool
    status: str
    error: str
    last_event_type: str
    waiting_since: float
    last_archive: dict | None
    archive_error: str
    runtime_diagnostics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class GameRuntimeSnapshot:
    """Small, lock-consistent service state for UI regions that do not need the engine."""
    running: bool
    done: bool
    status: str
    error: str
    last_event_type: str
    waiting_since: float
    last_archive: dict | None
    archive_error: str
    runtime_diagnostics: dict
    manager: object
    react_budget: int | None
    active_request_id: str | None
    pending_human_action: dict | None
    replay_logs: list
    human_action_integrity_error: str


class GameService:
    """同一对局的 next(gen)、控制、存档及快照共用一把锁。"""

    def __init__(self, engine, replay_directory, *, automatic=True, delay=0.6, turbo=False,
                 generator=None, record_display=False):
        self._engine = engine
        self._generator = None if engine.winner else (generator if generator is not None else engine.run())
        self._replay_directory = replay_directory
        self._condition = threading.Condition(threading.RLock())
        self._worker = None
        self._closed = False
        self._running = False
        self._done = bool(engine.winner)
        self._automatic = bool(automatic)
        self._delay = max(0.0, float(delay))
        self._turbo = bool(turbo)
        self._next_due = 0.0
        self._status = (f"游戏结束：{engine.winner}" if engine.winner else
                        f"已读档，将从第{engine.day + 1}夜继续" if getattr(engine, "_resume", False)
                        else "等待推进")
        self._error = ""
        self._last_event_type = ""
        self._waiting_since = 0.0
        self._last_progress_at = time.time()
        self._last_worker_heartbeat_at = 0.0
        self._worker_started_at = 0.0
        self._last_archive = None
        self._archive_error = ""
        self.revision = 0
        self.display_replay = None
        if record_display:
            from storage.replay_frames import DisplayReplay
            directory = engine.recorder.directory if engine.recorder else Path(replay_directory) / engine.game_id
            self.display_replay = DisplayReplay(engine.game_id, directory)
            self.display_replay.capture(engine)

    def configure(self, *, automatic, delay, turbo=False, react_budget=None):
        with self._condition:
            self._automatic = bool(automatic)
            self._delay = max(0.0, float(delay))
            self._turbo = bool(turbo)
            if react_budget is not None:
                self._engine.react_budget = max(0, int(react_budget))
            self._ensure_worker_locked()
            self._condition.notify_all()

    def play(self):
        with self._condition:
            if self._closed or self._done:
                return
            # A human action is an input boundary, not a paused AI step.
            # Resuming here previously started a worker yielding human_waiting
            # indefinitely and writing repeated events.
            if getattr(self._engine, "pending_human_action", None):
                return
            if not self._publish_display():
                return
            self._error = ""
            self.revision += 1
            self._running = True
            self._next_due = 0.0
            self._ensure_worker_locked()
            self._condition.notify_all()

    def pause(self):
        with self._condition:
            self.revision += 1
            self._running = False
            self._condition.notify_all()

    def retry_display_recording(self):
        """Retry committed display output even after game end, without advancing actions."""
        with self._condition:
            if self._closed:
                raise RuntimeError("对局服务已关闭")
            result = self._publish_display()
            if result:
                self._error = ""
            self.revision += 1
            return result

    def apply_model_settings(self, **settings):
        """只在干净暂停边界更换客户端，不推进生成器、不发网络请求。"""
        from agent.ai_agent import AgentManager

        with self._condition:
            if self._closed or self._done:
                raise RuntimeError("当前对局已结束")
            if self._running or self._engine.manager.has_pending():
                raise RuntimeError("请先暂停并等待后台请求完成；有待收集行动时先单步收集或跳过")
            previous = self._engine.manager
            candidate = AgentManager(**settings)
            try:
                candidate.load_state(copy.deepcopy(previous.export_state()))
                candidate.set_recorder(self._engine.recorder, self._engine._recording_context)
            except Exception:
                candidate.shutdown()
                raise
            self._engine.manager = candidate
            previous.shutdown()
            candidate._record_api(record_type="settings_applied", status="paused",
                                   provider=candidate.provider, thinking=candidate.thinking,
                                   reasoning_effort=candidate.reasoning_effort,
                                   good_model=candidate.good_model, wolf_model=candidate.wolf_model)
            return candidate.get_health_snapshot()

    def diagnose_connection(self):
        with self._condition:
            if self._closed or self._running or self._engine.manager.has_pending():
                raise RuntimeError("请先暂停并等待后台请求完成")
            return self._engine.manager.diagnose_connection()

    def step(self):
        with self._condition:
            if self._closed or self._done:
                return False
            if self._running and self._automatic:
                raise RuntimeError("请先暂停自动推演再单步")
            if getattr(self._engine, "pending_human_action", None):
                return False
            return self._advance_locked()

    def submit_human_action(self, action_id, payload):
        """校验并提交真人行动；提交后按当前自动/手动模式恢复推进。"""
        with self._condition:
            if self._closed or self._done:
                raise RuntimeError("对局已结束，无法提交行动")
            if not getattr(self._engine, "pending_human_action", None):
                raise RuntimeError("当前没有等待中的真人行动")
            self._engine.submit_human_action(action_id, payload)
            self.revision += 1
            self._error = ""
            self._condition.notify_all()
            if self._automatic:
                self._running = True
                self._next_due = 0.0
                self._ensure_worker_locked()
            else:
                self._advance_locked()
            return True

    def request_skip_current(self):
        with self._condition:
            if self._closed or self._done or not self._engine.request_skip_current():
                return False
            self._next_due = 0.0
            # 暂停时也立即收集取消结果，不等待网页或下一次“继续”。
            if not (self._running and self._automatic):
                self._advance_locked()
            self._condition.notify_all()
            return True

    def inject(self, seat, text):
        with self._condition:
            if self._closed or self._done:
                raise RuntimeError("对局已结束，无法传音")
            if seat not in self._engine.players:
                raise ValueError("无效的玩家座位")
            self._engine.manager.inject(seat, text)

    def snapshot(self):
        with self._condition:
            # 复制纯业务状态，排除客户端、Future、线程锁和取证器。
            # 多个界面可各自持有一份快照，渲染不与后台修改共享 list/dict。
            view = copy.copy(self._engine)
            for key, value in vars(self._engine).items():
                if key not in {"manager", "recorder"}:
                    setattr(view, key, copy.deepcopy(value))
            runtime = self._runtime_snapshot_locked(
                check_human_integrity=False,
                include_engine_fields=False,
            )
            view.manager = runtime.manager
            view.recorder = None
            return GameSnapshot(
                engine=view, running=runtime.running, done=runtime.done, status=runtime.status,
                error=runtime.error, last_event_type=runtime.last_event_type,
                waiting_since=runtime.waiting_since,
                last_archive=runtime.last_archive, archive_error=runtime.archive_error,
                runtime_diagnostics=runtime.runtime_diagnostics,
            )

    def runtime_snapshot(self):
        """Return current UI runtime state without deep-copying the game engine."""
        with self._condition:
            return self._runtime_snapshot_locked(
                check_human_integrity=True,
                include_engine_fields=True,
            )

    def _runtime_snapshot_locked(self, *, check_human_integrity, include_engine_fields):
        manager = self._engine.manager
        manager_view = _UsageSnapshot(manager)
        if hasattr(manager, "get_runtime_snapshot"):
            pending = manager.get_runtime_snapshot()
        else:
            has_pending = bool(manager.has_pending()) if hasattr(manager, "has_pending") else False
            pending = {
                "has_pending": has_pending,
                "pending_count": int(has_pending),
                "in_flight_count": int(has_pending),
                "pending_requests": [],
                "compression_pending_count": 0,
                "request_guard_count": 0,
                "transport_call_count": 0,
                "oldest_pending_seconds": None,
            }

        now = time.time()
        human_waiting = bool(getattr(self._engine, "pending_human_action", None))
        if self._closed:
            wait_kind = "closed"
        elif self._done:
            wait_kind = "finished"
        elif self._error:
            wait_kind = "error_paused"
        elif human_waiting:
            wait_kind = "human_input"
        elif self._running and pending.get("has_pending"):
            wait_kind = "model_request"
        elif self._running and self._automatic:
            wait_kind = "automatic_progress"
        elif self._running:
            wait_kind = "manual_progress"
        elif pending.get("has_pending"):
            wait_kind = "paused_with_pending_request"
        else:
            wait_kind = "paused"

        worker = self._worker
        worker_alive = bool(worker and worker.is_alive())
        worker_expected = bool(
            self._running and self._automatic and not self._done and not self._closed
        )
        last_progress_at = getattr(self, "_last_progress_at", 0.0)
        last_heartbeat_at = getattr(self, "_last_worker_heartbeat_at", 0.0)
        runtime_diagnostics = {
            "game_id": str(getattr(self._engine, "game_id", "")),
            "wait_kind": wait_kind,
            "running": self._running,
            "automatic": self._automatic,
            "done": self._done,
            "closed": self._closed,
            "worker_expected": worker_expected,
            "worker_alive": worker_alive,
            "worker_name": getattr(worker, "name", None),
            "worker_started_at": getattr(self, "_worker_started_at", 0.0) or None,
            "worker_heartbeat_at": last_heartbeat_at or None,
            "worker_heartbeat_age_seconds": (
                round(max(0.0, now - last_heartbeat_at), 1) if last_heartbeat_at else None
            ),
            "last_progress_at": last_progress_at or None,
            "last_progress_age_seconds": (
                round(max(0.0, now - last_progress_at), 1) if last_progress_at else None
            ),
            "waiting_since": self._waiting_since or None,
            "waiting_age_seconds": (
                round(max(0.0, now - self._waiting_since), 1) if self._waiting_since else None
            ),
            "last_event_type": self._last_event_type,
            "revision": self.revision,
            "pending": copy.deepcopy(pending),
            "error": self._error,
            "archive_error": self._archive_error,
        }
        return GameRuntimeSnapshot(
            running=self._running,
            done=self._done,
            status=self._status,
            error=self._error,
            last_event_type=self._last_event_type,
            waiting_since=self._waiting_since,
            last_archive=copy.deepcopy(self._last_archive),
            archive_error=self._archive_error,
            runtime_diagnostics=runtime_diagnostics,
            manager=manager_view,
            react_budget=getattr(self._engine, "react_budget", None),
            active_request_id=getattr(self._engine, "_active_request_id", None),
            pending_human_action=(
                copy.deepcopy(getattr(self._engine, "pending_human_action", None))
                if include_engine_fields else None
            ),
            replay_logs=(
                copy.deepcopy(getattr(self._engine, "replay_logs", []))
                if include_engine_fields else []
            ),
            human_action_integrity_error=(
                self._human_action_integrity_error() if check_human_integrity else ""
            ),
        )

    def save(self, directory):
        with self._condition:
            if self._closed:
                raise RuntimeError("对局已关闭")
            if (self._running and self._automatic) or self._engine.manager.has_pending():
                raise RuntimeError("请先暂停并等待后台任务完成后再存档")
            if getattr(self._engine, "pending_human_action", None):
                raise RuntimeError("请先提交当前真人行动，不能保存悬而未决的行动")
            target_dir = Path(directory)
            target_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            name = f"save_D{self._engine.day}_{stamp}_{uuid.uuid4().hex[:8]}.json"
            target = target_dir / name
            payload = {
                "version": "v6", "saved_at": stamp,
                "meta": {"day": self._engine.day, "phase": self._engine.phase,
                         "winner": self._engine.winner, "alive": len(self._engine.alive_seats())},
                "engine": self._engine.export_state(),
                "manager": self._engine.manager.export_state(),
            }
            temporary = target.with_suffix(".json.tmp")
            try:
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            return name

    def archive(self, reason="manual_archive"):
        with self._condition:
            self._archive_locked(reason)

    def close(self, reason="manual_reset"):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._running = False
            self._engine.manager.cancel_all_pending(reason)
            if self._generator is not None:
                self._generator.close()
                self._generator = None
            self._archive_locked(reason)
            if hasattr(self._engine.manager, "shutdown"):
                self._engine.manager.shutdown()
            self._condition.notify_all()

    def _ensure_worker_locked(self):
        if (not self._closed and not self._done and self._running and self._automatic
                and not getattr(self._engine, "pending_human_action", None)
                and self._worker is None):
            self._worker_started_at = time.time()
            self._last_worker_heartbeat_at = self._worker_started_at
            self._worker = threading.Thread(
                target=self._run, name=f"wolf-game-{self._engine.game_id}", daemon=True,
            )
            self._worker.start()

    def _run(self):
        current_thread = threading.current_thread()
        try:
            while True:
                with self._condition:
                    self._last_worker_heartbeat_at = time.time()
                    if self._closed or self._done or not self._running or not self._automatic:
                        self._worker = None
                        self._condition.notify_all()
                        return
                    remaining = self._next_due - time.monotonic()
                    if remaining > 0:
                        self._condition.wait(timeout=remaining)
                        continue
                    self._advance_locked()
                    self._next_due = time.monotonic() + rerun_delay(
                        self._last_event_type, self._delay, self._turbo,
                    )
        except Exception as error:
            # A scheduler/condition failure is outside _advance_locked's engine-error guard.
            # Fail closed instead of leaving running=True with no worker to make progress.
            with self._condition:
                if not self._closed and not self._done:
                    self._running = False
                    self._error = (
                        f"后台推进线程异常，已暂停：{type(error).__name__}: {error}"
                    )
                    self._status = "后台推进异常，已暂停"
                    self._last_event_type = "worker_error"
                    self.revision += 1
                if self._worker is current_thread:
                    self._worker = None
                self._condition.notify_all()
            config.get_logger().exception("后台推进线程意外退出：%s", error)
        finally:
            with self._condition:
                if self._worker is current_thread:
                    self._worker = None
                    self._condition.notify_all()

    def _publish_display(self):
        if self.display_replay is None:
            return True
        try:
            self.display_replay.capture(self._engine)
            return True
        except Exception:
            self.display_replay.issue = "展示记录写入中断；已暂停，保留已结算动作。"
            self._running = False
            self._error = self.display_replay.issue
            return False

    def _human_action_integrity_error(self):
        """Reject continued play if a configured human seat already has an AI-authored action."""
        human_seat = getattr(self._engine, "human_seat", None)
        if human_seat is None:
            return ""
        history = getattr(self._engine, "reasoning_log", {}).get(human_seat, [])
        for row in history:
            response = row.get("response") or {}
            if (row.get("actor_source") == "human" or response.get("_human_action")
                    or response.get("decision_source") == "human"):
                continue
            label = row.get("label", "未知行动")
            return (f"安全保护：真人席位 {int(human_seat):02d} 已出现非真人提交的行动“{label}”。"
                    "对局已暂停，避免继续由 AI 代打；请重置并重新开始一局。")
        return ""

    def _advance_locked(self):
        integrity_error = self._human_action_integrity_error()
        if integrity_error:
            self._running = False
            self._error = integrity_error
            self._status = "真人行动完整性检查失败，已暂停"
            self._last_event_type = "human_action_integrity_error"
            self.revision += 1
            config.get_logger().error(integrity_error)
            return False
        if self.display_replay is not None and self.display_replay.issue:
            if not self._publish_display():
                return False
        try:
            event = next(self._generator)
            self.revision += 1
            self._last_event_type = event.get("type", "")
            self._status = event.get("status", "")
            waiting_events = {"waiting", "compress_waiting", "human_waiting"}
            if self._last_event_type not in waiting_events:
                self._last_progress_at = time.time()
            if self._last_event_type == "human_input_required":
                # Do not hold the worker/service lock while waiting for a browser.
                self._running = False
                self._waiting_since = time.time()
            elif self._last_event_type == "human_waiting":
                # Defensive: an old caller must not be able to spin on this event.
                self._running = False
                if not self._waiting_since:
                    self._waiting_since = time.time()
            elif self._last_event_type in {"turn", "compress"}:
                self._waiting_since = time.time()
            elif self._last_event_type not in waiting_events:
                self._waiting_since = 0.0
            if self._last_event_type not in waiting_events:
                config.get_logger().info(f"STEP✓ {self._last_event_type} '{self._status}'")
            fatal = getattr(self._engine, "fatal_error", "")
            if fatal:
                self._engine.fatal_error = ""
                self._running = False
                self._error = f"API已暂停: {fatal}"
                self._status = "API 已暂停（查看诊断，应用设置后继续下一步）"
            if self._last_event_type not in waiting_events:
                self._publish_display()
            if self._last_event_type == "end":
                self._done = True
                self._running = False
                self._archive_locked("game_end")
            return True
        except StopIteration:
            self._done = True
            self._running = False
            self._status = "游戏结束"
            self._last_event_type = "game_over"
            self._archive_locked("stop_iteration")
            return True
        except Exception as error:
            self._running = False
            self._done = True
            self._error = f"引擎崩溃: {type(error).__name__}: {error}"
            self._status = "引擎崩溃"
            config.get_logger().exception(self._error)
            self._archive_locked("engine_error")
            return False

    def _archive_locked(self, reason):
        try:
            self._last_archive = archive_game(self._engine, self._replay_directory, reason=reason)
            self._archive_error = ""
        except Exception as error:
            self._archive_error = f"自动归档失败: {type(error).__name__}: {error}"
            config.get_logger().exception(self._archive_error)
