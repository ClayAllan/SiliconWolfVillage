# -*- coding: utf-8 -*-
import sys
import threading
from concurrent.futures import Future
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from engine import config
from agent.ai_agent import AgentManager
from engine.game_engine import GameEngine


class MemoryOnlyManager:
    def __init__(self):
        self.memory = {i: [] for i in range(1, 13)}
        self.pending = {}
        self.submitted = []
        self.ready = False

    def clear(self):
        self.memory = {i: [] for i in range(1, 13)}
        self.pending = {}

    def init_agent(self, seat, role, style, private_memory, roster=None):
        self.memory[seat].append(("init", role))

    def inject(self, seat, text):
        self.memory[seat].append(("inject", text))

    def set_truth(self, text):
        self.truth = text

    def set_hard_facts(self, seat, text):
        self.memory[seat].append(("hard_facts", text))

    def submit_ai_response(self, seat, prompt, response_kind="speech"):
        request_id = f"req-{seat}-{len(self.pending)}"
        self.pending[request_id] = (seat, response_kind)
        self.submitted.append((seat, response_kind, prompt))
        return request_id

    def poll_ai_response(self, request_id):
        if not self.ready:
            return False, None
        self.pending.pop(request_id, None)
        return True, {"reasoning": "完成", "speech": "过。", "vote_target": 0}

    def cancel_ai_response(self, request_id, reason=""):
        self.pending.pop(request_id, None)
        return {"reasoning": reason or "跳过", "speech": "过。", "vote_target": 0}

    def cancel_all_pending(self, reason=""):
        self.pending.clear()

    def compress_memories(self, seats):
        return {"compressed": 0, "fallback": 0, "total": 0}


