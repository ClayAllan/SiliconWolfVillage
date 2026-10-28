"""Portable, private JSON progress-save import validation.

Progress saves contain hidden roles and AI private memory. They are not public replay files.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

MAX_SAVE_PACKAGE_BYTES = 16 * 1024 * 1024
_FORBIDDEN_KEYS = {
    "api_key", "apikey", "authorization", "access_token", "refresh_token",
    "secret_key", "client_secret", "password",
}


def validate_save_payload(raw: bytes) -> dict:
    """Validate an untrusted uploaded save before writing it into session storage."""
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("存档必须是 JSON 文件")
    if not raw:
        raise ValueError("存档为空")
    if len(raw) > MAX_SAVE_PACKAGE_BYTES:
        raise ValueError("存档超过 16 MiB 上限")
    try:
        payload = json.loads(bytes(raw).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("无法解析 JSON 存档") from error
    if not isinstance(payload, dict) or payload.get("version") != "v6":
        raise ValueError("不支持的存档版本；仅接受本应用 v6 进度存档")
    if not isinstance(payload.get("engine"), dict) or not payload["engine"]:
        raise ValueError("存档缺少有效的引擎状态")
    if not isinstance(payload.get("manager"), dict):
        raise ValueError("存档缺少有效的 AI 管理器状态")

    def check_keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = str(key).strip().lower().replace("-", "_")
                if normalized in _FORBIDDEN_KEYS:
                    raise ValueError("存档中检测到不应导入的凭证字段")
                check_keys(child)
        elif isinstance(value, list):
            for child in value:
                check_keys(child)

    check_keys(payload)
    return payload


def store_uploaded_save(raw: bytes, directory: str | Path) -> str:
    """Atomically store a validated save under a server-generated filename."""
    payload = validate_save_payload(raw)
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    day = payload.get("meta", {}).get("day", 0) if isinstance(payload.get("meta"), dict) else 0
    try:
        day = max(0, min(9999, int(day)))
    except (TypeError, ValueError):
        day = 0
    name = f"portable_D{day}_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
    target = root / name
    temporary = target.with_suffix(".json.tmp")
    try:
        temporary.write_bytes(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return name
