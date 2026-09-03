"""In-memory Holdout vault and irreversible exposure ledger."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .canonical import canonical_json_bytes, sha256_bytes
from .capabilities import Capability, CapabilityAuthority, CapabilityScope
from .errors import LedgerIntegrityError


@dataclass(frozen=True, slots=True)
class ExposureEvent:
    sequence: int
    event_id: str
    previous_hash: str | None
    actor: str
    vintage: str
    purpose: str
    accessed_at: datetime
    capability_id: str
    event_hash: str


class ExposureLedger:
    """Append-only NDJSON ledger with a verifiable hash chain."""

    def __init__(self, path: Path) -> None:
        self._path = path.resolve()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _read_rows(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        rows = []
        for number, line in enumerate(self._path.read_bytes().splitlines(), start=1):
            try:
                rows.append(json.loads(line))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise LedgerIntegrityError(
                    "EXPOSURE_LEDGER_CORRUPT",
                    f"invalid exposure event at line {number}",
                    rule_id="RULE-016",
                    context={"line": number},
                ) from error
        return rows

    @staticmethod
    def _event_hash(row_without_hash: dict[str, Any]) -> str:
        return sha256_bytes(canonical_json_bytes(row_without_hash))

    def verify(self) -> tuple[ExposureEvent, ...]:
        rows = self._read_rows()
        previous_hash: str | None = None
        events: list[ExposureEvent] = []
        for sequence, row in enumerate(rows, start=1):
            event_hash = row.get("event_hash")
            unsigned = {key: value for key, value in row.items() if key != "event_hash"}
            expected = self._event_hash(unsigned)
            if row.get("sequence") != sequence or row.get("previous_hash") != previous_hash or event_hash != expected:
                raise LedgerIntegrityError(
                    "EXPOSURE_LEDGER_CHAIN_BROKEN",
                    f"exposure ledger hash chain failed at sequence {sequence}",
                    rule_id="RULE-016",
                    context={"sequence": sequence},
                )
            events.append(
                ExposureEvent(
                    sequence=sequence,
                    event_id=row["event_id"],
                    previous_hash=previous_hash,
                    actor=row["actor"],
                    vintage=row["vintage"],
                    purpose=row["purpose"],
                    accessed_at=datetime.fromisoformat(row["accessed_at"].replace("Z", "+00:00")),
                    capability_id=row["capability_id"],
                    event_hash=event_hash,
                )
            )
            previous_hash = event_hash
        return tuple(events)

    def append(
        self,
        *,
        actor: str,
        vintage: str,
        purpose: str,
        capability_id: str,
        accessed_at: datetime | None = None,
    ) -> ExposureEvent:
        if not purpose.strip():
            raise ValueError("Holdout access purpose must not be blank")
        timestamp = accessed_at or datetime.now(UTC)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("exposure time must include a timezone")
        with self._lock:
            prior = self.verify()
            sequence = len(prior) + 1
            previous_hash = prior[-1].event_hash if prior else None
            unsigned = {
                "accessed_at": timestamp.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z"),
                "actor": actor,
                "capability_id": capability_id,
                "event_id": f"HOLDOUT-EXPOSURE-{sequence:08d}",
                "previous_hash": previous_hash,
                "purpose": purpose,
                "sequence": sequence,
                "vintage": vintage,
            }
            event_hash = self._event_hash(unsigned)
            row = {**unsigned, "event_hash": event_hash}
            encoded = canonical_json_bytes(row) + b"\n"
            descriptor = os.open(self._path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
            try:
                remaining = memoryview(encoded)
                while remaining:
                    written = os.write(descriptor, remaining)
                    if written == 0:
                        raise OSError("zero-byte write while appending exposure event")
                    remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return ExposureEvent(
                sequence=sequence,
                event_id=unsigned["event_id"],
                previous_hash=previous_hash,
                actor=actor,
                vintage=vintage,
                purpose=purpose,
                accessed_at=timestamp.astimezone(UTC),
                capability_id=capability_id,
                event_hash=event_hash,
            )


class HoldoutVault:
    """Authority-process-only Holdout payloads; no backing path is exposed."""

    def __init__(self, authority: CapabilityAuthority, ledger: ExposureLedger) -> None:
        self._authority = authority
        self._ledger = ledger
        self._payloads: dict[str, bytes] = {}

    def __reduce__(self) -> None:
        raise TypeError("HoldoutVault cannot be serialized to another process")

    def seal(self, vintage: str, payload: bytes) -> None:
        if not vintage.strip():
            raise ValueError("Holdout vintage must not be blank")
        existing = self._payloads.get(vintage)
        if existing is not None and existing != payload:
            raise ValueError("a Holdout vintage is immutable once sealed")
        self._payloads[vintage] = payload

    def read(
        self,
        vintage: str,
        *,
        actor: str,
        purpose: str,
        capability: Capability | None,
        accessed_at: datetime | None = None,
    ) -> bytes:
        authorized = self._authority.require(
            capability,
            actor=actor,
            scope=CapabilityScope.READ_HOLDOUT,
            resource=vintage,
        )
        if vintage not in self._payloads:
            raise KeyError(vintage)
        self._ledger.append(
            actor=actor,
            vintage=vintage,
            purpose=purpose,
            capability_id=authorized.capability_id,
            accessed_at=accessed_at,
        )
        return self._payloads[vintage]
