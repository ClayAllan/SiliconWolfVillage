"""Application-level immutable containers for append-only game log rows.

Rows are frozen at the engine's write boundary. Their outer public_log and
reasoning_log containers remain ordinary mutable lists/dicts so the engine can
append new events and replace logs when restoring a save.
"""
from __future__ import annotations



class FrozenDict(dict):
    """A dict-compatible container whose normal mutation APIs are disabled."""

    __slots__ = ()

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("game log rows are immutable")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _immutable
    __ior__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


class FrozenList(list):
    """A list-compatible container whose normal mutation APIs are disabled."""

    __slots__ = ()

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("game log rows are immutable")

    __setitem__ = __delitem__ = append = clear = extend = insert = pop = remove = _immutable
    reverse = sort = __iadd__ = __imul__ = _immutable

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        memo[id(self)] = self
        return self


def _freeze_json_value(value, active):
    if isinstance(value, (FrozenDict, FrozenList)):
        return value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    marker = id(value)
    if marker in active:
        raise TypeError("cyclic values cannot be frozen as game log rows")

    if isinstance(value, dict):
        active.add(marker)
        try:
            frozen = {}
            for key, item in value.items():
                if key is not None and not isinstance(key, (str, int, float, bool)):
                    raise TypeError("game log dictionary keys must be JSON-compatible")
                frozen[key] = _freeze_json_value(item, active)
            return FrozenDict(frozen)
        finally:
            active.remove(marker)

    if isinstance(value, list):
        active.add(marker)
        try:
            return FrozenList(_freeze_json_value(item, active) for item in value)
        finally:
            active.remove(marker)

    if isinstance(value, tuple):
        active.add(marker)
        try:
            return tuple(_freeze_json_value(item, active) for item in value)
        finally:
            active.remove(marker)

    raise TypeError(f"unsupported game log value: {type(value).__name__}")


def freeze_log_row(row):
    """Freeze a JSON-like row; leave unsupported rows mutable for safe deepcopy.

    Failing closed preserves the pre-existing snapshot isolation contract:
    GameService will continue to deepcopy any row that could not be frozen.
    """
    try:
        return _freeze_json_value(row, set())
    except (TypeError, RecursionError):
        return row
