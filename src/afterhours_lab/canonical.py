"""Canonical JSON and SHA-256 helpers for immutable research evidence."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
from enum import Enum
from typing import Any


def _normalize(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dt.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical timestamps must include a timezone")
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON rejects NaN and infinity")
        return value
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("canonical JSON object keys must be strings")
        return {key: _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        raise ValueError("unordered collections are not canonical")
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """UTF-8 JSON contract: sorted keys, compact separators, explicit nulls."""
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_digest(schema: str, value: Any) -> str:
    if not schema.strip():
        raise ValueError("digest schema must be non-empty")
    payload = {"schema": schema, "value": _normalize(value)}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
