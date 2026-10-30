"""Benchmark-only immutable log-row prototype.

This module is deliberately not imported by production code. It models a
recursively frozen JSON-like event row whose deepcopy operation can safely
return the same object, allowing snapshots to copy log containers without
copying every nested event payload.
"""


class FrozenDict(dict):
    """A dict-compatible read-only container for recursively frozen values."""

    __slots__ = ()

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("FrozenDict is immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


class FrozenList(list):
    """A list-compatible read-only container for recursively frozen values."""

    __slots__ = ()

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("FrozenList is immutable")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = _immutable
    reverse = sort = __iadd__ = __imul__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


def freeze_log_value(value):
    """Recursively freeze JSON-like dict/list/tuple payloads.

    Unsupported mutable/custom values fail closed instead of being shared
    accidentally by the benchmark prototype.
    """
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if isinstance(value, dict):
        return FrozenDict({
            key: freeze_log_value(item)
            for key, item in value.items()
        })
    if isinstance(value, list):
        return FrozenList(freeze_log_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_log_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(
        f"unsupported immutable-log prototype value: {type(value).__name__}"
    )


def freeze_engine_logs(engine):
    """Freeze only event rows; keep appendable outer log containers mutable."""
    engine.public_log = [freeze_log_value(row) for row in engine.public_log]
    engine.reasoning_log = {
        seat: [freeze_log_value(row) for row in rows]
        for seat, rows in engine.reasoning_log.items()
    }
    return engine
