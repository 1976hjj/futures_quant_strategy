"""Deterministic JSON manifests and content hashes."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .errors import CanonicalizationError

HASH_ALGORITHM = "sha256"


def _normalize(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="python", by_alias=True, exclude_none=False))
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError(
                "NON_FINITE_NUMBER",
                "NaN and Infinity are forbidden in canonical manifests",
            )
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError(
                "NON_FINITE_NUMBER",
                "non-finite Decimal values are forbidden in canonical manifests",
            )
        return format(value, "f")
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalizationError(
                "NAIVE_DATETIME",
                "datetimes in manifests must include a timezone",
            )
        return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise CanonicalizationError(
                "NON_STRING_KEY",
                "canonical JSON object keys must be strings",
            )
        return {key: _normalize(value[key]) for key in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalized = [_normalize(item) for item in value]
        return sorted(normalized, key=lambda item: json.dumps(item, ensure_ascii=False, sort_keys=True))
    raise CanonicalizationError(
        "UNSUPPORTED_MANIFEST_TYPE",
        f"unsupported canonical manifest value: {type(value).__qualname__}",
    )


def canonical_json_bytes(value: Any) -> bytes:
    """Return a stable UTF-8 JSON representation of a supported value."""

    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(payload: bytes) -> str:
    return f"{HASH_ALGORITHM}:{hashlib.sha256(payload).hexdigest()}"


def content_hash(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


@dataclass(frozen=True, slots=True)
class FrozenManifest:
    """A manifest whose payload is frozen as canonical bytes."""

    kind: str
    schema_version: str
    payload_bytes: bytes
    payload_hash: str

    @classmethod
    def build(cls, kind: str, payload: Any, *, schema_version: str = "1") -> FrozenManifest:
        if not kind.strip():
            raise ValueError("manifest kind must not be blank")
        payload_bytes = canonical_json_bytes(payload)
        return cls(
            kind=kind,
            schema_version=schema_version,
            payload_bytes=payload_bytes,
            payload_hash=sha256_bytes(payload_bytes),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "payload": json.loads(self.payload_bytes),
            "payload_hash": self.payload_hash,
            "schema_version": self.schema_version,
        }

    def to_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())

    @property
    def manifest_hash(self) -> str:
        return sha256_bytes(self.to_bytes())

    def verify(self) -> bool:
        return self.payload_hash == sha256_bytes(self.payload_bytes)
