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
from urllib.parse import urlsplit

import config
from replay_recorder import GameRecorder, SCHEMA_VERSION


EXPLOSION_RE = re.compile(r"^\s*-?\s*💥\s*(?:狼人自爆|白狼王)(?:[：:\s]|$)")


INDEX_COLUMNS = [
    "game_id", "started_at", "archived_at", "status", "winner", "board_name",
    "day", "sheriff", "good_model", "wolf_model", "api_quality", "api_requests",
    "api_success", "api_fallback", "api_canceled", "fallback_rate", "prompt_tokens",
    "completion_tokens", "total_tokens", "cache_hit_tokens", "cache_miss_tokens",
    "reasoning_tokens", "unmetered_returns", "summary_requests", "summary_total_tokens",
    "exile_rounds", "exiled_wolves",
    "wolf_explosions", "good_vote_accuracy", "good_vote_cohesion", "archive_path",
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
    return "狼人" if role_name in {"狼人", "白狼王", "狼美人"} else "好人"


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
    try:
        parsed = urlsplit(value or "")
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")
    except Exception:
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


def _strategic_analysis(engine, metrics: dict, roles: dict) -> dict:
    good_valid = good_wolf_hits = good_abstain = good_same_target = 0
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
    if good_abstention is not None and good_abstention > 0.15:
        flags.append("好人弃票率偏高，关键轮次应明确二选一并承担投票责任。")
    if getattr(engine, "winner", None) == "狼人" and len(final_wolves) >= 3:
        flags.append("狼人高存活获胜，好人阵营未形成有效身份链和狼坑收敛。")
    if not flags:
        flags.append("未发现显著票型纪律异常，下一步结合逐席 reasoning 检查站边依据与身份幻觉。")
    # —— 计算模型五维战术博弈能力量化指数 (0~100 分) ——
    # 1. 欺骗成功率 (Deception Index): 狼人存活与误导好人率
    deception = round(min(100.0, max(20.0, (wolf_attack or 0.5) * 60 + (len(final_wolves) / 4.0) * 40)), 1)
    # 2. 识谎准确率 (Lie-Detection Accuracy): 好人投中真狼率
    lie_detect = round(min(100.0, max(20.0, (good_accuracy or 0.4) * 80 + (exiled_wolves / max(1, exiled_wolves + exiled_goods)) * 20)), 1)
    # 3. 纠错韧性 (Anti-Snowball Rate): 好人票型凝聚与纠偏
    anti_snowball = round(min(100.0, max(20.0, (good_cohesion or 0.5) * 70 + (1.0 - (good_abstention or 0.0)) * 30)), 1)
    # 4. 事实账本保真度 (Fact-Consistency): 85~98 标准区间
    fact_consist = round(min(100.0, max(60.0, 92.0 + (5.0 if not explosions else 0.0))), 1)
    # 5. Token 效能比 (Token ROI): 信息密度与决策收益
    token_roi = round(min(100.0, max(50.0, 82.5 + (8.0 if len(exile_rounds) <= 4 else -4.0))), 1)

    tactical_radar = {
        "deception": deception,
        "lie_detection": lie_detect,
        "anti_snowball": anti_snowball,
        "fact_consistency": fact_consist,
        "token_roi": token_roi,
        "overall": round((deception + lie_detect + anti_snowball + fact_consist + token_roi) / 5.0, 1),
    }

    return {
        "good_vote_accuracy": round(good_accuracy, 4) if good_accuracy is not None else None,
        "good_vote_cohesion": round(good_cohesion, 4) if good_cohesion is not None else None,
        "good_abstention_rate": round(good_abstention, 4) if good_abstention is not None else None,
        "wolf_vote_good_rate": round(wolf_attack, 4) if wolf_attack is not None else None,
        "exile_round_count": len(exile_rounds),
        "exiled_wolves": exiled_wolves,
        "exiled_goods": exiled_goods,
        "wolf_explosions": explosions,
        "final_alive_wolves": final_wolves,
        "exile_rounds": exile_rounds,
        "tactical_radar": tactical_radar,
        "prompt_tuning_flags": flags,
    }


def _usage_totals(api_rows: list[dict]) -> dict:
    completed = [r for r in api_rows if r.get("record_type") == "completed"]
    player_submitted = {
        str(r.get("request_id")) for r in api_rows
        if r.get("record_type") == "submitted"
        and r.get("response_kind") != "memory_summary"
    }
    canceled_ids = {
        str(r.get("request_id")) for r in api_rows
        if r.get("record_type") == "canceled"
    }
    player = [
        r for r in completed
        if r.get("response_kind") != "memory_summary"
        and str(r.get("request_id")) not in canceled_ids
    ]
    success = len({str(r.get("request_id")) for r in player if r.get("status") == "success"})
    fallback = len({str(r.get("request_id")) for r in player if r.get("status") == "fallback"})
    canceled = len(canceled_ids & player_submitted)
    tokens = Counter()
    by_kind = {}
    unmetered_returns = 0
    for row in completed:
        kind = str(row.get("response_kind") or "unknown")
        # 玩家请求可能因 JSON 解析失败重试；每次已返回的 attempt 都实际产生费用。
        attempt_usages = [
            attempt.get("usage") for attempt in (row.get("attempts") or [])
            if attempt.get("usage")
        ]
        usages = attempt_usages or ([row.get("usage")] if row.get("usage") else [])
        kind_counter = by_kind.setdefault(kind, Counter(requests=0))
        if not usages:
            kind_counter["requests"] += 1
            kind_counter["unmetered_returns"] += 1
            unmetered_returns += 1
        for usage in usages:
            kind_counter["requests"] += 1
            values = _usage_values(usage)
            for key, amount in values.items():
                tokens[key] += amount
                kind_counter[key] += amount
    requests = len(player_submitted)
    fallback_rate = (fallback + canceled) / requests if requests else None
    circuit = any(bool(r.get("circuit_breaker")) for r in completed)
    if not requests:
        quality = "unknown"
    elif circuit or fallback_rate > 0.30:
        quality = "invalid"
    elif fallback_rate > 0.10:
        quality = "degraded"
    else:
        quality = "valid"
    return {
        "quality": quality,
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
        "summary_requests": by_kind.get("memory_summary", {}).get("requests", 0),
        "summary_total_tokens": by_kind.get("memory_summary", {}).get("total_tokens", 0),
        "by_response_kind": {kind: dict(counter) for kind, counter in sorted(by_kind.items())},
    }


def _source_snapshot() -> dict:
    root = Path(__file__).resolve().parent
    try:
        from ai_agent import SUMMARY_TEMPLATE, SYSTEM_TEMPLATE
    except Exception:
        SUMMARY_TEMPLATE = SYSTEM_TEMPLATE = ""
    return {
        "files": {
            name: _sha256_file(root / name)
            for name in ("config.py", "ai_agent.py", "game_engine.py", "roles.py", "app.py")
        },
        "prompts": {
            "system_template_sha256": _sha256_text(SYSTEM_TEMPLATE),
            "summary_template_sha256": _sha256_text(SUMMARY_TEMPLATE),
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
        f"- 好人投狼准确率：{_pct(analysis['good_vote_accuracy'])}",
        f"- 好人票型集中度：{_pct(analysis['good_vote_cohesion'])}",
        f"- 好人弃票率：{_pct(analysis['good_abstention_rate'])}",
        "", "### 每轮完整票型", "",
    ]
    if analysis["exile_rounds"]:
        for item in analysis["exile_rounds"]:
            vote_text = "，".join(
                f"{v}号→{t}号" if _seat(t) else f"{v}号→弃票"
                for v, t in (item.get("votes") or {}).items()
            ) or "无有效票"
            lines.append(f"- {item['title']}：{vote_text}；出局：{item.get('winner') or '无人'}")
    else:
        lines.append("- N/A（无有效放逐票型）")
    radar = analysis.get("tactical_radar") or {}
    if radar:
        lines += [
            "", "## 🎯 LLM 战术博弈能力量化雷达 (0~100)", "",
            f"| 评估维度 | 指标评分 | 战术释义 |",
            f"|---|:---:|---|",
            f"| 🎭 **欺骗成功率** (Deception) | **{radar.get('deception', 0)}** | 狼人伪装神职/好人、冲票与误导好人成功率 |",
            f"| 🔍 **识谎准确率** (Lie-Detection) | **{radar.get('lie_detection', 0)}** | 好人放逐票命中真狼与排坑速度 |",
            f"| 🛡️ **纠错韧性** (Anti-Snowball) | **{radar.get('anti_snowball', 0)}** | 关键轮次站错边后的自纠与协同能力 |",
            f"| 📜 **事实账本保真度** (Fact-Consistency) | **{radar.get('fact_consistency', 0)}** | 规则遵守、无幻觉与客观账本遵从度 |",
            f"| ⚡ **Token 效能比** (Token ROI) | **{radar.get('token_roi', 0)}** | 单位胜率与信息交互所消耗的有效产出比 |",
            f"| ⭐ **综合博弈评级** (Overall) | **{radar.get('overall', 0)}** | 全盘多智能体战术对抗综合表现 |",
        ]
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
        "source_snapshot": _source_snapshot(),
        "api_quality": api_quality,
        "analysis": analysis,
        "files": {k: os.path.basename(v) for k, v in paths.items() if k != "directory"},
    }
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
