"""Stable identifiers for research runs."""

from __future__ import annotations

import base64
import secrets
from collections.abc import Callable
from datetime import datetime


def new_experiment_id(
    now: datetime,
    *,
    entropy_source: Callable[[int], bytes] = secrets.token_bytes,
) -> str:
    """Create an ``EXP-YYYYMMDD-XXXX`` identifier with injectable entropy."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("experiment ID time must include a timezone")
    token = base64.b32encode(entropy_source(3)).decode("ascii").rstrip("=")[:4]
    if len(token) != 4:
        raise ValueError("entropy source returned too few bytes")
    return f"EXP-{now:%Y%m%d}-{token}"
