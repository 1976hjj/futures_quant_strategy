"""Shared conventions for A-share provider adapters."""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")


def ashare_code(instrument_id: str) -> str:
    matches = re.findall(r"(?<!\d)(\d{6})(?!\d)", instrument_id)
    if len(matches) != 1:
        raise ValueError(f"instrument_id must contain exactly one six-digit A-share code: {instrument_id}")
    return matches[0]


def baostock_code(instrument_id: str) -> str:
    code = ashare_code(instrument_id)
    upper = instrument_id.upper()
    if ".SH" in upper or upper.startswith("SH.") or "SSE" in upper:
        exchange = "sh"
    elif ".SZ" in upper or upper.startswith("SZ.") or "SZSE" in upper:
        exchange = "sz"
    else:
        exchange = "sh" if code.startswith(("5", "6", "9")) else "sz"
    return f"{exchange}.{code}"


def instrument_key(provider_code: str) -> str:
    return f"CN-EQ-{ashare_code(provider_code)}"


def session_close(value: str | date) -> datetime:
    day = value if isinstance(value, date) else date.fromisoformat(str(value)[:10])
    return datetime.combine(day, time(15, 0), tzinfo=SHANGHAI)


def row_revision(row: dict[str, Any]) -> str:
    from alpha_research_os.kernel.canonical import canonical_json_bytes

    return "sha256-" + hashlib.sha256(canonical_json_bytes(row)).hexdigest()


def as_float(value: Any) -> float:
    if value in (None, ""):
        raise ValueError("required numeric provider value is missing")
    return float(value)


def as_int(value: Any) -> int:
    if value in (None, ""):
        raise ValueError("required integer provider value is missing")
    return int(float(value))
