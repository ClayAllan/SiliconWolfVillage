"""Offline aggregation for AI arena archives; this module never calls model APIs."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

MAX_MANIFEST_BYTES = 2 * 1024 * 1024
METRIC_KEYS = (
    "good_vote_accuracy",
    "good_vote_cohesion",
    "good_abstention_rate",
    "good_active_abstention_rate",
    "good_technical_miss_rate",
    "wolf_vote_good_rate",
)
QUALITY_VALUES = {"valid", "degraded", "invalid", "unknown"}
STATUS_VALUES = {"completed", "interrupted", "unknown"}

CSV_FIELDS = [
    "board_name", "base_url", "good_model", "wolf_model", "temperature",
    "agent_timeout", "react_budget", "source_fingerprint", "archive_count",
    "completed_archives", "interrupted_archives", "eligible_games", "excluded_archives",
    "valid_archives", "degraded_archives", "invalid_archives", "unknown_quality_archives",
    "good_wins", "wolf_wins", "other_wins", "good_win_rate", "wolf_win_rate", "other_win_rate",
]
for _metric in METRIC_KEYS:
    CSV_FIELDS.extend([f"{_metric}_numerator", f"{_metric}_denominator", f"{_metric}_value", f"{_metric}_games"])


def _safe_text(value, limit=200):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    return value[:limit]


def _nonnegative_int(value):
    return value if type(value) is int and value >= 0 else None


def _safe_base_url(value):
    """Keep endpoint identity but discard userinfo, query and fragment."""
    try:
        parsed = urlsplit(_safe_text(value, 1000))
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        if scheme not in {"http", "https"} or not hostname:
            return "unknown"
        host = hostname.lower()
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        port = parsed.port
        if port is not None:
            host = f"{host}:{port}"
        return urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))
    except (TypeError, ValueError):
        return "unknown"


def _source_fingerprint(source_snapshot):
    if not isinstance(source_snapshot, dict):
        return "unknown"
    runtime = source_snapshot.get("runtime") if isinstance(source_snapshot.get("runtime"), dict) else {}
    files = source_snapshot.get("files") if isinstance(source_snapshot.get("files"), dict) else {}
    prompts = source_snapshot.get("prompts") if isinstance(source_snapshot.get("prompts"), dict) else {}
    selected = {
        "runtime_version_id": _safe_text(runtime.get("version_id"), 128),
        "config.py": _safe_text(files.get("config.py"), 128),
        "game_engine.py": _safe_text(files.get("game_engine.py"), 128),
        "game_archive.py": _safe_text(files.get("game_archive.py"), 128),
        "ai_agent.py": _safe_text(files.get("ai_agent.py"), 128),
        "roles.py": _safe_text(files.get("roles.py"), 128),
        "system_template_sha256": _safe_text(prompts.get("system_template_sha256"), 128),
        "summary_template_sha256": _safe_text(prompts.get("summary_template_sha256"), 128),
    }
    if not any(selected.values()):
        return "unknown"
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _read_record(manifest_path: Path):
    try:
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            return None, "manifest 超过 2 MiB"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "manifest 无法读取或不是有效 JSON"
    if not isinstance(manifest, dict):
        return None, "manifest 根节点不是对象"
    metadata = manifest.get("metadata")
    api = manifest.get("api_quality")
    analysis = manifest.get("analysis")
    config = manifest.get("config") if isinstance(manifest.get("config"), dict) else {}
    source_snapshot = manifest.get("source_snapshot")
    if not isinstance(metadata, dict) or not isinstance(api, dict) or not isinstance(analysis, dict):
        return None, "manifest 缺少 metadata、api_quality 或 analysis"

    quality = _safe_text(api.get("quality"), 32)
    if quality not in QUALITY_VALUES:
        quality = "unknown"
    status = _safe_text(metadata.get("status"), 32)
    if status not in STATUS_VALUES:
        status = "unknown"
    winner = _safe_text(metadata.get("winner"), 80)
    temperature = config.get("temperature")
    if type(temperature) not in (int, float) or not math.isfinite(temperature) or not 0 <= temperature <= 2:
        temperature = None
    else:
        temperature = round(float(temperature), 3)
    timeout = config.get("agent_timeout")
    if type(timeout) is not int or not 1 <= timeout <= 3600:
        timeout = None
    react_budget = config.get("react_budget")
    if type(react_budget) is not int or not 0 <= react_budget <= 100:
        react_budget = None
    source_fingerprint = _source_fingerprint(source_snapshot)
    metrics = {}
    observed = analysis.get("observed_metrics")
    if isinstance(observed, dict):
        for key in METRIC_KEYS:
            metric = observed.get(key)
            if not isinstance(metric, dict):
                continue
            numerator = _nonnegative_int(metric.get("numerator"))
            denominator = _nonnegative_int(metric.get("denominator"))
            if numerator is None or denominator is None or denominator <= 0 or numerator > denominator:
                continue
            value = numerator / denominator
            if not math.isfinite(value) or not 0 <= value <= 1:
                continue
            metrics[key] = {"numerator": numerator, "denominator": denominator}

    return {
        "archive_name": manifest_path.parent.name,
        "game_id": _safe_text(metadata.get("game_id"), 100),
        "archived_at": _safe_text(metadata.get("archived_at"), 80),
        "status": status,
        "quality": quality,
        "winner": winner,
        "board_name": _safe_text(metadata.get("board_name"), 120) or "unknown",
        "base_url": _safe_base_url(config.get("base_url")),
        "good_model": _safe_text(metadata.get("good_model"), 120) or "unknown",
        "wolf_model": _safe_text(metadata.get("wolf_model"), 120) or "unknown",
        "temperature": temperature,
        "agent_timeout": timeout,
        "react_budget": react_budget,
        "source_fingerprint": source_fingerprint,
        "metrics": metrics,
    }, None


def _new_cohort(board, base_url, good_model, wolf_model, temperature, agent_timeout, react_budget, source_fingerprint):
    return {
        "board_name": board,
        "base_url": base_url,
        "good_model": good_model,
        "wolf_model": wolf_model,
        "temperature": temperature,
        "agent_timeout": agent_timeout,
        "react_budget": react_budget,
        "source_fingerprint": source_fingerprint,
        "archive_count": 0,
        "status_counts": Counter(),
        "quality_counts": Counter(),
        "eligible_games": 0,
        "wins": Counter(),
        "metrics": {
            key: {"numerator": 0, "denominator": 0, "games": 0}
            for key in METRIC_KEYS
        },
    }


def build_arena_report(archive_root) -> dict:
    """Aggregate existing manifests only; never load prompts, private state, or API logs."""
    root = Path(archive_root).resolve()
    if not root.is_dir():
        raise ValueError("档案根目录不存在或不是目录")

    records = []
    skipped = []
    seen_game_ids = set()
    # Archives are independent child directories. Avoid following symlinked archives
    # outside the selected root and cap each manifest before parsing.
    paths = sorted(root.glob("*/manifest.json"), key=lambda p: p.parent.name, reverse=True)
    for path in paths:
        try:
            if path.parent.resolve().parent != root or not path.parent.is_dir():
                skipped.append({"archive": path.parent.name[:120], "reason": "档案目录不在所选根目录内"})
                continue
        except OSError:
            skipped.append({"archive": path.parent.name[:120], "reason": "档案路径无法解析"})
            continue
        record, error = _read_record(path)
        if error:
            skipped.append({"archive": path.parent.name[:120], "reason": error})
            continue
        game_id = record["game_id"]
        if game_id and game_id in seen_game_ids:
            skipped.append({"archive": record["archive_name"], "reason": "重复 game_id，避免重复计样"})
            continue
        if game_id:
            seen_game_ids.add(game_id)
        records.append(record)

    cohorts = {}
    quality_counts = Counter()
    status_counts = Counter()
    for record in records:
        key = (
            record["board_name"], record["base_url"], record["good_model"], record["wolf_model"],
            record["temperature"], record["agent_timeout"], record["react_budget"], record["source_fingerprint"],
        )
        cohort = cohorts.setdefault(key, _new_cohort(*key))
        cohort["archive_count"] += 1
        cohort["status_counts"][record["status"]] += 1
        cohort["quality_counts"][record["quality"]] += 1
        quality_counts[record["quality"]] += 1
        status_counts[record["status"]] += 1

        # Primary outcomes require completed games, a known winner, valid API quality,
        # and an identified model pair/board. Degraded/invalid/unknown samples stay visible
        # in quality counts but never enter primary outcome or strategy denominators.
        eligible = (
            record["status"] == "completed"
            and bool(record["winner"])
            and record["quality"] == "valid"
            and record["board_name"] != "unknown"
            and record["base_url"] != "unknown"
            and record["good_model"] != "unknown"
            and record["wolf_model"] != "unknown"
            and record["temperature"] is not None
            and record["agent_timeout"] is not None
            and record["react_budget"] is not None
            and record["source_fingerprint"] != "unknown"
        )
        if not eligible:
            continue
        cohort["eligible_games"] += 1
        if record["winner"] == "好人":
            cohort["wins"]["good"] += 1
        elif record["winner"] == "狼人":
            cohort["wins"]["wolf"] += 1
        else:
            cohort["wins"]["other"] += 1

        for metric_key, metric in record["metrics"].items():
            target = cohort["metrics"][metric_key]
            target["numerator"] += metric["numerator"]
            target["denominator"] += metric["denominator"]
            target["games"] += 1

    cohort_rows = []
    for key in sorted(cohorts):
        cohort = cohorts[key]
        eligible_games = cohort["eligible_games"]
        wins = cohort["wins"]
        row = {
            "board_name": cohort["board_name"],
            "base_url": cohort["base_url"],
            "good_model": cohort["good_model"],
            "wolf_model": cohort["wolf_model"],
            "temperature": cohort["temperature"],
            "agent_timeout": cohort["agent_timeout"],
            "react_budget": cohort["react_budget"],
            "source_fingerprint": cohort["source_fingerprint"],
            "archive_count": cohort["archive_count"],
            "completed_archives": cohort["status_counts"]["completed"],
            "interrupted_archives": cohort["status_counts"]["interrupted"],
            "eligible_games": eligible_games,
            "excluded_archives": cohort["archive_count"] - eligible_games,
            "valid_archives": cohort["quality_counts"]["valid"],
            "degraded_archives": cohort["quality_counts"]["degraded"],
            "invalid_archives": cohort["quality_counts"]["invalid"],
            "unknown_quality_archives": cohort["quality_counts"]["unknown"],
            "good_wins": wins["good"],
            "wolf_wins": wins["wolf"],
            "other_wins": wins["other"],
            "good_win_rate": wins["good"] / eligible_games if eligible_games else None,
            "wolf_win_rate": wins["wolf"] / eligible_games if eligible_games else None,
            "other_win_rate": wins["other"] / eligible_games if eligible_games else None,
            "metrics": {},
        }
        for metric_key, counts in cohort["metrics"].items():
            denominator = counts["denominator"]
            row["metrics"][metric_key] = {
                **counts,
                "value": counts["numerator"] / denominator if denominator else None,
            }
        cohort_rows.append(row)

    return {
        "schema_version": 1,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_root": str(root),
        "policy": {
            "primary_quality": "valid",
            "eligible_status": "completed",
            "requires_known_winner_board_and_model_pair": True,
            "degraded_invalid_unknown_quality_excluded_from_primary_rates": True,
            "aggregates_metrics_by_summing_numerators_and_denominators": True,
            "model_api_calls": 0,
            "paid_experiments_started": False,
        },
        "archive_count": len(records),
        "skipped_count": len(skipped),
        "quality_counts": {key: quality_counts[key] for key in sorted(QUALITY_VALUES)},
        "status_counts": {key: status_counts[key] for key in sorted(STATUS_VALUES)},
        "cohorts": cohort_rows,
        "skipped_archives": skipped[:200],
        "limitations": [
            "按板子与好人/狼人模型组合分组；不同组合之间不是随机化或交叉平衡实验，不能据此给模型做全局排名。",
            "主指标只使用有效 API 质量且已结束、胜者明确的对局；降级、无效、未知质量和中断档案仅计入质量分布。",
            "策略指标按分子/分母汇总，不对各局百分比做简单平均；缺失指标不会按 0 计入。",
            "报告只读取 manifest.json，不调用模型、不读取提示词/私密记忆，也不估算货币成本。",
        ],
    }


def write_arena_report(report: dict, output_dir) -> tuple[Path, Path]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    json_path = output / "arena_report.json"
    csv_path = output / "arena_report.csv"
    json_tmp = json_path.with_suffix(".json.tmp")
    csv_tmp = csv_path.with_suffix(".csv.tmp")
    json_tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    rows = []
    for cohort in report["cohorts"]:
        row = {key: cohort.get(key) for key in CSV_FIELDS if not any(
            key.startswith(metric + "_") for metric in METRIC_KEYS
        )}
        for metric_key in METRIC_KEYS:
            metric = cohort["metrics"][metric_key]
            row[f"{metric_key}_numerator"] = metric["numerator"]
            row[f"{metric_key}_denominator"] = metric["denominator"]
            row[f"{metric_key}_value"] = metric["value"]
            row[f"{metric_key}_games"] = metric["games"]
        rows.append(row)
    with csv_tmp.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(json_tmp, json_path)
    os.replace(csv_tmp, csv_path)
    return json_path, csv_path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="对现有硅基狼村归档做离线竞技场统计；不会发起模型请求。"
    )
    parser.add_argument("archive_root", type=Path, help="包含每局子目录及 manifest.json 的归档根目录")
    parser.add_argument("--output-dir", type=Path, default=None, help="报告输出目录（默认 <archive_root>/analysis）")
    args = parser.parse_args(argv)
    report = build_arena_report(args.archive_root)
    output = args.output_dir or (args.archive_root / "analysis")
    json_path, csv_path = write_arena_report(report, output)
    print(f"读取归档：{report['archive_count']}；跳过：{report['skipped_count']}；分组：{len(report['cohorts'])}")
    print(f"有效样本：{sum(row['eligible_games'] for row in report['cohorts'])}")
    print(f"JSON：{json_path}")
    print(f"CSV：{csv_path}")
    print("离线分析完成；模型 API 调用：0。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
