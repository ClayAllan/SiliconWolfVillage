"""保存导入时的源码；与归档时磁盘哈希分开，便于发现热更新版本差异。"""
import hashlib
import marshal
from pathlib import Path

_SOURCES = {}


def register_source(name, filename):
    text = Path(filename).read_text(encoding="utf-8-sig")
    record = {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "text": text}
    _SOURCES[name] = record
    short_name = name.split(".")[-1]
    if short_name != name:
        _SOURCES[short_name] = record


def snapshot(engine=None):
    codes = {}
    for prefix, obj, names in (("engine", engine, ("run", "_ask", "_action_context")),
                               ("manager", getattr(engine, "manager", None), ("_execute_response", "_build_messages"))):
        for name in names:
            code = getattr(getattr(obj, name, None), "__code__", None)
            if code:
                codes[prefix + "." + name] = hashlib.sha256(marshal.dumps(code)).hexdigest()
    digest = hashlib.sha256(repr(sorted(codes.items())).encode()).hexdigest()
    return {"version_id": digest, "loaded_code": codes, "loaded_sources": dict(_SOURCES)}
