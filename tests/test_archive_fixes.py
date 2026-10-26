"""归档缺陷回归：只使用本地响应与模拟玩家，禁止付费请求。"""
import json
import ast
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from engine import config
from agent.ai_agent import AgentManager
from storage.game_archive import game_statistics, _usage_totals, _source_snapshot, _sanitize_base_url
from engine.game_engine import GameEngine
from agent.model_protocol import parse_action, validate_content, ResponseProtocolError, request_options
from agent.public_memory import select_evidence
from engine.roles import parse_yes_save


WITCH_SPEECH = "救。7号首夜被刀，我决定使用解药救他，先保一个信息位。若不救，好人直接少一人，信息也更少。先救。"
GRAVE_SPEECH = "我跳守墓人，硬信息：5号真实阵营是好人。我昨天投5是站错队，我认。今天我票9号。"


def response(content, finish="stop"):
    return NS(id="offline", model="offline", choices=[NS(finish_reason=finish,
               message=NS(content=content, reasoning_content=""))],
              usage={"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20})


class RulesManager:
    """按合法名单生成确定性测试行动；底牌访问仅限测试夹具。"""
    def __init__(self):
        self.contracts, self.requests, self.memory = {}, {}, {}
        self.evidence = {}

    def clear(self):
        self.memory = {}

    def init_agent(self, seat, *args, **kwargs):
        self.memory[seat] = []

    def inject(self, seat, text):
        self.memory[seat].append(text)

    def set_truth(self, text):
        pass

    def set_hard_facts(self, seat, text):
        pass

    def set_action_context(self, seat, context):
        self.contracts[seat] = context

    def set_public_evidence(self, seat, entries):
        self.evidence[seat] = entries

    def submit_ai_response(self, seat, prompt, response_kind="speech"):
        key = str(len(self.requests))
        self.requests[key] = (seat, response_kind)
        return key

    def poll_ai_response(self, key):
        seat, kind = self.requests[key]
        context = self.contracts[seat]
        allowed = context.get("allowed_targets", [])
        positive = [s for s in allowed if s]
        wolf = self.engine.roles[seat].camp == "狼人"
        preferred = [s for s in positive if (self.engine.roles[s].camp == "狼人") != wolf]
        target = (preferred or positive or [0])[0]
        result = {"speech": "我依据已知信息继续观察。", "reasoning": "离线规则夹具", "decision_source": "model"}
        if kind in {"vote", "check", "poison", "hunt", "charm", "dream"}:
            result["vote_target"] = target
        elif kind == "guard":
            result["vote_target"] = 0
        elif kind == "consolidation":
            result["vote_target"] = target
            result["speech"] = f"归票{target}号"
        elif kind == "antidote":
            result["use_antidote"] = True
        elif kind in {"link", "swap"}:
            result["targets"] = positive[:2]
        elif kind == "join_police":
            result["join_police"] = self.engine.roles[seat].role_name == "预言家"
        elif kind == "withdraw":
            result["withdraw"] = False
        elif kind == "badge_target":
            result["badge_target"] = target
        elif kind == "order":
            result["speech"] = "警右"
        elif kind == "react_explode":
            result["is_explode"] = False
        elif kind == "react_duel":
            result["is_duel"] = False
        parse_action(json.dumps(result, ensure_ascii=False), kind, context)
        return True, result

    def compress_memories(self, seats):
        return {"compressed": 0, "fallback": 0, "total": 0}

    def submit_memory_compression(self, seats):
        return "offline-compression"

    def poll_memory_compression(self, request_id):
        return True, self.compress_memories([])


class ArchiveFixTests(unittest.TestCase):
    def test_archive_base_url_redacts_userinfo_query_and_fragment(self):
        self.assertEqual(
            _sanitize_base_url("https://user:super-secret@example.com:8443/v1?token=query-secret#frag"),
            "https://example.com:8443/v1",
        )
        self.assertEqual(_sanitize_base_url("https://api.deepseek.com/v1"), "https://api.deepseek.com/v1")
        self.assertEqual(_sanitize_base_url("not a URL"), "")

    def setUp(self):
        network = patch("openai.resources.chat.completions.Completions.create",
                        side_effect=AssertionError("离线回归禁止模型网络"))
        network.start()
        self.addCleanup(network.stop)

    def manager(self):
        manager = AgentManager(api_key="offline", base_url="https://api.deepseek.com",
                               agent_timeout=2, max_retries=2, fallback_model="")
        manager.init_agent(1, "平民", "冷静", "无")
        self.addCleanup(manager.shutdown)
        return manager

    def engine(self, board="12人标准预女猎守"):
        manager = RulesManager()
        engine = GameEngine(manager)
        engine.setup(config.GAME_BOARDS[board])
        manager.engine = engine
        engine.phase, engine.day = "第1夜", 1
        engine.night = {}
        return engine

    def test_witch_original_counterfactual_does_not_reverse_decision(self):
        self.assertTrue(parse_yes_save(WITCH_SPEECH))
        self.assertFalse(parse_yes_save("不救。若救了自刀狼就会浪费药。"))

    def test_witch_structured_decision_controls_actual_settlement(self):
        engine = self.engine()
        witch = engine.roles[engine._seat_with_role("女巫")]
        victim = next(s for s in engine.alive_seats() if engine.players[s]["role"] == "平民")
        engine.night = {"wolf_kill": victim}
        witch._pending = "antidote"
        witch.parse_night_response(engine, {"use_antidote": True, "speech": "不救只是反例"})
        self.assertEqual(engine.night["witch_save"], victim)
        self.assertNotIn(victim, engine.settle_night())
        self.assertFalse(witch.antidote_available)
        witch._pending = "poison"
        witch.parse_night_response(engine, {"vote_target": engine.wolves[0]})
        self.assertNotIn("witch_poison", engine.night)

    def test_witch_cannot_save_self_after_first_night_or_spend_twice(self):
        engine = self.engine()
        witch = engine.roles[engine._seat_with_role("女巫")]
        engine.day = 2
        engine.night = {"wolf_kill": witch.seat}
        witch._pending = "antidote"
        witch.parse_night_response(engine, {"use_antidote": True})
        self.assertNotIn("witch_save", engine.night)
        self.assertTrue(witch.antidote_available)

    def test_missing_public_speech_rejected_but_vote_accepted(self):
        raw = '{"reasoning":"9号可疑","vote_target":9}'
        with self.assertRaises(ResponseProtocolError):
            parse_action(raw, "speech")
        self.assertEqual(parse_action(raw, "vote")["vote_target"], 9)

    def test_speech_repair_does_not_publish_private_reasoning(self):
        manager = self.manager()
        manager._create_with_hard_timeout = Mock(side_effect=[response('{"reasoning":"私密判断","vote_target":9}'),
                                                            response('{"speech":"公开表态"}')])
        result = manager._execute_response("local", 1, "speech", [], manager.good_model)
        self.assertEqual(result["speech"], "公开表态")
        self.assertEqual(manager._create_with_hard_timeout.call_count, 2)
        self.assertEqual(manager._create_with_hard_timeout.call_args.kwargs["extra_body"]["thinking"]["type"], "disabled")

    def test_illegal_target_gets_repaired_before_execution(self):
        manager = self.manager()
        manager._create_with_hard_timeout = Mock(side_effect=[response('{"vote_target":4}'), response('{"vote_target":9}')])
        result = manager._execute_response("local", 1, "vote", [], manager.good_model, {"allowed_targets": [0, 9]})
        self.assertEqual(result["vote_target"], 9)
        self.assertEqual(manager._create_with_hard_timeout.call_count, 2)

    def test_guard_structured_zero_does_not_pick_number_in_explanation(self):
        engine = self.engine()
        guard = engine.roles[engine._seat_with_role("守卫")]
        guard.parse_night_response(engine, {"vote_target": 0, "speech": "本来考虑守3号，但最终空守"})
        self.assertIsNone(engine.night["guard_target"])
        target = next(s for s in engine.alive_seats() if s != guard.seat)
        guard.parse_night_response(engine, {"vote_target": target, "speech": "说明中提及别的号码"})
        self.assertEqual(engine.night["guard_target"], target)
        with self.assertRaises(ResponseProtocolError):
            parse_action(json.dumps({"vote_target": target}), "guard", engine._action_context(guard.seat, "guard"))

    def test_malformed_or_illegal_skill_flags_rejected(self):
        for raw, kind, context in [('{"use_antidote":"true"}', "antidote", {}),
                                    ('{"targets":[3,3]}', "swap", {}),
                                    ('{"targets":[3,7]}', "link", {"allowed_targets": [3,4]}),
                                    ('{"speech":"爆","is_explode":true}', "speech", {"allowed_flags": []}),
                                    ('{"is_duel":true,"duel_target":0}', "react_duel", {"allowed_flags":["is_duel"],"skill_targets":[3]})]:
            with self.subTest(raw=raw), self.assertRaises(ResponseProtocolError):
                parse_action(raw, kind, context)

    def test_dead_speaker_public_claim_survives_day_change(self):
        engine = self.engine("12人白狼王守墓人")
        engine.phase, engine.day = "第2天", 2
        engine._log_player(10, GRAVE_SPEECH)
        engine._log_god("私密信息绝不可进公屏证词")
        engine.players[10]["alive"] = False
        engine.phase, engine.day = "第3天", 3
        manager = self.manager()
        manager.set_public_evidence(1, engine._public_evidence())
        joined = json.dumps(manager._build_messages(1), ensure_ascii=False)
        self.assertIn("5号真实阵营是好人", joined)
        self.assertIn("未经身份验证", joined)
        self.assertNotIn("私密信息绝不可", joined)
        self.assertNotIn('"role": "守墓人"', joined)

    def test_public_memory_survives_save_restore_without_raw_roles(self):
        manager = self.manager()
        entries = [{"id":1,"seat":10,"phase":"第2天","text":GRAVE_SPEECH}]
        manager.set_public_evidence(1, entries)
        saved = manager.export_state()
        restored = self.manager()
        restored.load_state(saved)
        self.assertIn(GRAVE_SPEECH, json.dumps(restored._build_messages(1), ensure_ascii=False))

    def test_public_memory_budget_reports_omissions(self):
        rows = [{"id":i,"seat":i,"phase":"第1天","text":"公开发言"*20} for i in range(1,13)]
        text, ids, omitted = select_evidence(rows, max_chars=300)
        self.assertLessEqual(len(text), 300)
        self.assertEqual(set(ids) | set(omitted), set(range(1,13)))
        self.assertFalse(set(ids) & set(omitted))

    def test_technical_and_active_abstentions_have_separate_denominators(self):
        engine = self.engine()
        goods = [s for s in engine.alive_seats() if engine.roles[s].camp != "狼人"]
        engine.public_log = [{"kind":"votechart","title":"第1天 · 放逐投票", "votes": {goods[0]:0,goods[1]:0,goods[2]:engine.wolves[0]},
                              "decision_sources": {goods[0]:"technical_fallback",goods[1]:"model",goods[2]:"model"}}]
        stats = game_statistics(engine)
        self.assertEqual(stats["vote_provenance"]["technical_misses"], 1)
        self.assertEqual(stats["vote_provenance"]["active_abstentions"], 1)
        self.assertEqual(stats["good_active_abstention_rate"], .5)
        self.assertEqual(stats["observed_metrics"]["good_technical_miss_rate"]["denominator"], 3)

    def test_legacy_failure_inferred_from_action_record_without_rewriting_archive(self):
        engine = self.engine()
        good = next(s for s in engine.alive_seats() if engine.roles[s].camp != "狼人")
        engine.public_log = [{"kind":"votechart","title":"第3天 放逐投票","votes":{good:0}}]
        engine.reasoning_log[good] = [{"label":"D3 暗投","response":{"_is_fallback":True}}]
        stats = game_statistics(engine)
        self.assertEqual(stats["vote_provenance"]["technical_misses"], 1)
        self.assertFalse(any("承担投票责任" in f for f in stats["prompt_tuning_flags"]))

    def test_vote_detail_exposes_failure_not_strategic_abstention(self):
        text = GameEngine._vote_detail("3号->弃票，4号->弃票", {3:"technical_fallback",4:"model"})
        self.assertIn("3号->技术性未提交", text)
        self.assertIn("4号->弃票", text)
        self.assertIn("11号->弃票", GameEngine._vote_detail("1号->弃票，11号->弃票", {1:"technical_fallback"}))

    def test_async_request_carries_contract_and_compacts_previous_prompt(self):
        manager = self.manager()
        manager.set_action_context(1, {"allowed_targets":[0,9]})
        manager.set_public_evidence(1, [{"id":7,"seat":10,"phase":"第2天","text":GRAVE_SPEECH}])
        manager._create_with_hard_timeout = Mock(side_effect=[response('{"vote_target":4}'), response('{"vote_target":9}')])
        prompt = "包含历史全文的本轮提示" * 100
        request_id = manager.submit_ai_response(1, prompt, "vote")
        deadline = time.monotonic() + 2
        while True:
            done, result = manager.poll_ai_response(request_id)
            if done:
                break
            self.assertLess(time.monotonic(), deadline)
            threading.Event().wait(.005)
        self.assertEqual(result["vote_target"], 9)
        self.assertEqual(manager.agent_memory[1]["public_seen"], [7])
        self.assertNotIn(prompt, json.dumps(manager.agent_memory[1]["recent"], ensure_ascii=False))
        self.assertEqual(manager.agent_memory[1]["decisions"][-1]["action"]["vote_target"], 9)

    def test_order_does_not_take_direction_from_explanatory_text(self):
        self.assertEqual(parse_action('{"speech":"警左","reasoning":"警右收益较低"}', "order")["speech"], "警左")
        with self.assertRaises(ResponseProtocolError):
            parse_action('{"speech":"随便","reasoning":"警右"}', "order")

    def test_cancel_has_manual_provenance(self):
        self.assertEqual(self.manager().cancel_ai_response("missing")["decision_source"], "manual_skip")

    def test_private_action_display_uses_executed_field(self):
        self.assertEqual(GameEngine._action_display("guard", {"vote_target":9,"speech":"只在说明里提3号"}), "守护9号")
        self.assertIn("技术", GameEngine._action_display("vote", {"_is_fallback":True}))

    def test_exact_saved_archive_inputs(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "archive_failures.json").read_text(encoding="utf-8"))
        self.assertTrue(parse_yes_save(fixture["witch_speech"]))
        with self.assertRaises(ResponseProtocolError):
            parse_action(fixture["missing_speech_response"], "speech")
        self.assertEqual(parse_action(fixture["missing_speech_response"], "vote")["vote_target"], 9)

    def test_runtime_snapshot_has_loaded_code_and_excludes_config_source(self):
        runtime = _source_snapshot(self.engine())["runtime"]
        self.assertIn("engine._ask", runtime["loaded_code"])
        self.assertIn("roles", runtime["loaded_sources"])
        self.assertNotIn("config", runtime["loaded_sources"])

    def test_zero_vote_chart_distinguishes_technical_failure(self):
        from ui import ui_design
        source = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
        function = next(n for n in ast.parse(source).body if isinstance(n, ast.FunctionDef) and n.name == "render_vote_chart")
        namespace = {"design": ui_design}
        exec(compile(ast.Module(body=[function], type_ignores=[]), "app.py", "exec"), namespace)
        html = namespace["render_vote_chart"]({"votes":{1:0,11:0},"totals":{},
                                               "decision_sources":{1:"technical_fallback",11:"model"}})
        self.assertIn("1号（技术失票）", html)
        self.assertIn("不代表主动弃票", html)
        self.assertNotIn("11号（技术失票）", html)

    def test_length_requires_complete_valid_schema_and_target(self):
        for raw in ['{"vote_target":', '{"reasoning":"没有动作"}', '{"vote_target":4}']:
            with self.subTest(raw=raw), self.assertRaises(ResponseProtocolError):
                validate_content(raw, {"finish_reason":"length","has_message":True}, {}, 100, "vote", {"allowed_targets":[0,9]})
        meta = {"finish_reason":"length","has_message":True}
        validate_content('{"vote_target":9}', meta, {}, 100, "vote", {"allowed_targets":[0,9]})
        self.assertTrue(meta["rescued_length"])

    def test_simple_ack_and_order_disable_thinking(self):
        for kind in ["ack","order"]:
            self.assertEqual(request_options("deepseek",kind,budget=256,temperature=.5)["extra_body"]["thinking"]["type"], "disabled")

    def test_any_technical_failure_marks_sample_affected(self):
        rows = [{"record_type":"submitted","request_id":str(i),"response_kind":"vote"} for i in range(20)]
        rows += [{"record_type":"completed","request_id":str(i),"response_kind":"vote",
                  "status":"fallback" if i==0 else "success","attempts":[]} for i in range(20)]
        self.assertEqual(_usage_totals(rows)["quality"], "degraded")
        self.assertEqual(len(_usage_totals(rows)["affected_actions"]), 1)

    def test_all_boards_complete_with_structured_mock_actions(self):
        for board in config.GAME_BOARDS:
            with self.subTest(board=board):
                engine = self.engine(board)
                gen = engine.run()
                for step, event in enumerate(gen):
                    self.assertLess(step, 3000, "模拟对局没有收敛")
                self.assertIn(engine.winner, {"好人", "狼人", "情侣"})
                # 新版票型都保留决策来源，私密角色不进入公共证词。
                for row in engine.public_log:
                    if row.get("kind") == "votechart":
                        self.assertEqual(set(map(int,row["decision_sources"])), set(map(int,row["votes"])))


if __name__ == "__main__":
    unittest.main()
