"""模型能力、输出预算和响应协议；不发请求、不访问凭证。"""
import json
import re
from urllib.parse import urlparse

import config


class ResponseProtocolError(ValueError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


ERROR_LABELS = {
    "budget_exhausted": "思考/输出预算不足，未得到完整正文",
    "empty_content": "服务端没有返回最终正文",
    "invalid_json": "响应不是完整的 JSON 对象",
    "invalid_action": "响应字段不符合行动协议",
    "invalid_response": "服务端响应缺少有效消息",
    "auth": "API Key 无效或未授权",
    "balance": "API 余额不足",
    "permission": "API 无权限访问该模型",
    "rate_limit": "服务商限流",
    "provider_error": "服务商暂时异常或生成中断",
    "content_filter": "服务商未提供可用正文",
    "connect_timeout": "连接模型服务超时",
    "read_timeout": "等待模型响应超时",
    "timeout": "行动总时限已到，用量可能尚未返回",
    "connection_refused": "模型服务拒绝连接",
    "connection_error": "模型服务连接失败",
    "request_error": "模型请求参数或模型不可用",
    "unknown": "模型请求失败，原因待检查",
}


def provider_for(base_url, mode="auto"):
    if mode not in {"auto", "generic", "deepseek"}:
        raise ValueError("未知的模型接口类型")
    if mode != "auto":
        return mode
    return "deepseek" if urlparse(str(base_url)).hostname == "api.deepseek.com" else "generic"


def output_budget(kind, *, repair=False):
    if kind in {"ack", "order"}:
        return config.DIAGNOSTIC_OUTPUT_TOKENS
    if kind == "memory_summary":
        return config.SUMMARY_OUTPUT_TOKENS
    if kind == "diagnostic":
        return config.DIAGNOSTIC_OUTPUT_TOKENS
    quick = kind in {"vote", "withdraw", "join_police", "badge_target", "order", "quick", "ack", "antidote"}
    if quick:
        return config.QUICK_REPAIR_TOKENS if repair else config.QUICK_OUTPUT_TOKENS
    return config.ACTION_REPAIR_TOKENS if repair else config.ACTION_OUTPUT_TOKENS


def request_options(provider, kind, *, budget, temperature, thinking=True, effort="low"):
    options = {"max_tokens": int(budget)}
    if kind != "memory_summary":
        options["response_format"] = {"type": "json_object"}
    enabled = bool(thinking) and kind not in {"memory_summary", "diagnostic", "ack", "order"}
    if provider == "deepseek":
        options["extra_body"] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
        if enabled:
            options["reasoning_effort"] = effort
        else:
            options["temperature"] = temperature
    else:
        options["temperature"] = temperature
    return options


def message_metadata(completion):
    """只返回诊断元数据和最终正文，不导出隐式思考文本。"""
    choices = getattr(completion, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    content = getattr(message, "content", None)
    reasoning = getattr(message, "reasoning_content", None)
    return content, {
        "returned_model": getattr(completion, "model", None),
        "completion_id": getattr(completion, "id", None),
        "finish_reason": getattr(choice, "finish_reason", None),
        "content_length": len(content) if isinstance(content, str) else 0,
        "reasoning_content_length": len(reasoning) if isinstance(reasoning, str) else 0,
        "has_message": message is not None,
    }


def validate_content(raw, metadata, usage, budget, kind=None, context=None):
    reason = metadata.get("finish_reason")
    usage = usage or {}
    completion = int(usage.get("completion_tokens") or 0)
    reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    if reason == "length":
        # 只抢救完整、符合本轮协议和合法目标约束的对象；绝不补全截断 JSON。
        try:
            if kind is None:
                raise ValueError("缺少本轮协议")
            parse_action(raw, kind, context)
        except (ValueError, TypeError, AttributeError):
            raise ResponseProtocolError("budget_exhausted", ERROR_LABELS["budget_exhausted"])
        metadata["rescued_length"] = True
    if reason in {"insufficient_system_resource", "aborted"}:
        raise ResponseProtocolError("provider_error", ERROR_LABELS["provider_error"])
    if reason == "content_filter":
        raise ResponseProtocolError("content_filter", ERROR_LABELS["content_filter"])
    if not metadata.get("has_message"):
        raise ResponseProtocolError("invalid_response", ERROR_LABELS["invalid_response"])
    if not isinstance(raw, str) or not raw.strip():
        exhausted = (reasoning > 0 and reasoning == completion
                     and completion >= max(1, int(budget) - 2))
        category = "budget_exhausted" if exhausted else "empty_content"
        raise ResponseProtocolError(category, ERROR_LABELS[category])
    if reason == "tool_calls":
        raise ResponseProtocolError("invalid_response", "本局请求不接受工具调用代替行动 JSON")


TARGET_KINDS = {"check", "guard", "poison", "hunt", "charm", "dream"}


def action_instruction(kind, context=None):
    """回合末尾重复唯一动作契约，覆盖历史轮次不同格式的干扰。"""
    fields = {"speech": '"speech":"公开发言"', "ack": '"speech":"已确认"',
              "order": '"speech":"警左或警右或死左或死右"',
              "antidote": '"use_antidote":true或false',
              "join_police": '"join_police":true或false',
              "withdraw": '"withdraw":true或false', "badge_target": '"badge_target":座位号',
              "react_explode": '"is_explode":true或false',
              "react_duel": '"is_duel":true或false',
              "consolidation": '"speech":"归票X号或不归票","vote_target":座位号',
              "link": '"targets":[座位号,座位号]', "swap": '"targets":[座位号,座位号]'}.get(
                  kind, '"vote_target":座位号')
    return ('\n【本轮输出契约】只输出一个完整JSON对象：{"reasoning":"简短依据，最多120字",'
            + fields + '}。动作以结构字段为准，不能只写在说明里。'
            + '主动自爆/决斗时还必须附相应目标字段。'
            + (" 合法目标=" + json.dumps(context["allowed_targets"]) if context and "allowed_targets" in context else ""))


def parse_action(raw, kind, context=None):
    value = raw.strip()
    if value.startswith("```"):
        match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", value)
        if match:
            value = match.group(1)
    try:
        data = json.loads(value)
    except (ValueError, TypeError) as exc:
        raise ResponseProtocolError("invalid_json", ERROR_LABELS["invalid_json"]) from exc
    if not isinstance(data, dict) or not data:
        raise ResponseProtocolError("invalid_action", ERROR_LABELS["invalid_action"])
    if kind == "diagnostic":
        if data.get("ok") != 1 or isinstance(data.get("ok"), bool):
            raise ResponseProtocolError("invalid_action", '诊断响应须为 {"ok":1}')
        return data
    for field in ("reasoning", "speech"):
        if field in data and not isinstance(data[field], str):
            raise ResponseProtocolError("invalid_action", f"{field} 必须是文本")
    for field in ("is_explode", "is_duel", "join_police", "withdraw", "use_antidote"):
        if field in data and not isinstance(data[field], bool):
            raise ResponseProtocolError("invalid_action", f"{field} 必须是布尔值")
    for field in ("vote_target", "explode_target", "duel_target", "badge_target"):
        if field in data:
            seat = data[field]
            if isinstance(seat, str) and re.fullmatch(r"\d{1,2}", seat.strip()):
                seat = int(seat)
            if isinstance(seat, bool) or not isinstance(seat, int) or not 0 <= seat <= 12:
                raise ResponseProtocolError("invalid_action", f"{field} 必须是 0 到 12 的座位号")
            data[field] = seat
    required = {"vote": "vote_target", "join_police": "join_police",
                "withdraw": "withdraw", "badge_target": "badge_target", "antidote": "use_antidote",
                "react_explode": "is_explode", "react_duel": "is_duel",
                "link": "targets", "swap": "targets"}.get(kind)
    if kind == "consolidation":
        required = "vote_target"
    if kind in TARGET_KINDS:
        required = "vote_target"
    if required and required not in data:
        raise ResponseProtocolError("invalid_action", f"本轮缺少 {required}")
    if kind in {"speech", "ack", "order", "consolidation"} and not data.get("speech", "").strip():
        raise ResponseProtocolError("invalid_action", "本轮必须提供非空 speech")
    if kind == "order" and data["speech"].strip() not in {"警左", "警右", "死左", "死右"}:
        raise ResponseProtocolError("invalid_action", "定序只能选择警左、警右、死左或死右")
    if kind in {"link", "swap"}:
        targets = data.get("targets")
        if (not isinstance(targets, list) or len(targets) != 2
                or any(type(s) is not int or not 1 <= s <= 12 for s in targets) or targets[0] == targets[1]):
            raise ResponseProtocolError("invalid_action", "targets 必须包含两个不同的合法座位")
    context = context or {}
    allowed = context.get("allowed_targets")
    field = "badge_target" if kind == "badge_target" else "vote_target"
    if allowed is not None:
        targets = data.get("targets", [data.get(field)])
        if any(s not in allowed for s in targets):
            raise ResponseProtocolError("invalid_action", "目标不在本轮合法名单中")
    for flag, target in (("is_explode", "explode_target"), ("is_duel", "duel_target")):
        if data.get(flag):
            if context.get("allowed_flags") is not None and flag not in context["allowed_flags"]:
                raise ResponseProtocolError("invalid_action", "当前身份或时机不允许发动该技能")
            if flag == "is_duel" or context.get("explosion_requires_target"):
                if data.get(target) not in context.get("skill_targets", range(1, 13)):
                    raise ResponseProtocolError("invalid_action", f"{target} 必须是存活的其他玩家")
    action_fields = {"vote_target", "badge_target", "join_police", "withdraw", "is_explode", "is_duel"}
    if not required and not (data.get("speech", "").strip() or action_fields.intersection(data)):
        raise ResponseProtocolError("invalid_action", "响应缺少公开发言或行动字段")
    return data


def error_category(error, status=None):
    if isinstance(error, ResponseProtocolError):
        return error.category
    if status in {401, 402, 403}:
        return {401: "auth", 402: "balance", 403: "permission"}[status]
    if status == 429:
        return "rate_limit"
    if status is not None:
        return "provider_error" if status >= 500 else "request_error"
    name = type(error).__name__.lower()
    cause = error
    names, messages = [], []
    for _ in range(5):
        if cause is None:
            break
        names.append(type(cause).__name__.lower())
        messages.append(str(cause).lower())
        cause = cause.__cause__
    chain = " ".join(names)
    detail = " ".join(messages)
    if "connecttimeout" in chain:
        return "connect_timeout"
    if "readtimeout" in chain:
        return "read_timeout"
    if isinstance(error, TimeoutError) or "timeout" in name:
        return "timeout"
    if "refused" in detail or "10061" in detail or "拒绝" in detail:
        return "connection_refused"
    if isinstance(error, ConnectionError) or "connect" in chain:
        return "connection_error"
    return "unknown"


from runtime_version import register_source as _register_source
_register_source(__name__, __file__)