class AsyncAgentTests(unittest.TestCase):
    def make_manager(self):
        manager = AgentManager(
            api_key="test-key",
            base_url="https://api.deepseek.com/v1",
            good_model="test-model",
            wolf_model="test-model",
            fallback_model="",
            max_retries=3,
            agent_timeout=1,
        )
        manager.init_agent(1, "平民", "冷静", "无")
        return manager

    def test_cancel_discards_late_result(self):
        manager = self.make_manager()
        gate = threading.Event()

        def delayed(*_args):
            gate.wait(2)
            return {"reasoning": "迟到结果", "speech": "不应写入", "vote_target": 0}

        manager._execute_response = delayed
        request_id = manager.submit_ai_response(1, "请发言")
        fallback = manager.cancel_ai_response(request_id, "测试跳过")
        gate.set()
        time.sleep(0.1)

        self.assertEqual(fallback["vote_target"], 0)
        serialized = str(manager.agent_memory[1]["recent"])
        self.assertIn("测试跳过", serialized)
        self.assertNotIn("迟到结果", serialized)
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_runtime_snapshot_reports_safe_pending_request_metadata(self):
        manager = self.make_manager()
        request_id = "private-prompt-request-123"
        future = Future()
        with manager._pending_lock:
            manager._pending[request_id] = {
                "future": future,
                "seat": 4,
                "kind": "speech",
                "started": time.monotonic() - 2.0,
                "prompt_text": "must never appear in diagnostics",
            }

        snapshot = manager.get_runtime_snapshot()
        self.assertTrue(snapshot["has_pending"])
        self.assertEqual(snapshot["pending_count"], 1)
        self.assertEqual(snapshot["in_flight_count"], 1)
        self.assertEqual(snapshot["pending_requests"][0]["request_id"], request_id[:8])
        self.assertEqual(snapshot["pending_requests"][0]["seat"], 4)
        self.assertGreaterEqual(snapshot["oldest_pending_seconds"], 1.5)
        self.assertNotIn("must never appear", str(snapshot))
        self.assertNotIn("prompt_text", str(snapshot))

        with manager._pending_lock:
            manager._pending.pop(request_id, None)
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_memory_compression_submission_is_nonblocking(self):
        manager = self.make_manager()
        gate = threading.Event()

        def slow_compression(_seats):
            gate.wait(2)
            return {"compressed": 1, "fallback": 0, "total": 1}

        manager.compress_memories = slow_compression
        started = time.monotonic()
        request_id = manager.submit_memory_compression([1])
        done, stats = manager.poll_memory_compression(request_id)
        self.assertFalse(done)
        self.assertIsNone(stats)
        self.assertLess(time.monotonic() - started, 0.2)

        gate.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            done, stats = manager.poll_memory_compression(request_id)
            if done:
                break
            time.sleep(0.01)
        self.assertTrue(done)
        self.assertEqual(stats["compressed"], 1)
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_http_402_is_not_retried_and_requests_pause(self):
        manager = self.make_manager()
        calls = []

        class BalanceError(Exception):
            status_code = 402

        def fail_once(*_args, **_kwargs):
            calls.append(1)
            raise BalanceError("insufficient balance")

        manager._create_with_hard_timeout = fail_once
        result = manager._execute_response(
            "request-402", 1, "speech", manager._build_messages(1), "test-model"
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual(result["vote_target"], 0)
        self.assertIn("402", result.get("_fatal_error", ""))
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_hard_facts_precede_subjective_summary(self):
        manager = self.make_manager()
        manager.set_hard_facts(1, "8号声称查验3号为好人")
        manager.agent_memory[1]["summary"] = "我是8号的金水。"
        messages = manager._build_messages(1)
        joined = "\n".join(m["content"] for m in messages)

        self.assertLess(joined.index("Python结构化事实账本"), joined.index("主观逻辑备忘录"))
        self.assertIn("冲突时以事实账本为准", joined)
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_short_recent_memory_skips_summary_api(self):
        manager = self.make_manager()
        manager.agent_memory[1]["recent"] = [{"role": "user", "content": "短记忆"}]

        def must_not_run(_seat):
            raise AssertionError("短期记忆未达阈值，不应调用摘要模型")

        manager._summarize_one = must_not_run
        stats = manager.compress_memories([1])
        self.assertEqual(stats, {"compressed": 0, "fallback": 0, "total": 0, "skipped": 1})
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_system_prompt_only_lists_roles_on_current_board(self):
        manager = self.make_manager()
        system = manager.agent_memory[1]["system"]["content"]
        self.assertNotIn("守墓人每夜", system)
        self.assertNotIn("骑士白天", system)
        self.assertNotIn("狼美人夜间", system)
        self.assertIn("反向金水", system)
        self.assertIn("警徽关系链", system)
        self.assertIn("代跳", system)
        self.assertIn("传话", system)
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_wolf_playbook_includes_explicit_social_tactics(self):
        engine = GameEngine(MemoryOnlyManager())
        engine.setup()
        wolf = engine.roles[engine.wolves[0]]
        private_memory = wolf.get_private_memory(engine)
        self.assertIn("反向金水", private_memory)
        self.assertIn("传话", private_memory)
        prompt = wolf.get_playbook_prompt(engine)
        self.assertIn("反向金水", prompt)
        self.assertIn("警徽链", prompt)
        self.assertIn("代跳", prompt)
        self.assertIn("传话", prompt)

    def test_token_usage_accumulates_and_survives_save_load(self):
        manager = self.make_manager()
        manager._accumulate_usage({
            "prompt_tokens": 100,
            "completion_tokens": 30,
            "total_tokens": 130,
            "prompt_cache_hit_tokens": 40,
            "prompt_cache_miss_tokens": 60,
            "completion_tokens_details": {"reasoning_tokens": 20},
        }, "speech")
        manager._accumulate_usage({
            "prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60,
        }, "memory_summary")
        snapshot = manager.get_usage_snapshot()
        self.assertEqual(snapshot["total_tokens"], 190)
        self.assertEqual(snapshot["reasoning_tokens"], 20)
        self.assertEqual(snapshot["summary_requests"], 1)
        self.assertEqual(snapshot["by_response_kind"]["speech"]["total_tokens"], 130)

        restored = self.make_manager()
        restored.load_state(manager.export_state())
        self.assertEqual(restored.get_usage_snapshot(), snapshot)
        manager._request_pool.shutdown(wait=True, cancel_futures=True)
        restored._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_parse_json_payload_resilience(self):
        manager = self.make_manager()
        # 1. 标准 JSON
        self.assertEqual(manager._parse_json_payload('{"speech": "hello", "vote_target": 3}'), {"speech": "hello", "vote_target": 3})
        # 2. Markdown 代码块包裹
        self.assertEqual(manager._parse_json_payload('```json\n{"speech": "hello"}\n```'), {"speech": "hello"})
        # 3. 前后带附带说明文字
        self.assertEqual(manager._parse_json_payload('这是我的决定：\n{"speech": "你好", "vote_target": 5}\n以上'), {"speech": "你好", "vote_target": 5})
        # 4. 轻微截断闭合
        repaired = manager._parse_json_payload('{"reasoning": "我认为1号是狼", "speech": "投1号')
        self.assertEqual(repaired.get("speech"), "投1号")
        manager._request_pool.shutdown(wait=True, cancel_futures=True)

    def test_max_tokens_for_kind_limits(self):
        self.assertGreaterEqual(AgentManager._max_tokens_for_kind("speech"), 1200)
        self.assertGreaterEqual(AgentManager._max_tokens_for_kind("vote"), 500)
        self.assertGreaterEqual(AgentManager._max_tokens_for_kind("memory_summary"), 1000)


class EngineRegressionTests(unittest.TestCase):
    def make_engine(self):
        engine = GameEngine(MemoryOnlyManager())
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        return engine

    def test_position_context_uses_real_daily_order(self):
        engine = self.make_engine()
        order = [7, 8, 10, 11, 12, 1]
        first = engine._position_context(7, order)
        middle = engine._position_context(10, order)
        last = engine._position_context(1, order)

        self.assertIn("7号→8号→10号→11号→12号→1号", first)
        self.assertIn("首置位", first)
        self.assertIn("第3/6位", middle)
        self.assertIn("末置位", last)
        self.assertIn("外置位", middle)
        self.assertIn("与座位号远近无关", middle)

    def test_ask_yields_while_waiting_and_can_be_skipped(self):
        engine = self.make_engine()
        gen = engine._ask(1, "测试", "测试行动", public=False)

        self.assertEqual(next(gen)["type"], "turn")
        self.assertEqual(next(gen)["type"], "waiting")
        self.assertTrue(engine.request_skip_current())
        self.assertEqual(next(gen)["type"], "done")
        with self.assertRaises(StopIteration):
            next(gen)
        self.assertFalse(engine.has_pending_action())
        self.assertEqual(engine._last_resp["vote_target"], 0)

    def test_all_real_deaths_go_through_single_entry_and_win_immediately(self):
        engine = self.make_engine()
        wolves = list(engine.wolves)
        engine._mark_dead_batch(wolves[:-1], "测试预处理")
        self.assertIsNone(engine.winner)

        changed = engine._mark_dead(wolves[-1], "测试最后一狼")
        self.assertTrue(changed)
        self.assertEqual(engine.winner, "好人")
        self.assertFalse(engine.players[wolves[-1]]["alive"])

    def test_batch_death_preserves_simultaneous_resolution(self):
        engine = self.make_engine()
        gods = [s for s, p in engine.players.items() if p["role"] in config.GOD_ROLES]
        changed = engine._mark_dead_batch(gods, "测试屠神")

        self.assertEqual(set(changed), set(gods))
        self.assertEqual(engine.winner, "狼人")

    def test_explode_only_accepts_structured_true(self):
        engine = self.make_engine()
        wolf = engine.wolves[0]
        engine._last_resp = {
            "is_explode": False,
            "speech": "如果我是狼早就自爆了，但我现在绝不自爆。",
        }
        self.assertFalse(engine._explode_requested(wolf))
        engine._last_resp["is_explode"] = True
        self.assertTrue(engine._explode_requested(wolf))

    def test_public_seer_claim_keeps_speaker_and_target_separate(self):
        engine = self.make_engine()
        engine.day = 1
        engine._log_player(8, "我是8号，预言家。昨晚查验3号，金水。")
        facts = engine._hard_facts_for(7)

        self.assertIn("8号声称自己是预言家", facts)
        self.assertIn("8号声称查验3号为好人", facts)
        self.assertIn("反向金水分析", facts)
        self.assertIn("被利用或甩锅的好人", facts)
        self.assertNotIn("7号为好人", facts)

    def test_badge_chain_with_confirmed_wolf_triggers_relationship_audit(self):
        engine = self.make_engine()
        engine.day = 3
        engine.badge_history = [
            {"day": 2, "from": 3, "to": 4},
            {"day": 2, "from": 4, "to": 9},
        ]
        engine._confirm_role(4, "狼人", "公开自爆")

        facts = engine._hard_facts_for(1)
        self.assertIn("关系审计警报", facts)
        self.assertIn("3号曾把警徽交给后来机械确认的狼人4号", facts)
        self.assertIn("机械确认的狼人4号曾把警徽交给9号", facts)
        self.assertIn("警徽关系链推理", facts)
        self.assertIn("故意甩锅", facts)
        self.assertIn("自爆只机械确认自爆者本人", facts)

    def test_consolidation_is_broadcast_as_non_binding_advice(self):
        engine = self.make_engine()
        speaker, target = engine.alive_seats()[:2]
        engine._last_resp = {"speech": f"归票{target}号"}

        self.assertEqual(engine._apply_consolidation(speaker), target)
        injections = [text for kind, text in engine.manager.memory[1] if kind == "inject"]
        joined = "\n".join(injections)
        self.assertIn("归票建议", joined)
        self.assertIn("仍须独立核验证据", joined)
        self.assertNotIn("好人请围绕它集火", joined)

    def test_good_audit_assignments_are_stable_and_diverse(self):
        engine = self.make_engine()
        first = [engine._good_audit_brief(s) for s in range(1, 5)]
        second = [engine._good_audit_brief(s) for s in range(1, 5)]

        self.assertEqual(first, second)
        self.assertEqual(len(set(first)), 4)

    def test_structured_explosion_events_are_idempotent(self):
        engine = self.make_engine()
        wolf = engine.wolves[0]
        engine.day = 2

        engine._record_explosion(wolf, "regular_day")
        engine._record_explosion(wolf, "regular_day")
        self.assertEqual(len(engine.explosion_events), 1)
        self.assertEqual(engine.explosion_events[0]["seat"], wolf)

    def test_guard_replay_distinguishes_empty_dead_and_absent(self):
        engine = self.make_engine()
        guard = engine._seat_with_role("守卫")
        engine.night = {"guard_target": None}
        self.assertIn("本夜空守", engine._guard_replay_line())

        engine.players[guard]["alive"] = False
        self.assertIn("守卫已出局", engine._guard_replay_line())

        no_guard = GameEngine(MemoryOnlyManager())
        no_guard.setup(config.GAME_BOARDS["12人丘比特"])
        no_guard.night = {"guard_target": None}
        self.assertIn("本局无守卫", no_guard._guard_replay_line())

    def test_reactive_window_asks_only_one_wolf_per_threat(self):
        engine = self.make_engine()
        engine.manager.ready = True
        engine._react_left = 3
        list(engine._maybe_react_window([(1, "我查杀2号，建议投2号")], exclude=1))

        wolf_calls = [item for item in engine.manager.submitted if item[0] in engine.wolves]
        self.assertEqual(len(wolf_calls), 1)


    def test_all_numbers_does_not_split_large_numbers_into_seats(self):
        from engine.roles import _all_numbers, parse_seat
        text = "我有100%把握，2026年，归票7号"
        nums = _all_numbers(text)
        self.assertEqual(nums, [7])
        self.assertEqual(parse_seat(text, {3, 7, 10}), 7)

    def test_consolidation_excludes_speaker_seat(self):
        engine = self.make_engine()
        engine._last_resp = {"speech": "我是12号警长，今天大家听我归票，全票出7号"}
        target = engine._apply_consolidation(12)
        self.assertEqual(target, 7)

    def test_hunter_does_not_shoot_on_passing_mention(self):
        from engine.roles import parse_hunter
        cands = [3, 5, 8]
        # 仅加油/提及，不应开枪
        self.assertIsNone(parse_hunter("天哪我倒了，好人加油，预言家3号你要挺住", cands))
        # 明确动作，应当开枪
        self.assertEqual(parse_hunter("临死开枪带走3号", cands), 3)

    def test_wolf_beauty_cannot_explode(self):
        wb_engine = GameEngine(MemoryOnlyManager())
        wb_engine.setup(config.GAME_BOARDS["12人狼美骑士"])
        wb_seat = wb_engine._seat_with_role("狼美人")
        wb_engine._last_resp = {"is_explode": True}
        self.assertFalse(wb_engine._explode_requested(wb_seat))

    def test_ghost_rider_cannot_explode_and_reflects_seer_and_poison(self):
        gr_engine = GameEngine(MemoryOnlyManager())
        gr_engine.setup(config.GAME_BOARDS["12人摄梦人恶灵骑士"])
        gr_seat = gr_engine._seat_with_role("恶灵骑士")
        seer_seat = gr_engine._seat_with_role("预言家")
        witch_seat = gr_engine._seat_with_role("女巫")
        
        # 1) 恶灵骑士不可自爆
        gr_engine._last_resp = {"is_explode": True}
        self.assertFalse(gr_engine._explode_requested(gr_seat))
        
        # 2) 狼刀恶灵骑士免疫
        gr_engine.night = {"wolf_kill": gr_seat}
        deaths = gr_engine.settle_night()
        self.assertNotIn(gr_seat, deaths)

        # 3) 预言家查验恶灵骑士 → 预言家被反伤暴毙
        gr_role = gr_engine.roles[gr_seat]
        self.assertFalse(gr_role.has_reflected)
        gr_engine.night = {"seer_check": (gr_seat, "狼人")}
        deaths = gr_engine.settle_night()
        self.assertEqual(deaths.get(seer_seat), "reflect")
        self.assertTrue(gr_role.has_reflected)

        # 4) 已反伤过一次后，女巫再下毒不再反伤，但恶灵骑士仍免疫毒杀
        gr_engine.night = {"witch_poison": gr_seat}
        deaths = gr_engine.settle_night()
        self.assertNotIn(gr_seat, deaths)
        self.assertNotIn(witch_seat, deaths)

    def test_dreamer_protects_target_and_kills_on_consecutive_nights(self):
        d_engine = GameEngine(MemoryOnlyManager())
        d_engine.setup(config.GAME_BOARDS["12人摄梦人恶灵骑士"])
        d_seat = d_engine._seat_with_role("摄梦人")
        target_seat = (set(range(1, 13)) - {d_seat}).pop()

        # 1) 摄梦人守护：免受狼刀
        d_engine.night = {"wolf_kill": target_seat, "dream_target": target_seat}
        d_engine.roles[d_seat].dream_history = [target_seat]
        deaths = d_engine.settle_night()
        self.assertNotIn(target_seat, deaths)

        # 2) 连续两晚摄梦同一人 → 目标在第二晚暴毙
        d_engine.roles[d_seat].dream_history = [target_seat, target_seat]
        d_engine.night = {"dream_target": target_seat}
        deaths = d_engine.settle_night()
        self.assertEqual(deaths.get(target_seat), "dream_overdose")

        # 3) 摄梦人夜死 → 梦游者一同殉情
        d_engine.roles[d_seat].dream_history = [target_seat]
        d_engine.night = {"wolf_kill": d_seat, "dream_target": target_seat}
        deaths = d_engine.settle_night()
        self.assertEqual(deaths.get(d_seat), "wolf")
        self.assertEqual(deaths.get(target_seat), "dream_link")

    def test_magician_swaps_targets(self):
        m_engine = GameEngine(MemoryOnlyManager())
        m_engine.setup(config.GAME_BOARDS["12人魔术师白狼王"])
        m_seat = m_engine._seat_with_role("魔术师")
        # 狼刀目标为 5号，但魔术师将 5号 与 8号 交换，导致 8号 倒牌
        m_engine.night = {"wolf_kill": 5, "magician_swap": (5, 8)}
        deaths = m_engine.settle_night()
        self.assertNotIn(5, deaths)
        self.assertEqual(deaths.get(8), "wolf")

    def test_unmeasured_tactical_scores_are_not_invented(self):
        from storage.game_archive import _strategic_analysis
        fake_engine = GameEngine(MemoryOnlyManager())
        fake_engine.setup(config.GAME_BOARDS["12人标准预女猎守"])
        fake_metrics = {
            "vote_rounds": [
                {
                    "title": "D1 放逐投票",
                    "totals": {1: 5.0, 2: 3.0},
                    "votes": {3: 1, 4: 1, 5: 1, 6: 1, 7: 1, 8: 2, 9: 2, 10: 2},
                    "winner": 1,
                }
            ],
            "final_alive": [2, 3, 4, 5, 6, 7],
            "final_dead": [1, 8, 9, 10, 11, 12],
        }
        roles = {str(i): fake_engine.players[i]["role"] for i in range(1, 13)}
        analysis = _strategic_analysis(fake_engine, fake_metrics, roles)
        self.assertIn("tactical_radar", analysis)
        self.assertIsNone(analysis["tactical_radar"])
        self.assertEqual(analysis["metrics_schema_version"], 2)
        self.assertTrue(analysis["observed_metrics"])

    def test_every_board_counts_each_surviving_god_before_slaughter(self):
        from engine.roles import ROLE_CLASSES
        for name, board in config.GAME_BOARDS.items():
            for god_name in set(board):
                if not ROLE_CLASSES[god_name].is_god:
                    continue
                with self.subTest(board=name, god=god_name):
                    engine = GameEngine(MemoryOnlyManager())
                    engine.setup(board)
                    god = next(s for s, p in engine.players.items() if p["role"] == god_name)
                    wolf = engine.wolves[0]
                    villager = next(s for s, p in engine.players.items() if p["role"] == "平民")
                    for seat, player in engine.players.items():
                        player["alive"] = seat in {god, wolf, villager}
                    self.assertIsNone(engine.check_winner())
                    engine.players[god]["alive"] = False
                    self.assertEqual(engine.check_winner(), "狼人")
                    engine.players[god]["alive"] = True
                    engine.players[wolf]["alive"] = False
                    self.assertEqual(engine.check_winner(), "好人")

    def test_invalid_board_is_rejected_before_existing_state_is_changed(self):
        engine = self.make_engine()
        before = dict(engine.players)
        for board in ([], ["平民"] * 11, ["未知"] * 12, ["狼人"] * 12,
                      ["狼人"] + ["平民"] * 11,
                      ["狼人"] * 4 + ["平民"] * 4 + ["预言家"] * 4):
            with self.subTest(board=board), self.assertRaises(ValueError):
                engine.setup(board)
            self.assertEqual(engine.players, before)


class CircuitBreakerTests(unittest.TestCase):
    def test_three_failed_requests_trip_breaker(self):
        manager = AgentManager(
            api_key="test-key", base_url="https://api.deepseek.com/v1",
            good_model="test-model", wolf_model="test-model", fallback_model="",
            max_retries=1, agent_timeout=1,
        )
        manager.init_agent(1, "平民", "冷静", "无")

        def fail(*_args, **_kwargs):
            raise ConnectionError("offline")

        manager._create_with_hard_timeout = fail
        results = [
            manager._execute_response(
                f"request-{i}", 1, "speech", manager._build_messages(1), "test-model"
            )
            for i in range(3)
        ]
        self.assertNotIn("_fatal_error", results[0])
        self.assertNotIn("_fatal_error", results[1])
        self.assertIn("熔断", results[2]["_fatal_error"])
        self.assertEqual(manager.api_stats["circuit_breaks"], 1)
        manager._request_pool.shutdown(wait=True, cancel_futures=True)


if __name__ == "__main__":
    unittest.main()
