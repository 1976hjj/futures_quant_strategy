"""Canonicalize tabular client responses without importing pandas in core code."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from alpha_research_os.kernel.canonical import canonical_json_bytes


def _scalar(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _scalar(converted)
    if type(value).__module__.startswith("pandas"):
        if str(value) == "NaT":
            return None
        isoformat = getattr(value, "isoformat", None)
        if callable(isoformat):
            return str(isoformat())
    raise TypeError(f"unsupported provider scalar: {type(value).__qualname__}")


def records_from_table(table: Any) -> list[dict[str, str | int | float | bool | None]]:
    if hasattr(table, "to_dict"):
        raw_rows = table.to_dict(orient="records")
    elif isinstance(table, Mapping):
        raw_rows = [table]
    elif isinstance(table, Iterable) and not isinstance(table, (str, bytes, bytearray)):
        raw_rows = list(table)
    else:
        raise TypeError("provider response is not tabular")
    rows = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            raise TypeError("provider row is not a mapping")
        rows.append({str(key): _scalar(value) for key, value in raw_row.items()})
    return rows


def tabular_payload(*, endpoint: str, rows: list[dict[str, object]], metadata: Mapping[str, object]) -> bytes:
    return canonical_json_bytes(
        {
            "endpoint": endpoint,
            "metadata": dict(metadata),
            "rows": rows,
            "schema": "provider-tabular-v1",
        }
    )


def payload_rows(payload: bytes) -> list[dict[str, Any]]:
    import json

    document = json.loads(payload)
    if document.get("schema") != "provider-tabular-v1" or not isinstance(document.get("rows"), list):
        raise ValueError("unsupported provider payload schema")
    return document["rows"]
