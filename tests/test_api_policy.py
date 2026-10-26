"""复现思考预算耗尽、熔断和迟到响应；所有响应在本地构造。"""
import json
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

from engine import config
from agent.ai_agent import AgentManager
from storage.game_archive import _usage_totals
from engine.game_engine import GameEngine
from service.game_service import GameService
from agent.model_protocol import error_category, provider_for, validate_base_url
from service.view_projection import project_api_health


def completion(content='{"speech":"我暂不站边","vote_target":3}', *, finish="stop", tokens=80, reasoning=30):
    return NS(id="local-completion", model="deepseek-flash", choices=[NS(
        finish_reason=finish, message=NS(content=content, reasoning_content="hidden-must-not-export"))],
        usage={"prompt_tokens": 100, "completion_tokens": tokens, "total_tokens": 100+tokens,
               "completion_tokens_details": {"reasoning_tokens": reasoning}})


class PolicyTests(unittest.TestCase):
    def setUp(self):
        # 最后一道防线，任何未被模拟的 SDK 调用都会使测试失败，绝不发送网络请求。
        network = patch("openai.resources.chat.completions.Completions.create",
                        side_effect=AssertionError("禁止测试访问模型网络"))
        network.start()
        self.addCleanup(network.stop)

    def manager(self, **settings):
        kwargs = dict(api_key="test-key-not-sent", base_url="https://api.deepseek.com",
                      good_model="deepseek-flash", wolf_model="deepseek-flash",
                      fallback_model="", agent_timeout=2, max_retries=3)
        kwargs.update(settings)
        manager = AgentManager(**kwargs)
        manager.init_agent(1, "平民", "冷静", "无")
        self.addCleanup(manager.shutdown)
        return manager

    def call(self, manager, kind="speech", request_id="local-request"):
        return manager._execute_response(request_id, 1, kind, manager._build_messages(1), manager.good_model)

    def wait(self, predicate):
        deadline = time.monotonic()+2
        while time.monotonic() < deadline:
            if predicate():
                return
            threading.Event().wait(.005)
        self.fail("模拟请求未按期结束")

    def test_thinking_consumes_budget_then_one_larger_attempt_succeeds(self):
        manager = self.manager()
        create = Mock(side_effect=[completion("", finish=None, tokens=config.QUICK_OUTPUT_TOKENS, reasoning=config.QUICK_OUTPUT_TOKENS), completion()])
        manager._create_with_hard_timeout = create
        result = self.call(manager, "vote")
        self.assertFalse(result.get("_is_fallback"))
        self.assertEqual([c.kwargs["max_tokens"] for c in create.call_args_list], [config.QUICK_OUTPUT_TOKENS, config.QUICK_REPAIR_TOKENS])
        self.assertEqual(create.call_args_list[0].kwargs["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(create.call_args_list[0].kwargs["reasoning_effort"], "low")
        self.assertNotIn("temperature", create.call_args_list[0].kwargs)
        self.assertEqual(manager.get_usage_snapshot()["total_tokens"], (100 + config.QUICK_OUTPUT_TOKENS) + 180)
        diagnostic = manager.get_health_snapshot()["last_diagnostic"]
        self.assertEqual(diagnostic["budget_evidence"], "reasoning_usage_inferred")
        self.assertTrue(diagnostic["resolved"])
        self.assertNotIn("hidden-must-not-export", json.dumps(manager.get_health_snapshot()))

    def test_length_finish_reason_rescues_complete_valid_action(self):
        manager = self.manager()
        manager._create_with_hard_timeout = Mock(side_effect=[
            completion('{"speech":"投3号","vote_target":3}', finish="length", tokens=config.ACTION_OUTPUT_TOKENS), completion()])
        self.call(manager)
        self.assertEqual(manager._create_with_hard_timeout.call_count, 1)

    def test_unknown_empty_output_does_not_trigger_unbounded_budget_increase(self):
        manager = self.manager()
        manager._create_with_hard_timeout = Mock(return_value=completion("", tokens=10, reasoning=0))
        result = self.call(manager)
        self.assertTrue(result["_is_fallback"])
        self.assertEqual(manager._create_with_hard_timeout.call_count, 2)
        self.assertEqual([c.kwargs["max_tokens"] for c in manager._create_with_hard_timeout.call_args_list], [config.ACTION_OUTPUT_TOKENS, config.ACTION_OUTPUT_TOKENS])
        self.assertEqual(result["_api_diagnostic"]["category"], "empty_content")

    def test_invalid_json_and_invalid_action_are_repaired_without_executing_them(self):
        for bad in ['{"vote_target":3', '[]', '{"vote_target":true}', '{"vote_target":99}',
                    '{"speech":"test"}', '{"vote_target":3,"is_explode":"false"}']:
            with self.subTest(bad=bad):
                manager = self.manager()
                manager._create_with_hard_timeout = Mock(side_effect=[completion(bad), completion()])
                result = self.call(manager, "vote")
                self.assertEqual(result["vote_target"], 3)
                self.assertFalse(result["is_explode"])
                self.assertEqual(manager._create_with_hard_timeout.call_count, 2)

    def test_missing_choices_keeps_usage_and_classifies_response(self):
        manager = self.manager(max_retries=1)
        response = completion()
        response.choices = []
        manager._create_with_hard_timeout = Mock(return_value=response)
        result = self.call(manager)
        self.assertEqual(result["_api_diagnostic"]["category"], "invalid_response")
        self.assertEqual(manager.get_usage_snapshot()["total_tokens"], 180)
        self.assertEqual(manager.api_stats["success"], 0)

    def test_three_failed_actions_use_six_calls_and_preserve_breaker(self):
        manager = self.manager()
        manager._create_with_hard_timeout = Mock(return_value=completion("", finish="length", tokens=4096))
        results = [self.call(manager, request_id=f"request-{i}") for i in range(3)]
        self.assertEqual(manager._create_with_hard_timeout.call_count, 6)
        self.assertNotIn("_fatal_error", results[0])
        self.assertIn("预算不足", results[2]["_fatal_error"])
        self.assertIn("熔断", results[2]["_fatal_error"])
        self.assertNotIn("检查网络", results[2]["_fatal_error"])
        self.assertTrue(manager.get_health_snapshot()["circuit_open"])

    def test_generic_endpoint_gets_no_deepseek_private_parameters(self):
        with patch.dict("os.environ", {"SWV_ALLOWED_MODEL_HOSTS": "example.invalid"}):
            manager = self.manager(base_url="https://example.invalid/v1")
        manager._create_with_hard_timeout = Mock(return_value=completion())
        self.call(manager)
        kwargs = manager._create_with_hard_timeout.call_args.kwargs
        self.assertNotIn("extra_body", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertEqual(provider_for("https://api.deepseek.com.evil.invalid"), "generic")
        self.assertEqual(provider_for("https://example.invalid", "deepseek"), "deepseek")

    def test_base_url_requires_trusted_https_endpoint(self):
        with patch.dict("os.environ", {"SWV_ALLOWED_MODEL_HOSTS": "", "SWV_ALLOW_PRIVATE_MODEL_URLS": ""}):
            self.assertEqual(validate_base_url("https://api.deepseek.com/v1"), "https://api.deepseek.com/v1")
            self.assertEqual(validate_base_url("https://api.openai.com/v1"), "https://api.openai.com/v1")
            for unsafe in (
                "http://api.deepseek.com/v1",
                "https://user:secret@api.deepseek.com/v1",
                "https://api.deepseek.com/v1?api_key=secret",
                "http://127.0.0.1:11434/v1",
                "http://169.254.169.254/latest/meta-data",
                "https://example.invalid/v1",
            ):
                with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                    validate_base_url(unsafe)

    def test_agent_manager_rejects_unsafe_base_url_before_creating_client(self):
        with self.assertRaises(ValueError):
            AgentManager(api_key="never-sent", base_url="http://127.0.0.1:11434/v1")

    def test_custom_and_private_endpoints_need_explicit_self_hosted_configuration(self):
        with patch.dict("os.environ", {"SWV_ALLOWED_MODEL_HOSTS": "example.invalid"}):
            self.assertEqual(validate_base_url("https://example.invalid/v1"), "https://example.invalid/v1")
        with patch.dict("os.environ", {"SWV_ALLOW_PRIVATE_MODEL_URLS": "1"}):
            self.assertEqual(validate_base_url("http://127.0.0.1:11434/v1"), "http://127.0.0.1:11434/v1")

    def test_global_request_limit_timeout_does_not_start_http_worker(self):
        manager = self.manager()
        with patch("agent.ai_agent._MODEL_REQUEST_SLOTS", threading.BoundedSemaphore(0)):
            with self.assertRaises(TimeoutError):
                manager._create_with_hard_timeout(0.01)

    def test_auth_and_balance_errors_stop_after_one_call(self):
        for code, category in [(401, "auth"), (402, "balance"), (403, "permission")]:
            manager = self.manager()
            error = RuntimeError("provider-error")
            error.status_code = code
            manager._create_with_hard_timeout = Mock(side_effect=error)
            result = self.call(manager)
            self.assertEqual(manager._create_with_hard_timeout.call_count, 1)
            self.assertEqual(result["_api_diagnostic"]["category"], category)
            self.assertIn(str(code), result["_fatal_error"])

    def test_rate_limit_retry_after_beyond_deadline_is_not_shortened(self):
        manager = self.manager()
        error = RuntimeError("rate limited")
        error.status_code = 429
        error.response = NS(headers={"retry-after": "90"})
        manager._create_with_hard_timeout = Mock(side_effect=error)
        with patch("agent.ai_agent.time.sleep") as sleep:
            result = self.call(manager)
        self.assertEqual(manager._create_with_hard_timeout.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(result["_api_diagnostic"]["category"], "rate_limit")
        self.assertIn("90", result["_fatal_error"])
        self.assertTrue(manager.get_health_snapshot()["circuit_open"])

    def test_temporary_provider_error_has_one_finite_retry(self):
        manager = self.manager()
        error = RuntimeError("unavailable")
        error.status_code = 503
        manager._create_with_hard_timeout = Mock(side_effect=[error, completion()])
        with patch.object(manager, "_wait_for_retry"):
            self.assertFalse(self.call(manager).get("_is_fallback"))
        self.assertEqual(manager._create_with_hard_timeout.call_count, 2)

    def test_model_fallback_consumes_the_same_two_attempt_limit(self):
        manager = self.manager(fallback_model="fallback-model")
        error = RuntimeError("model does not exist")
        error.status_code = 404
        manager._create_with_hard_timeout = Mock(side_effect=[error, completion()])
        self.assertFalse(self.call(manager).get("_is_fallback"))
        self.assertEqual([c.kwargs["model"] for c in manager._create_with_hard_timeout.call_args_list],
                         ["deepseek-flash", "fallback-model"])

    def test_bad_parameter_is_not_blindly_retried_as_missing_model(self):
        manager = self.manager(fallback_model="fallback-model")
        error = RuntimeError("unsupported parameter")
        error.status_code = 400
        manager._create_with_hard_timeout = Mock(side_effect=error)
        self.assertTrue(self.call(manager)["_is_fallback"])
        self.assertEqual(manager._create_with_hard_timeout.call_count, 1)

    def test_unexpected_worker_exception_is_also_counted_by_breaker(self):
        manager = self.manager()
        manager._execute_response = Mock(side_effect=RuntimeError("local worker failure"))
        for _ in range(3):
            result = manager.get_ai_response(1, "请发言")
        self.assertTrue(manager.get_health_snapshot()["circuit_open"])
        self.assertIn("熔断", result["_fatal_error"])

    def test_action_deadline_is_shared_across_attempts(self):
        manager = self.manager(agent_timeout=1)
        now = [0.0]
        def slow(*args, **kwargs):
            now[0] = 2.0
            return completion("", finish="length")
        manager._create_with_hard_timeout = Mock(side_effect=slow)
        with patch("agent.ai_agent.time.monotonic", side_effect=lambda: now[0]):
            self.call(manager)
        self.assertEqual(manager._create_with_hard_timeout.call_count, 1)

    def test_canceled_late_success_does_not_reset_health_or_write_memory(self):
        manager = self.manager()
        manager._consecutive_failures = 2
        entered, gate = threading.Event(), threading.Event()
        def delayed(*args, **kwargs):
            entered.set()
            gate.wait(2)
            return completion('{"speech":"late secret"}')
        manager._create_with_hard_timeout = delayed
        request_id = manager.submit_ai_response(1, "请发言")
        self.assertTrue(entered.wait(1))
        manager.cancel_ai_response(request_id)
        gate.set()
        self.wait(lambda: not manager.has_pending())
        self.assertEqual(manager.get_health_snapshot()["consecutive_failures"], 2)
        self.assertNotIn("late secret", str(manager.agent_memory[1]["recent"]))
        self.assertEqual(manager.get_usage_snapshot()["total_tokens"], 180)

    def test_hard_timeout_blocks_reconfigure_until_late_usage_is_accounted(self):
        manager = self.manager(agent_timeout=.03)
        gate = threading.Event()
        self.addCleanup(gate.set)
        def delayed(**kwargs):
            gate.wait(1)
            return completion()
        manager.client.chat.completions.create = delayed
        result = self.call(manager)
        self.assertEqual(result["_api_diagnostic"]["category"], "timeout")
        self.assertTrue(manager.has_pending())
        self.assertEqual(manager.get_usage_snapshot()["unknown_requests"], 1)
        gate.set()
        self.wait(lambda: not manager.has_pending())
        self.assertEqual(manager.get_usage_snapshot()["unknown_requests"], 0)
        self.assertEqual(manager.get_usage_snapshot()["total_tokens"], 180)
        self.assertEqual(manager.get_health_snapshot()["consecutive_failures"], 1)

    def test_summary_uses_explicit_non_thinking_budget_and_records_failed_usage(self):
        manager = self.manager()
        manager.agent_memory[1]["recent"] = [{"role": "user", "content": "我的判断"}]
        manager.agent_memory[1]["summary"] = "已有摘要"
        manager._create_with_hard_timeout = Mock(return_value=completion("", finish="length", tokens=1200))
        status, _ = manager._summarize_one(1)
        self.assertEqual(status, "fallback")
        kwargs = manager._create_with_hard_timeout.call_args.kwargs
        self.assertEqual(kwargs["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertEqual(kwargs["max_tokens"], 1200)
        self.assertEqual(manager._create_with_hard_timeout.call_count, 1)
        self.assertEqual(manager.agent_memory[1]["summary"], "已有摘要")
        self.assertEqual(manager.get_usage_snapshot()["summary_total_tokens"], 1300)

    def test_short_diagnostic_requires_json_and_does_not_clear_circuit(self):
        manager = self.manager()
        manager._consecutive_failures, manager._circuit_open = 3, True
        manager._create_with_hard_timeout = Mock(return_value=completion('{"ok":1}'))
        ok, message, diagnostic = manager.diagnose_connection()
        self.assertTrue(ok)
        self.assertEqual(manager.get_health_snapshot()["consecutive_failures"], 3)
        self.assertEqual(manager._create_with_hard_timeout.call_count, 1)
        self.assertEqual(manager._create_with_hard_timeout.call_args.kwargs["max_tokens"], 256)
        self.assertEqual(manager.get_usage_snapshot()["by_response_kind"]["diagnostic"]["total_tokens"], 180)
        manager._create_with_hard_timeout.return_value = completion("")
        self.assertFalse(manager.diagnose_connection()[0])

    def test_transport_errors_are_distinguished(self):
        self.assertEqual(error_category(ConnectionRefusedError("refused")), "connection_refused")
        class ConnectTimeout(Exception):
            pass
        class ReadTimeout(Exception):
            pass
        self.assertEqual(error_category(ConnectTimeout()), "connect_timeout")
        self.assertEqual(error_category(ReadTimeout()), "read_timeout")

    def test_diagnostic_reasoning_usage_survives_save_load(self):
        manager = self.manager(max_retries=1)
        manager._create_with_hard_timeout = Mock(return_value=completion("", tokens=4096, reasoning=4096))
        self.call(manager)
        restored = self.manager()
        restored.load_state(manager.export_state())
        self.assertEqual(restored.get_health_snapshot()["last_diagnostic"]["usage"]["reasoning_tokens"], 4096)

    def test_diagnostic_export_redacts_other_seat_model_association(self):
        health = {"last_diagnostic": {"seat": 4, "model": "wolf-model", "request_id": "secret-id",
                                      "category": "budget_exhausted", "usage": {"total_tokens": 200}}}
        projected = project_api_health(health, 1)
        self.assertNotIn("wolf-model", json.dumps(projected))
        self.assertNotIn("secret-id", json.dumps(projected))
        self.assertEqual(projected["last_diagnostic"]["usage"]["total_tokens"], 200)
        self.assertIn("model", health["last_diagnostic"])

    def test_archive_counts_diagnostics_and_late_usage_without_inflating_player_success(self):
        rows = [
            {"record_type": "submitted", "request_id": "a", "response_kind": "speech"},
            {"record_type": "completed", "request_id": "a", "response_kind": "speech", "status": "fallback",
             "attempts": [{"attempt": 1, "category": "timeout"}]},
            {"record_type": "late_usage", "request_id": "a", "attempt_key": "a:1",
             "response_kind": "speech", "usage": {"total_tokens": 50}},
            {"record_type": "submitted", "request_id": "d", "response_kind": "diagnostic"},
            {"record_type": "completed", "request_id": "d", "response_kind": "diagnostic",
             "status": "success", "usage": {"total_tokens": 10}},
        ]
        result = _usage_totals(rows)
        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["success"], 0)
        self.assertEqual(result["unknown_requests"], 0)
        self.assertEqual(result["total_tokens"], 60)

    def test_settings_are_rejected_while_running_and_for_pending_transports(self):
        manager = self.manager()
        engine = GameEngine(manager)
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        with tempfile.TemporaryDirectory() as directory:
            service = GameService(engine, directory, automatic=False)
            service.play()
            with self.assertRaisesRegex(RuntimeError, "暂停"):
                service.apply_model_settings(api_key="test")
            service.pause()
            manager._transport_calls.add("old-request")
            with self.assertRaisesRegex(RuntimeError, "后台请求"):
                service.apply_model_settings(api_key="test")
            manager._transport_calls.clear()
            service.close()


if __name__ == "__main__":
    unittest.main()
