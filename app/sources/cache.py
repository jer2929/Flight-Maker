"""Tiny in-memory TTL cache, shared by upstream clients to stay polite."""
from __future__ import annotations

import time
from typing import Any

_store: dict[str, tuple[float, Any]] = {}


def get(key: str) -> Any | None:
    item = _store.get(key)
    if item is None:
        return None
    expires, value = item
    if time.time() > expires:
        _store.pop(key, None)
        return None
    return value


def put(key: str, value: Any, ttl: int) -> None:
    _store[key] = (time.time() + ttl, value)


def clear() -> None:
    _store.clear()
