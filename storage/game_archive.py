# -*- coding: utf-8 -*-
"""对局档案 V2：实时取证流、终局摘要、质量标记与跨局索引。"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from engine import config
from storage.replay_recorder import GameRecorder, SCHEMA_VERSION


EXPLOSION_RE = re.compile(r"^\s*-?\s*💥\s*(?:狼人自爆|白狼王)(?:[：:\s]|$)")


INDEX_COLUMNS = [
    "game_id", "started_at", "archived_at", "status", "winner", "board_name",
    "day", "sheriff", "good_model", "wolf_model", "api_quality", "api_requests",
    "api_success", "api_fallback", "api_canceled", "fallback_rate", "prompt_tokens",
    "completion_tokens", "total_tokens", "cache_hit_tokens", "cache_miss_tokens",
    "reasoning_tokens", "unmetered_returns", "summary_requests", "summary_total_tokens",
    "exile_rounds", "exiled_wolves",
    "wolf_explosions", "good_vote_accuracy", "good_vote_cohesion", "archive_path",
    "api_attempt_count", "retried_actions", "good_active_abstention_rate", "good_technical_misses",
]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _seat(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if 1 <= number <= 12 else 0


def _role_camp(engine, seat: int, role_name: str = "") -> str:
    role = (getattr(engine, "roles", {}) or {}).get(seat)
    if role is not None and getattr(role, "camp", None):
        return role.camp
    role_class = config.ROLE_CLASSES.get(role_name)
    return role_class.camp if role_class is not None else "好人"


def _pct(value) -> str:
    return "N/A（无有效样本）" if value is None else f"{value:.1%}"


def _usage_values(usage: dict | None) -> dict:
    """把 DeepSeek/OpenAI usage 统一成可加总的整数指标。"""
    usage = usage or {}

    def as_int(value) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    prompt = as_int(usage.get("prompt_tokens"))
    completion = as_int(usage.get("completion_tokens"))
    total = as_int(usage.get("total_tokens")) or (prompt + completion)
    prompt_details = usage.get("prompt_tokens_details") or {}
    completion_details = usage.get("completion_tokens_details") or {}
    cache_hit = as_int(usage.get("prompt_cache_hit_tokens")) or as_int(prompt_details.get("cached_tokens"))
    cache_miss = as_int(usage.get("prompt_cache_miss_tokens"))
    if not cache_miss and prompt:
        cache_miss = max(0, prompt - cache_hit)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
        "cache_hit_tokens": cache_hit,
        "cache_miss_tokens": cache_miss,
        "reasoning_tokens": as_int(completion_details.get("reasoning_tokens")),
    }


def _sanitize_base_url(value: str) -> str:
    """Keep endpoint diagnostics without persisting URL userinfo, query or fragment."""
    try:
        parsed = urlsplit(str(value or ""))
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            return ""
        host = hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        if port is not None:
            host = f"{host}:{port}"
        return urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))
    except (TypeError, ValueError):
        return ""


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _metrics(engine) -> dict:
    public_log = list(getattr(engine, "public_log", []) or [])
    speeches = Counter()
    vote_rounds = []
    for item in public_log:
        if item.get("kind") == "player":
            speeches[str(item.get("seat"))] += 1
        elif item.get("kind") == "votechart":
            vote_rounds.append({
                "title": item.get("title", ""),
                "totals": item.get("totals", {}),
                "votes": item.get("votes", {}),
                "winner": item.get("winner"),
                "sheriff": item.get("sheriff"),
                "decision_sources": _vote_sources(engine, item),
            })
    players = getattr(engine, "players", {}) or {}
    alive = sorted(int(s) for s, p in players.items() if p.get("alive"))
    dead = sorted(int(s) for s, p in players.items() if not p.get("alive"))
    return {
        "speech_count_by_seat": dict(speeches),
        "vote_rounds": vote_rounds,
        "final_alive": alive,
        "final_dead": dead,
    }


def _vote_sources(engine, item):
    sources = {str(k): v for k, v in (item.get("decision_sources") or {}).items()}
    # 兼容旧归档：按日和座位关联真实行动，绝不把所有0票推断为主动弃票。
    day = re.search(r"第(\d+)天", str(item.get("title", "")))
    if day and "放逐" in str(item.get("title", "")):
        for seat, rows in (getattr(engine, "reasoning_log", {}) or {}).items():
            row = next((r for r in reversed(rows) if r.get("label") == f"D{day[1]} 暗投"), None)
            if row and str(seat) not in sources:
                response = row.get("response") or {}
                sources[str(seat)] = response.get("decision_source") or (
                    "technical_fallback" if response.get("_is_fallback") else "model")
    return sources


def _strategic_analysis(engine, metrics: dict, roles: dict) -> dict:
    good_valid = good_wolf_hits = good_abstain = good_same_target = 0
    active_abstain = technical_misses = manual_skips = invalid_targets = unknown_abstain = 0
    wolf_valid = wolf_good_hits = 0
    exile_rounds = []
    exiled_wolves = exiled_goods = 0

    for round_data in metrics["vote_rounds"]:
        title = str(round_data.get("title", ""))
        if "放逐" not in title:
            continue
        good_targets = Counter()
        round_good_valid = round_good_hits = round_good_abstain = 0
        for raw_voter, raw_target in (round_data.get("votes") or {}).items():
            voter, target = _seat(raw_voter), _seat(raw_target)
            if not voter:
                continue
            voter_camp = _role_camp(engine, voter, roles.get(str(voter), ""))
            if voter_camp == "狼人":
                if target:
                    wolf_valid += 1
                    if _role_camp(engine, target, roles.get(str(target), "")) == "好人":
                        wolf_good_hits += 1
                continue
            if not target:
                good_abstain += 1
                round_good_abstain += 1
                source = (round_data.get("decision_sources") or {}).get(str(voter), "unknown")
                active_abstain += source == "model"
                technical_misses += source == "technical_fallback"
                manual_skips += source == "manual_skip"
                invalid_targets += source == "invalid_target"
                unknown_abstain += source == "unknown"
                continue
            good_valid += 1
            round_good_valid += 1
            good_targets[target] += 1
            if _role_camp(engine, target, roles.get(str(target), "")) == "狼人":
                good_wolf_hits += 1
                round_good_hits += 1
        peak = max(good_targets.values(), default=0)
        good_same_target += peak
        out = _seat(round_data.get("winner"))
        if out:
            if _role_camp(engine, out, roles.get(str(out), "")) == "狼人":
                exiled_wolves += 1
            else:
                exiled_goods += 1
        exile_rounds.append({
            "title": title,
            "winner": out or None,
            "good_valid_votes": round_good_valid,
            "good_wolf_hits": round_good_hits,
            "good_abstentions": round_good_abstain,
            "good_max_same_target": peak,
            "votes": round_data.get("votes", {}),
            "totals": round_data.get("totals", {}),
            "decision_sources": round_data.get("decision_sources", {}),
        })

    structured_explosions = getattr(engine, "explosion_events", None)
    if structured_explosions is not None:
        explosions = len(structured_explosions)
    else:
        explosions = sum(
            bool(EXPLOSION_RE.match(str(line)))
            for line in (getattr(engine, "replay_logs", []) or [])
        )
    good_accuracy = good_wolf_hits / good_valid if good_valid else None
    good_cohesion = good_same_target / good_valid if good_valid else None
    good_abstention = good_abstain / (good_valid + good_abstain) if good_valid + good_abstain else None
    active_rate = active_abstain / (good_valid + active_abstain) if good_valid + active_abstain else None
    wolf_attack = wolf_good_hits / wolf_valid if wolf_valid else None
    alive = set(metrics["final_alive"])
    final_wolves = sorted(
        seat for seat in alive if _role_camp(engine, seat, roles.get(str(seat), "")) == "狼人"
    )

    flags = []
    if good_accuracy is not None and good_accuracy < 0.5:
        flags.append("好人放逐票命中狼人比例偏低，应强化身份交叉验证、票型复盘和临投纠错。")
    if good_cohesion is not None and good_cohesion < 0.6:
        flags.append("好人有效票集中度偏低，应在独立审计后强化证据一致时的协同收票，避免盲从未经验证的警长。")
    if active_rate is not None and active_rate > 0.15:
        flags.append("好人弃票率偏高，关键轮次应明确二选一并承担投票责任。")
    if getattr(engine, "winner", None) == "狼人" and len(final_wolves) >= 3:
        flags.append("狼人高存活获胜，好人阵营未形成有效身份链和狼坑收敛。")
    if technical_misses:
        flags.append(f"好人发生{technical_misses}次技术失票，需先排除调用故障，不应解释为主动躲表态。")
    if not good_valid and not good_abstain and not wolf_valid:
        flags.append("尚无可统计的放逐票型，无法评估投票表现。")
    elif not flags:
        flags.append("当前票型未触发预设阈值；这不代表已验证模型的推理、识谎或纠错能力。")
    # 仅输出能由本局票型直接计数的描述性统计。0 是有效观测；无分母则为 None。
    observed_metrics = {}
    for key, label, numerator, denominator, definition in (
        ("good_vote_accuracy", "好人投狼比例", good_wolf_hits, good_valid,
         "投向狼人的好人有效放逐票 / 好人全部有效放逐票"),
        ("good_vote_cohesion", "好人票型集中度", good_same_target, good_valid,
         "每轮好人投向同一目标的最多票数之和 / 好人全部有效放逐票"),
        ("good_abstention_rate", "好人弃票率", good_abstain, good_valid + good_abstain,
         "好人弃票数 / 好人全部放逐投票记录数"),
        ("good_active_abstention_rate", "好人主动弃票率", active_abstain, good_valid + active_abstain,
         "正常返回的主动弃票 / 好人正常返回的有效投票与主动弃票"),
        ("good_technical_miss_rate", "好人技术失票率", technical_misses, good_valid + good_abstain,
         "技术失败造成的失票 / 好人全部放逐投票记录数"),
        ("wolf_vote_good_rate", "狼人投好人比例", wolf_good_hits, wolf_valid,
         "投向好人的狼人有效放逐票 / 狼人全部有效放逐票"),
    ):
        observed_metrics[key] = {
            "label": label, "numerator": numerator, "denominator": denominator,
            "value": round(numerator / denominator, 4) if denominator else None,
            "definition": definition,
        }

    return {
        "good_vote_accuracy": round(good_accuracy, 4) if good_accuracy is not None else None,
        "good_vote_cohesion": round(good_cohesion, 4) if good_cohesion is not None else None,
        "good_abstention_rate": round(good_abstention, 4) if good_abstention is not None else None,
        "good_active_abstention_rate": round(active_rate, 4) if active_rate is not None else None,
        "vote_provenance": {"active_abstentions": active_abstain, "technical_misses": technical_misses,
                            "manual_skips": manual_skips, "invalid_targets": invalid_targets,
                            "unknown_abstentions": unknown_abstain},
        "wolf_vote_good_rate": round(wolf_attack, 4) if wolf_attack is not None else None,
        "exile_round_count": len(exile_rounds),
        "exiled_wolves": exiled_wolves,
        "exiled_goods": exiled_goods,
        "wolf_explosions": explosions,
        "final_alive_wolves": final_wolves,
        "exile_rounds": exile_rounds,
        "metrics_schema_version": 2,
        "observed_metrics": observed_metrics,
        # 保留旧字段，明确表示未测量，避免旧消费者误用编造的综合分。
        "tactical_radar": None,
        "prompt_tuning_flags": flags,
    }


def game_statistics(engine) -> dict:
    """界面和归档共用同一套计数与分母；不推断模型的心理或综合能力。"""
    roles = {str(s): p.get("role", "") for s, p in engine.players.items()}
    return _strategic_analysis(engine, _metrics(engine), roles)


def _usage_totals(api_rows: list[dict]) -> dict:
    completed = [r for r in api_rows if r.get("record_type") == "completed"]
    player_submitted = {
        str(r.get("request_id")) for r in api_rows
        if r.get("record_type") == "submitted"
        and r.get("response_kind") not in {"memory_summary", "diagnostic"}
    }
    canceled_ids = {
        str(r.get("request_id")) for r in api_rows
        if r.get("record_type") == "canceled"
    }
    player = [
        r for r in completed
        if r.get("response_kind") not in {"memory_summary", "diagnostic"}
        and str(r.get("request_id")) not in canceled_ids
    ]
    success = len({str(r.get("request_id")) for r in player if r.get("status") == "success"})
    fallback = len({str(r.get("request_id")) for r in player if r.get("status") == "fallback"})
    canceled = len(canceled_ids & player_submitted)
    tokens = Counter()
    by_kind = {}
    unmetered_returns = 0
    receipts, uncertain = {}, set()
    for row in completed:
        kind = str(row.get("response_kind") or "unknown")
        attempts = row.get("attempts") or []
        if attempts:
            for index, attempt in enumerate(attempts, 1):
                key = f"{row.get('request_id')}:{attempt.get('attempt', index)}"
                if attempt.get("usage") or attempt.get("response_received"):
                    receipts.setdefault(key, (kind, attempt.get("usage")))
                elif attempt.get("category") in {"timeout", "read_timeout", "connect_timeout"}:
                    uncertain.add(key)
        else:
            key = f"{row.get('request_id')}:1"
            if row.get("usage"):
                receipts.setdefault(key, (kind, row["usage"]))
            elif (row.get("category") or (row.get("diagnostic") or {}).get("category")) in {
                    "timeout", "read_timeout", "connect_timeout"}:
                uncertain.add(key)
            else:
                receipts.setdefault(key, (kind, None))
    for row in api_rows:
        if row.get("record_type") == "late_usage":
            key = row.get("attempt_key") or f"{row.get('request_id')}:1"
            receipts.setdefault(key, (str(row.get("response_kind") or "unknown"), row.get("usage")))
    uncertain.difference_update(receipts)
    for kind, usage in receipts.values():
        counter = by_kind.setdefault(kind, Counter(requests=0))
        counter["requests"] += 1
        if not usage:
            counter["unmetered_returns"] += 1
            unmetered_returns += 1
        for key, amount in _usage_values(usage).items():
            tokens[key] += amount
            counter[key] += amount
    requests = len(player_submitted)
    fallback_rate = (fallback + canceled) / requests if requests else None
    circuit = any(bool(r.get("circuit_breaker")) for r in completed)
    if not requests:
        quality = "unknown"
    elif circuit or fallback_rate > 0.30:
        quality = "invalid"
    elif fallback or canceled:
        quality = "degraded"
    else:
        quality = "valid"
    return {
        "quality": quality,
        "attempt_count": sum(len(r.get("attempts") or []) for r in player),
        "retried_actions": sum(len(r.get("attempts") or []) > 1 for r in player),
        "attempt_errors": dict(Counter(a.get("category", "unknown") for r in completed
                                       for a in (r.get("attempts") or []) if a.get("status") == "error")),
        "affected_actions": [{"request_id": r.get("request_id"), "seat": r.get("seat"),
                              "day": r.get("day"), "phase": r.get("phase"), "kind": r.get("response_kind")}
                             for r in player if r.get("status") != "success"],
        "requests": requests,
        "success": success,
        "fallback": fallback,
        "canceled": canceled,
        "fallback_rate": round(fallback_rate, 4) if fallback_rate is not None else None,
        "circuit_breaker": circuit,
        "prompt_tokens": tokens["prompt_tokens"],
        "completion_tokens": tokens["completion_tokens"],
        "total_tokens": tokens["total_tokens"],
        "cache_hit_tokens": tokens["cache_hit_tokens"],
        "cache_miss_tokens": tokens["cache_miss_tokens"],
        "reasoning_tokens": tokens["reasoning_tokens"],
        "unmetered_returns": unmetered_returns,
        "unknown_requests": len(uncertain),
        "summary_requests": by_kind.get("memory_summary", {}).get("requests", 0),
        "summary_total_tokens": by_kind.get("memory_summary", {}).get("total_tokens", 0),
        "by_response_kind": {kind: dict(counter) for kind, counter in sorted(by_kind.items())},
    }


def _source_snapshot(engine=None) -> dict:
    from engine.runtime_version import snapshot
    root = Path(__file__).resolve().parent
    try:
        from agent.ai_agent import SUMMARY_TEMPLATE, SYSTEM_TEMPLATE
    except Exception:
        SUMMARY_TEMPLATE = SYSTEM_TEMPLATE = ""
    return {
        "runtime": snapshot(engine),
        "files": {
            name: _sha256_file(root / name)
            for name in ("config.py", "ai_agent.py", "game_engine.py", "roles.py", "app.py",
                         "game_service.py", "game_archive.py", "ui_runtime.py",
                         "ui_design.py", "ui_preview.py", "ui_theme.py", "view_projection.py",
                         "feed_ui.py", "live_feed.py", "model_protocol.py", "public_memory.py", "runtime_version.py",
                         "room_view.py", "room_registry.py", "replay_frames.py", "replay_reader.py", "spectator_ui.py")
        },
        "prompts": {
            "system_template_sha256": _sha256_text(SYSTEM_TEMPLATE),
            "summary_template_sha256": _sha256_text(SUMMARY_TEMPLATE),
            "system_template": SYSTEM_TEMPLATE, "summary_template": SUMMARY_TEMPLATE,
        },
    }


def _summary_markdown(metadata, players, metrics, analysis, api_quality, replay_text) -> str:
    winner = metadata.get("winner") or "未完成"
    status = "已完成" if metadata.get("status") == "completed" else "中断/未完成"
    quality_name = {
        "valid": "有效", "degraded": "降级可用", "invalid": "无效样本", "unknown": "未知",
    }.get(api_quality["quality"], api_quality["quality"])
    lines = [
        "# 赛博狼人杀对局复盘", "",
        f"- 对局 ID：`{metadata['game_id']}`",
        f"- 状态：{status}",
        f"- 胜者：{winner}",
        f"- 板子：{metadata.get('board_name') or '未知'}",
        f"- 开始时间：{metadata.get('started_at')}",
        f"- 归档时间：{metadata.get('archived_at')}",
        f"- API 样本质量：**{quality_name}**",
        "",
        "## 模型调用质量", "",
        f"- 玩家请求：{api_quality['requests']}（成功 {api_quality['success']}，兜底 {api_quality['fallback']}，取消 {api_quality['canceled']}）",
        f"- 兜底率：{_pct(api_quality['fallback_rate'])}",
        f"- 玩家调用尝试：{api_quality.get('attempt_count', 0)} / 发生重试的行动：{api_quality.get('retried_actions', 0)}",
        f"- 尝试失败分类：{api_quality.get('attempt_errors', {})}",
        f"- Token：输入 {api_quality['prompt_tokens']} / 输出 {api_quality['completion_tokens']} / 合计 {api_quality['total_tokens']}",
        f"- Prompt 缓存：命中 {api_quality['cache_hit_tokens']} / 未命中 {api_quality['cache_miss_tokens']}",
        f"- 隐式推理 Token：{api_quality['reasoning_tokens']}",
        f"- 记忆摘要：{api_quality['summary_requests']} 次 / {api_quality['summary_total_tokens']} Token",
        f"- 未返回 usage 的响应：{api_quality['unmetered_returns']} 次（实际消耗可能略高）",
        "",
        "## 真身与终局", "",
        "| 座位 | 身份 | 阵营 | 终局状态 |",
        "|---:|---|---|---|",
    ]
    for seat in sorted(players, key=lambda x: int(x)):
        p = players[seat]
        lines.append(f"| {seat} | {p['role']} | {p['camp']} | {'存活' if p['alive'] else '死亡'} |")
    lines += [
        "", "## 票型指标", "",
        f"- 放逐轮数：{analysis['exile_round_count']}",
        f"- 放逐狼人 / 好人：{analysis['exiled_wolves']} / {analysis['exiled_goods']}",
        f"- 好人投狼比例：{_pct(analysis['good_vote_accuracy'])}",
        f"- 好人票型集中度：{_pct(analysis['good_vote_cohesion'])}",
        f"- 好人原始弃票率：{_pct(analysis['good_abstention_rate'])}",
        f"- 好人主动弃票率：{_pct(analysis.get('good_active_abstention_rate'))}",
        f"- 失票归因：{analysis.get('vote_provenance', {})}",
        "", "### 每轮完整票型", "",
    ]
    if analysis["exile_rounds"]:
        for item in analysis["exile_rounds"]:
            vote_text = "，".join(
                f"{v}号→{t}号" if _seat(t) else f"{v}号→未投票（来源：{(item.get('decision_sources') or {}).get(str(v), 'unknown')}）"
                for v, t in (item.get("votes") or {}).items()
            ) or "无有效票"
            lines.append(f"- {item['title']}：{vote_text}；出局：{item.get('winner') or '无人'}")
    else:
        lines.append("- N/A（无有效放逐票型）")
    lines += ["", "### 指标计数与定义", ""]
    for metric in analysis.get("observed_metrics", {}).values():
        lines.append(
            f"- {metric['label']}：{_pct(metric['value'])}"
            f"（{metric['numerator']} / {metric['denominator']}）；{metric['definition']}。"
        )
    lines.append("- 以上是本局描述性票型统计，不代表识谎、事实保真度或 Token 效能的实测综合评级。")
    lines += ["", "## Prompt 调整线索", ""]
    lines.extend(f"- {flag}" for flag in analysis["prompt_tuning_flags"])
    lines += ["", "## 引擎关键复盘", "", replay_text.rstrip() or "（暂无关键事件）", ""]
    return "\n".join(lines)


def should_archive(engine) -> bool:
    return bool(engine and (
        getattr(engine, "winner", None)
        or int(getattr(engine, "day", 0) or 0) > 0
        or getattr(engine, "public_log", None)
        or getattr(engine, "replay_logs", None)
    ))


def archive_game(engine, directory, reason: str = "game_end", force: bool = False) -> dict | None:
    """写出终局快照和摘要；过程流由 GameRecorder 在对局中实时追加。"""
    if not should_archive(engine):
        return None
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    engine.game_id = getattr(engine, "game_id", None) or uuid.uuid4().hex[:12]
    engine.started_at = getattr(engine, "started_at", None) or _now_iso()

    recorder = getattr(engine, "recorder", None)
    if recorder is None:
        if hasattr(engine, "enable_recording"):
            recorder = engine.enable_recording(root)
        else:
            recorder = GameRecorder(root, engine.game_id, engine.started_at, getattr(engine, "archive_basename", None))
            engine.archive_basename = recorder.basename
    completed = bool(getattr(engine, "winner", None))
    paths = recorder.paths()
    manifest_path = Path(paths["manifest"])
    if completed and getattr(engine, "archive_complete", False) and manifest_path.exists() and not force:
        return {
            "markdown": paths["summary"], "json": paths["final_state"],
            "manifest": paths["manifest"], "directory": paths["directory"],
            "completed": True, "game_id": engine.game_id,
        }

    manager = getattr(engine, "manager", None)
    players = {
        str(s): {
            "role": p.get("role", "未知"),
            "camp": _role_camp(engine, int(s), p.get("role", "")),
            "alive": bool(p.get("alive")),
            "style": p.get("style", ""),
        }
        for s, p in (getattr(engine, "players", {}) or {}).items()
    }
    roles = {seat: p["role"] for seat, p in players.items()}
    metrics = _metrics(engine)
    analysis = _strategic_analysis(engine, metrics, roles)
    api_quality = _usage_totals(recorder.read_jsonl("api_calls"))
    archived_at = _now_iso()
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "game_id": engine.game_id,
        "started_at": engine.started_at,
        "archived_at": archived_at,
        "archive_reason": reason,
        "status": "completed" if completed else "interrupted",
        "winner": getattr(engine, "winner", None),
        "day": int(getattr(engine, "day", 0) or 0),
        "phase": getattr(engine, "phase", ""),
        "board_name": getattr(engine, "board_name", "") or getattr(engine, "_roster", ""),
        "sheriff": getattr(engine, "sheriff", None),
        "random_seed": getattr(engine, "random_seed", None),
        "good_model": getattr(manager, "good_model", "") if manager else "",
        "wolf_model": getattr(manager, "wolf_model", "") if manager else "",
    }
    config_snapshot = {
        "base_url": _sanitize_base_url(getattr(manager, "base_url", config.BASE_URL) if manager else config.BASE_URL),
        "good_model": metadata["good_model"],
        "wolf_model": metadata["wolf_model"],
        "fallback_model": getattr(manager, "fallback_model", "") if manager else "",
        "temperature": getattr(manager, "temperature", None) if manager else None,
        "agent_timeout": getattr(manager, "agent_timeout", None) if manager else None,
        "max_retries": getattr(manager, "max_retries", None) if manager else None,
        "react_budget": getattr(engine, "react_budget", None),
    }
    replay_text = engine.export_replay_text() if hasattr(engine, "export_replay_text") else ""
    final_state = {
        "metadata": metadata,
        "players": players,
        "metrics": metrics,
        "analysis": analysis,
        "api_quality": api_quality,
        "replay_logs": list(getattr(engine, "replay_logs", []) or []),
        "public_log": list(getattr(engine, "public_log", []) or []),
        "reasoning_log": getattr(engine, "reasoning_log", {}) or {},
        "engine_state": engine.export_state() if hasattr(engine, "export_state") else {},
        "manager_state": manager.export_state() if manager and hasattr(manager, "export_state") else {},
    }
    summary = _summary_markdown(metadata, players, metrics, analysis, api_quality, replay_text)
    manifest = {
        "metadata": metadata,
        "config": config_snapshot,
        "source_snapshot": _source_snapshot(engine),
        "api_quality": api_quality,
        "analysis": analysis,
        "files": {k: os.path.basename(v) for k, v in paths.items() if k != "directory"},
    }
    display_path = recorder.directory / "replay_manifest.json"
    if display_path.exists():
        try:
            manifest["display_replay"] = json.loads(display_path.read_text(encoding="utf-8"))
            for name in ("replay_frames.jsonl", "replay_items.jsonl", "replay_manifest.json"):
                manifest["files"][name.removesuffix(".jsonl").removesuffix(".json")] = name
        except (OSError, ValueError):
            manifest["display_replay"] = {"status": "unreadable"}
    recorder.record_event("archive_snapshot", reason=reason, completed=completed, api_quality=api_quality)
    recorder.write_json("final_state.json", final_state)
    recorder.write_json("manifest.json", manifest)
    recorder.write_text("summary.md", summary)
    recorder.log("INFO", f"ARCHIVE completed={completed} reason={reason} quality={api_quality['quality']}")
    if completed:
        engine.archive_complete = True
    rebuild_index(root)
    return {
        "markdown": paths["summary"], "json": paths["final_state"],
        "manifest": paths["manifest"], "directory": paths["directory"],
        "completed": completed, "game_id": engine.game_id,
    }


def _index_row_from_manifest(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    meta = data.get("metadata", {})
    analysis = data.get("analysis", {})
    api = data.get("api_quality", {})
    return {
        "game_id": meta.get("game_id", ""), "started_at": meta.get("started_at", ""),
        "archived_at": meta.get("archived_at", ""), "status": meta.get("status", ""),
        "winner": meta.get("winner") or "", "board_name": meta.get("board_name", ""),
        "day": meta.get("day", ""), "sheriff": meta.get("sheriff") or "",
        "good_model": meta.get("good_model", ""), "wolf_model": meta.get("wolf_model", ""),
        "api_quality": api.get("quality", "unknown"), "api_requests": api.get("requests", ""),
        "api_success": api.get("success", ""), "api_fallback": api.get("fallback", ""),
        "api_canceled": api.get("canceled", ""), "fallback_rate": api.get("fallback_rate", ""),
        "prompt_tokens": api.get("prompt_tokens", ""), "completion_tokens": api.get("completion_tokens", ""),
        "total_tokens": api.get("total_tokens", ""), "cache_hit_tokens": api.get("cache_hit_tokens", ""),
        "cache_miss_tokens": api.get("cache_miss_tokens", ""), "reasoning_tokens": api.get("reasoning_tokens", ""),
        "unmetered_returns": api.get("unmetered_returns", ""),
        "summary_requests": api.get("summary_requests", ""), "summary_total_tokens": api.get("summary_total_tokens", ""),
        "exile_rounds": analysis.get("exile_round_count", ""),
        "exiled_wolves": analysis.get("exiled_wolves", ""), "wolf_explosions": analysis.get("wolf_explosions", ""),
        "good_vote_accuracy": analysis.get("good_vote_accuracy", ""),
        "good_vote_cohesion": analysis.get("good_vote_cohesion", ""),
        "archive_path": str(path.parent),
        "api_attempt_count": api.get("attempt_count", ""), "retried_actions": api.get("retried_actions", ""),
        "good_active_abstention_rate": analysis.get("good_active_abstention_rate", ""),
        "good_technical_misses": (analysis.get("vote_provenance") or {}).get("technical_misses", ""),
    }


def _index_row_from_v1(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    meta = data.get("metadata", {})
    if not meta.get("game_id"):
        return None
    analysis = data.get("analysis", {})
    roles = data.get("roles", {}) or {}
    wolf_roles = {"狼人", "白狼王", "狼美人"}
    exile_rounds = [
        item for item in ((data.get("metrics") or {}).get("vote_rounds") or [])
        if "放逐" in str(item.get("title", ""))
    ]
    exiled_wolves = sum(
        roles.get(str(_seat(item.get("winner")))) in wolf_roles
        for item in exile_rounds if _seat(item.get("winner"))
    )
    structured_explosions = (data.get("engine_state") or {}).get("explosion_events")
    if isinstance(structured_explosions, list):
        wolf_explosions = len(structured_explosions)
    else:
        wolf_explosions = sum(
            bool(EXPLOSION_RE.match(str(line))) for line in (data.get("replay_logs") or [])
        )
    return {
        "game_id": meta.get("game_id", ""), "started_at": meta.get("started_at", ""),
        "archived_at": meta.get("archived_at", ""), "status": meta.get("status", ""),
        "winner": meta.get("winner") or "", "board_name": meta.get("board_name", ""),
        "day": meta.get("day", ""), "sheriff": meta.get("sheriff") or "",
        "good_model": meta.get("good_model", ""), "wolf_model": meta.get("wolf_model", ""),
        "api_quality": "legacy_unknown", "exile_rounds": len(exile_rounds),
        "exiled_wolves": exiled_wolves, "wolf_explosions": wolf_explosions,
        "good_vote_accuracy": analysis.get("good_vote_accuracy", ""),
        "good_vote_cohesion": analysis.get("good_vote_cohesion", ""),
        "archive_path": str(path),
    }


def rebuild_index(directory) -> Path:
    """重建跨局 CSV；同时兼容旧版根目录 JSON 与 V2 每局 manifest。"""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in root.glob("*/manifest.json"):
        row = _index_row_from_manifest(path)
        if row:
            rows.append(row)
    for path in root.glob("*.json"):
        row = _index_row_from_v1(path)
        if row:
            rows.append(row)
    rows.sort(key=lambda row: str(row.get("started_at", "")), reverse=True)
    target = root / "index.csv"
    temp = root / f"index.{uuid.uuid4().hex}.tmp"
    with temp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=INDEX_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    os.replace(temp, target)
    return target
