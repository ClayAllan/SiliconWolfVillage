"""真实 Streamlit 控件与后台服务的连接，不调用网络或付费模型。"""
import io
import json
import zipfile
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from agent.ai_agent import AgentManager
from engine import config
from engine.game_engine import GameEngine
from service.game_service import GameService
from storage.archive_zip import PRIVATE_ARCHIVE_ZIP_CACHE_KEY
from test_runtime_regressions import MemoryOnlyManager


APP = Path(__file__).resolve().parents[1] / "app.py"


class AppIntegrationTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.saves = Path(folder.name) / "saves"
        self.replays = Path(folder.name) / "replays"
        self.saves.mkdir()
        self.replays.mkdir()
        storage = patch("storage.session_storage.storage_paths", return_value=(str(self.saves), str(self.replays)))
        storage.start()
        self.addCleanup(storage.stop)
        # 防止未来修改让测试意外访问真实模型服务。
        network = patch.object(AgentManager, "_execute_response", side_effect=AssertionError("测试禁止模型请求"))
        network.start()
        self.addCleanup(network.stop)
        self.app = AppTest.from_file(str(APP), default_timeout=10).run()
        self.addCleanup(self.close_service)
        self.assert_clean()

    def close_service(self):
        service = self.app.session_state["game_service"]
        if service is not None:
            service.close("test_cleanup")

    def assert_clean(self):
        self.assertFalse(self.app.exception, [e.message for e in self.app.exception])

    def click(self, label):
        next(b for b in self.app.button if b.label == label).click().run()
        self.assert_clean()

    def start_manual(self):
        self.app.checkbox[0].uncheck().run()
        self.app.text_input[0].set_value("test-key-never-sent").run()
        self.click("🎬 开始新游戏")

    def install_pending_human_action(self, human_seat=1, wolf_talk=None):
        manager = MemoryOnlyManager()
        manager.has_pending = lambda: False
        manager.get_usage_snapshot = lambda: {"api_returns": 0, "unmetered_returns": 0, "unknown_requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cache_hit_tokens": 0, "cache_miss_tokens": 0, "reasoning_tokens": 0, "summary_requests": 0, "summary_total_tokens": 0, "by_response_kind": {}}
        engine = GameEngine(manager)
        engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
        if human_seat == "wolf":
            human_seat = engine.wolves[0]
        elif human_seat == "good":
            human_seat = next(
                seat for seat, role in engine.roles.items() if role.camp != "狼人"
            )
        engine.human_seat = human_seat
        if wolf_talk is not None:
            engine.current_wolf_talk = wolf_talk
        is_wolf = engine.roles[human_seat].camp == "狼人"
        generator = engine._ask(
            human_seat,
            "测试用内部提示；不应替代真人输入。",
            "D1 狼人讨论" if is_wolf else "D1 发言",
            public=not is_wolf,
        )
        service = GameService(
            engine, str(self.replays), automatic=False, generator=generator
        )
        self.assertTrue(service.step())
        self.assertEqual(service.snapshot().last_event_type, "human_input_required")
        self.app.session_state["game_service"] = service
        return engine, service, human_seat

    def test_manual_save_load_resume_and_reset(self):
        self.start_manual()
        self.click("⏭️ 单步")
        self.assertEqual(self.app.session_state["last_event_type"], "start")
        self.click("⏸️ 暂停")
        self.click("💾 保存当前进度")
        saved = list(self.saves.glob("*.json"))
        self.assertEqual(len(saved), 1)
        self.assertNotIn("test-key-never-sent", saved[0].read_text(encoding="utf-8"))
        self.assertTrue(any(
            item.label == "📤 下载所选私密进度存档"
            for item in self.app.get("download_button")
        ))
        old_service = self.app.session_state["game_service"]
        self.click("📂 读取存档")
        self.assertFalse(old_service.step())
        self.assertFalse(self.app.session_state["running"])
        self.assertIn("第1夜", self.app.session_state["status"])
        self.click("⏭️ 单步")
        self.assertEqual(self.app.session_state["last_event_type"], "resume")
        self.click("🔄 重置")
        self.assertIsNone(self.app.session_state["engine"])
        self.assertIsNone(self.app.session_state["game_service"])


    def test_pending_human_turn_renders_and_submits_the_real_action_form(self):
        engine, service, human_seat = self.install_pending_human_action(1)
        self.app.run()
        self.assert_clean()
        self.assertTrue(any("轮到你行动" in item.value for item in self.app.subheader))
        speech = next(item for item in self.app.text_area if item.label == "你的发言")
        speech.set_value("这是由真人表单提交的发言。")
        submit = next(item for item in self.app.button if item.label == "提交行动")
        submit.click().run()
        self.assert_clean()
        self.assertEqual(engine.reasoning_log[human_seat][-1]["speech"], "这是由真人表单提交的发言。")
        self.assertEqual(engine.reasoning_log[human_seat][-1]["actor_source"], "human")
        self.assertIsNone(engine.pending_human_action)

    def test_living_human_wolf_sees_teammate_discussion_in_ui(self):
        engine, service, human_seat = self.install_pending_human_action("wolf")
        teammate = next(seat for seat in engine.wolves if seat != human_seat)
        engine.current_wolf_talk = [(teammate, "队友的私密讨论内容")]
        self.app.run()
        self.assert_clean()
        self.assertTrue(any("狼队私密频道" in item.value for item in self.app.subheader))
        rendered = "\n".join(item.value for item in self.app.markdown)
        self.assertIn("队友的私密讨论内容", rendered)

    def test_good_human_never_sees_wolf_private_discussion_in_ui(self):
        engine, service, human_seat = self.install_pending_human_action("good")
        teammate = engine.wolves[0]
        engine.current_wolf_talk = [(teammate, "不得向好人泄露的狼队私密内容")]
        self.app.run()
        self.assert_clean()
        rendered = "\n".join(item.value for item in self.app.markdown)
        self.assertNotIn("不得向好人泄露的狼队私密内容", rendered)
        self.assertFalse(any("狼队私密频道" in item.value for item in self.app.subheader))

    def test_automatic_completion_renders_terminal_archive_and_metrics(self):
        def short_game(engine):
            engine.phase = "第1夜"
            engine.day = 1
            yield engine._ev("night", "测试夜晚")
            engine.winner = "好人"
            engine.phase = "游戏结束"
            yield engine._ev("end", "游戏结束")

        self.app.text_input[0].set_value("test-key-never-sent").run()
        with patch.object(GameEngine, "run", short_game):
            self.click("🎬 开始新游戏")
        # 等待后台服务完成时不要高频轮询 snapshot()：它会获取同一把服务锁并
        # 深拷贝引擎状态，测试自身可能与正在推进的 worker 争抢锁。
        # 通过服务条件变量等待真实的 done 通知，保留硬超时以检测真实卡死。
        service = self.app.session_state["game_service"]
        with service._condition:
            completed = service._condition.wait_for(lambda: service._done, timeout=8)
        self.assertTrue(completed, "后台服务未在 8 秒内完成；不是通过放宽断言掩盖超时。")
        self.assertTrue(service.snapshot().done)
        self.app.run()
        self.assert_clean()
        self.assertTrue(self.app.session_state["done"])
        self.assertFalse(self.app.session_state["running"])
        archive = self.app.session_state["last_archive"]
        self.assertTrue(Path(archive["json"]).exists())
        metric = next(m for m in self.app.metric if m.label == "好人投狼比例")
        self.assertEqual(metric.value, "N/A")

    def test_paused_player_can_be_skipped_from_ui(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def waiting_response(*_args):
            gate.wait(3)
            return {"reasoning": "迟到结果", "speech": "过。", "vote_target": 0}

        with patch.object(AgentManager, "_execute_response", side_effect=waiting_response):
            self.start_manual()
            for _ in range(5):
                self.click("⏭️ 单步")
                if self.app.session_state["engine"].has_pending_action():
                    break
            self.assertTrue(self.app.session_state["engine"].has_pending_action())
            self.click("⏸️ 暂停")
            self.click("⏭️ 跳过当前玩家")
            self.assertFalse(self.app.session_state["engine"].has_pending_action())
            self.assertFalse(self.app.session_state["running"])
            self.assertEqual(self.app.session_state["last_event_type"], "done")
        gate.set()

    def test_invalid_load_preserves_the_current_service(self):
        self.start_manual()
        self.click("⏸️ 暂停")
        service = self.app.session_state["game_service"]
        (self.saves / "invalid.json").write_text(json.dumps(["invalid"]), encoding="utf-8")
        self.app.run()
        self.click("📂 读取存档")
        self.assertIs(self.app.session_state["game_service"], service)
        self.assertTrue(any("读档失败" in e.value for e in self.app.error))
        self.assertTrue(service.step())

    def test_preview_is_isolated_and_does_not_create_a_model_client(self):
        with patch.object(AgentManager, "__init__", side_effect=AssertionError("示例禁止创建模型客户端")):
            self.click("查看示例 · 零 API")
            self.assertTrue(self.app.session_state["preview_mode"])
            self.assertIsNone(self.app.session_state["engine"])
            self.assertIsNone(self.app.session_state["game_service"])
            self.assertFalse(list(self.replays.iterdir()))
            self.click("退出示例")
            self.assertFalse(self.app.session_state["preview_mode"])

    def test_preview_feed_filters_by_player_and_judge(self):
        self.click("查看示例 · 零 API")
        self.app.radio(key="feed_channel").set_value("玩家发言").run()
        self.assert_clean()
        rows = [m.value for m in self.app.markdown if '<article class="wv-event' in m.value]
        self.assertEqual(len(rows), 1)
        self.assertIn("03 号玩家", rows[0])
        self.assertNotIn("法官播报", rows[0])
        self.app.radio(key="feed_channel").set_value("法官播报").run()
        rows = [m.value for m in self.app.markdown if '<article class="wv-event' in m.value]
        self.assertIn("法官播报", rows[0])
        self.assertNotIn("03 号玩家", rows[0])

    def test_feed_controls_preserve_game_and_top_exits_follow_mode(self):
        self.click("查看示例 · 零 API")
        self.assertTrue(self.app.toggle(key="feed_follow_latest").value)
        self.click("⬆️ 顶部")
        self.assertFalse(self.app.toggle(key="feed_follow_latest").value)
        self.assertIsNone(self.app.session_state.filtered_state.get("_feed_scroll_action"))
        self.click("⬇️ 沉底一次")
        self.assertFalse(self.app.toggle(key="feed_follow_latest").value)
        self.assertIsNone(self.app.session_state.filtered_state.get("_feed_scroll_action"))
        self.app.toggle(key="feed_follow_latest").set_value(True).run()
        self.app.toggle(key="feed_follow_latest").set_value(False).run()
        self.assertIsNone(self.app.session_state.filtered_state.get("_feed_scroll_action"))
        self.assert_clean()
        self.assertIsNone(self.app.session_state["game_service"])

    def test_old_150_row_terminal_does_not_survive_new_64_row_game(self):
        from service.game_service import GameService
        from engine import config

        def make_engine(count, old=False):
            manager = AgentManager(api_key="offline", base_url="https://api.deepseek.com")
            self.addCleanup(manager.shutdown)
            engine = GameEngine(manager)
            engine.setup(config.GAME_BOARDS[config.DEFAULT_BOARD])
            engine.public_log = []
            for i in range(count - int(old)):
                engine._log_judge(f"{'OLD' if old else 'NEW'}动态{i}")
            if old:
                engine.winner, engine.phase = "狼人", "游戏结束"
                engine._log_win("OLD终局 狼人获胜")
            else:
                engine.day, engine.phase, engine.current_speaker = 2, "第2天", 6
            return engine

        old = GameService(make_engine(150, True), str(self.replays), automatic=False)
        self.addCleanup(old.close)
        self.app.session_state["game_service"] = old
        self.app.run()
        self.assert_clean()
        self.assertIn("OLD终局", "\n".join(m.value for m in self.app.markdown))
        current = GameService(make_engine(64), str(self.replays), automatic=False)
        self.addCleanup(current.close)
        self.app.session_state["game_service"] = current
        self.app.run()
        self.assert_clean()
        rendered = "\n".join(m.value for m in self.app.markdown)
        self.assertNotIn("OLD", rendered)
        self.assertNotIn("狼人获胜", rendered)
        self.assertIn("NEW动态63", rendered)
        self.assertIn("06 号的回合", rendered)
        self.assertFalse(any("仅展示最近" in c.value for c in self.app.caption))

    def test_single_view_hides_other_players_and_full_knowledge_exports(self):
        self.click("查看示例 · 零 API")
        self.app.selectbox(key="viewer_seat").set_value(7)
        self.app.radio(key="viewer_mode").set_value("单一视角").run()
        self.assert_clean()
        options=self.app.selectbox(key="sel_seat").options
        self.assertEqual(options,["07 号 · 平民"])
        rendered="\n".join(m.value for m in self.app.markdown)
        self.assertIn('aria-label="1号 未知 存活"',rendered)
        self.assertNotIn("秘密行动 · 全知可见",rendered)
        self.assertNotIn('aria-label="狼人"',rendered)
        self.assertIn("存活未知",rendered)
        self.app.checkbox(key="reveal_factions").check().run()
        self.assert_clean()
        rendered="\n".join(m.value for m in self.app.markdown)
        self.assertIn('aria-label="狼人" aria-valuenow="4"',rendered)

    def test_native_appearance_entry_and_perspective_preserve_a_live_game(self):
        self.start_manual()
        self.click("⏸️ 暂停")
        service=self.app.session_state["game_service"]
        game_id=service.snapshot().engine.game_id
        self.assertFalse(any(box.key == "ui_theme" for box in self.app.selectbox))
        self.assertTrue(any("右上角" in caption.value and "跟随系统" in caption.value for caption in self.app.caption))
        for _ in range(2):
            self.app.run()
            self.assert_clean()
            self.assertIs(self.app.session_state["game_service"],service)
            self.assertEqual(service.snapshot().engine.game_id,game_id)
        self.app.radio(key="viewer_mode").set_value("单一视角").run()
        self.assert_clean()
        self.assertFalse(any(b.label=="📥 导出本局复盘 (MD格式)" for b in self.app.get("download_button")))
        self.assertFalse(any(b.label=="⚡ 发送法旨" for b in self.app.button))
        self.app.radio(key="viewer_mode").set_value("全知视角").run()
        self.assert_clean()
        self.assertIs(self.app.session_state["game_service"],service)

    def test_apply_settings_preserves_paused_game_memory_usage_and_failure_count(self):
        self.start_manual()
        self.click("⏸️ 暂停")
        service = self.app.session_state["game_service"]
        previous = service._engine.manager
        previous._accumulate_usage({"total_tokens": 123}, "speech")
        previous._consecutive_failures = 2
        previous.agent_memory[1]["summary"] = "保留我的判断"
        game_id = service.snapshot().engine.game_id
        self.app.run()
        self.click("应用模型设置（保持暂停）")
        current = service._engine.manager
        self.assertIsNot(current, previous)
        self.assertEqual(current.agent_memory[1]["summary"], "保留我的判断")
        self.assertEqual(current.get_usage_snapshot()["total_tokens"], 123)
        self.assertEqual(current.get_health_snapshot()["consecutive_failures"], 2)
        self.assertEqual(service.snapshot().engine.game_id, game_id)
        self.assertFalse(service.snapshot().running)

    def test_existing_service_uses_current_snapshot_protocol_after_hot_update(self):
        self.start_manual()
        self.click("⏸️ 暂停")
        service = self.app.session_state["game_service"]
        original_snapshot = service.snapshot

        class LegacyUsage:
            def get_usage_snapshot(self):
                return service._engine.manager.get_usage_snapshot()

            def has_pending(self):
                return False

        def legacy_snapshot():
            snapshot = original_snapshot()
            snapshot.engine.manager = LegacyUsage()
            return snapshot

        with patch.object(service, "snapshot", side_effect=legacy_snapshot):
            self.app.run()
            self.assert_clean()
            self.assertTrue(any(e.label == "API 诊断与恢复" for e in self.app.expander))

    def test_sharing_opt_in_and_observer_route_never_loads_owner_controls(self):
        from service.room_registry import get_registry
        self.app.checkbox(key="enable_sharing").check().run()
        self.start_manual()
        room, owner = self.app.session_state["room_receipt"]
        service = self.app.session_state["game_service"]
        self.assertIsNotNone(service.display_replay)
        grant, secret = get_registry().issue(room, owner, "public")
        observer = AppTest.from_file(str(APP), default_timeout=10)
        observer.query_params["room"] = room
        with patch.object(AgentManager, "__init__", side_effect=AssertionError("observer created client")):
            observer.run()
            self.assertFalse(observer.exception)
            self.assertEqual(len(observer.text_input), 1)
            observer.text_input[0].set_value(secret)
            next(b for b in observer.button if b.label == "加入观战").click().run()
            self.assertFalse(observer.exception)
            self.assertFalse(any(b.label in {"🎬 开始新游戏", "⏸️ 暂停", "应用模型设置（保持暂停）"} for b in observer.button))
            self.assertNotIn("game_service", observer.session_state.filtered_state)
            self.assertNotIn("engine", observer.session_state.filtered_state)
            self.assertTrue(any("未知" in m.value for m in observer.markdown))
            get_registry().revoke(room, owner, grant)
            observer.run()
            self.assertFalse(observer.exception)
            self.assertTrue(any("权限已失效" in e.value for e in observer.error))
            self.assertFalse(any('<div class="wv-matrix"' in m.value for m in observer.markdown))
        service.close()
        get_registry().release(room, owner)


    def test_private_archive_zip_download_requires_owner_consent(self):
        archived = self.replays / "20261029_zip_fixture"
        archived.mkdir()
        (archived / "manifest.json").write_text(
            json.dumps({"metadata": {"game_id": "offline-zip", "status": "completed"}}),
            encoding="utf-8",
        )
        (archived / "final_state.json").write_text(
            json.dumps({"reasoning_log": {"1": "PRIVATE_ONLY"}}, ensure_ascii=False),
            encoding="utf-8",
        )
        (archived / "events.jsonl").write_text(
            '{"event":"public_log"}\\n', encoding="utf-8"
        )
        self.app.run()
        self.assert_clean()
        label = "⬇️ 下载已准备的历史对局归档 ZIP"
        download = next(
            item for item in self.app.get("download_button")
            if item.label == label
        )
        self.assertTrue(download.disabled)
        self.assertNotIn(PRIVATE_ARCHIVE_ZIP_CACHE_KEY, self.app.session_state)

        agreement = next(
            item for item in self.app.checkbox
            if item.label == "我了解这是一份包含私密信息的完整归档"
        )
        agreement.check().run()
        self.assert_clean()
        download = next(
            item for item in self.app.get("download_button")
            if item.label == label
        )
        self.assertFalse(download.disabled)
        captions = [item.value for item in self.app.caption]
        self.assertTrue(any("部署验收标记：SWV-20261030-ARCHIVE-IMPORT-COMPAT" in value for value in captions))
        self.assertTrue(any("ZIP 已在当前会话中准备并缓存" in value and "bytes" in value for value in captions))
        # Consent prepares the ZIP once. The ordinary download widget must
        # keep re-registering the same bytes across full-page auto-refresh reruns.
        cached = self.app.session_state[PRIVATE_ARCHIVE_ZIP_CACHE_KEY]
        self.assertIsInstance(cached["data"], bytes)
        with zipfile.ZipFile(io.BytesIO(cached["data"])) as package:
            self.assertIn("20261029_zip_fixture/manifest.json", package.namelist())
            self.assertIn("20261029_zip_fixture/PRIVATE_ARCHIVE_NOTICE.txt", package.namelist())
        cached_bytes = cached["data"]
        self.app.run()
        self.assert_clean()
        self.assertEqual(
            self.app.session_state[PRIVATE_ARCHIVE_ZIP_CACHE_KEY]["data"],
            cached_bytes,
        )
        download = next(
            item for item in self.app.get("download_button")
            if item.label == label
        )
        self.assertFalse(download.disabled)

        # An unfinished restricted human perspective may not access the full ZIP.
        self.app.session_state["viewer_mode"] = "单一视角"
        self.app.run()
        self.assert_clean()
        self.assertNotIn(PRIVATE_ARCHIVE_ZIP_CACHE_KEY, self.app.session_state)
        self.assertFalse(any(
            item.label == label
            for item in self.app.get("download_button")
        ))

    def test_structured_archive_review_ui_uses_curated_public_events(self):
        archive = self.replays / "20261029_fixture"
        archive.mkdir()
        manifest = {
            "metadata": {
                "game_id": "fixture-game", "started_at": "2026-10-29T01:00:00+08:00",
                "archived_at": "2026-10-29T01:10:00+08:00", "status": "completed",
                "winner": "好人", "board_name": "验收测试板", "good_model": "offline-good",
                "wolf_model": "offline-wolf", "day": 2,
            },
            "api_quality": {
                "requests": 4, "success": 3, "fallback": 1, "canceled": 0,
                "prompt_tokens": 80, "completion_tokens": 43, "total_tokens": 123,
                "fallback_rate": 0.25,
            },
            "analysis": {
                "observed_metrics": {
                    "good_vote_accuracy": {
                        "label": "好人投狼比例", "value": 0.5, "numerator": 1,
                        "denominator": 2, "definition": "离线验收样本",
                    },
                },
                "prompt_tuning_flags": ["仅用于验收的提示"],
            },
        }
        (archive / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        (archive / "summary.md").write_text(
            "# 私密复盘摘要\n仅供房主查看", encoding="utf-8"
        )
        events = [
            {
                "seq": 1, "timestamp": "t1", "event": "player_died",
                "state": {"role": "狼人", "secret": "DO_NOT_RENDER"},
                "role": "狼人", "seat": 3,
            },
            {
                "seq": 2, "timestamp": "t2", "event": "public_log",
                "state": {"role": "狼人", "secret": "DO_NOT_RENDER"},
                "entry": {
                    "kind": "player", "phase": "第1天", "seat": 4,
                    "text": "公开发言样本", "nested_secret": "DO_NOT_RENDER",
                },
            },
            {
                "seq": 3, "timestamp": "t3", "event": "public_log",
                "entry": {"kind": "god", "text": "DO_NOT_RENDER"},
            },
        ]
        (archive / "events.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in events),
            encoding="utf-8",
        )

        self.app.run()
        self.assert_clean()
        self.click("读取结构化复盘")

        self.assertTrue(any("验收测试板 · 好人" in item.value for item in self.app.subheader))
        self.assertTrue(any(item.label == "总 Token" and item.value == "123" for item in self.app.metric))
        rendered = "\n".join(
            [item.value for item in self.app.markdown]
            + [item.value for item in self.app.text]
        )
        self.assertIn("公开发言样本", rendered)
        self.assertIn("仅供房主查看", rendered)
        self.assertNotIn("DO_NOT_RENDER", rendered)
        self.assert_clean()

if __name__ == "__main__":
    unittest.main()
