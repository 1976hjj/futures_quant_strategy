"""Point-in-time record hashing and revision-aware as-of selection."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.errors import IntegrityViolation

from .contracts import NormalizedRecord


def record_content_hash(record: NormalizedRecord) -> str:
    return content_hash(record.model_dump(mode="python", exclude={"record_hash"}))


def fact_content_hash(record: NormalizedRecord) -> str:
    """Hash economic content independent of repeated acquisition lineage."""

    return content_hash(
        record.model_dump(
            mode="python",
            exclude={"record_hash", "raw_snapshot_id", "ingested_at", "source_record_id"},
        )
    )


def seal_record(record: NormalizedRecord) -> NormalizedRecord:
    validated = NormalizedRecord.model_validate(record)
    digest = record_content_hash(validated)
    if validated.record_hash is not None and validated.record_hash != digest:
        raise ValueError("declared record_hash does not match normalized content")
    return validated.model_copy(update={"record_hash": digest})


def select_as_of(
    records: Iterable[NormalizedRecord],
    *,
    signal_cutoff: datetime,
    require_ingested: bool = True,
) -> tuple[NormalizedRecord, ...]:
    if signal_cutoff.tzinfo is None or signal_cutoff.utcoffset() is None:
        raise ValueError("signal_cutoff must include a timezone")
    selected: dict[str, NormalizedRecord] = {}
    for candidate in records:
        record = NormalizedRecord.model_validate(candidate)
        if record.record_hash != record_content_hash(record):
            raise IntegrityViolation(
                "RECORD_HASH_MISMATCH",
                "as-of query received an unsealed or corrupted record",
                rule_id="RULE-027",
                context={"source_record_id": record.source_record_id},
            )
        if record.available_at < record.published_at:
            raise IntegrityViolation(
                "AVAILABILITY_PRECEDES_PUBLICATION",
                "as-of query received impossible publication chronology",
                rule_id="RULE-004",
                context={"source_record_id": record.source_record_id},
            )
        if record.available_at > signal_cutoff:
            continue
        if require_ingested and record.ingested_at > signal_cutoff:
            continue
        current = selected.get(record.logical_key)
        ordering = (record.available_at, record.published_at, record.ingested_at, record.revision_id)
        if current is None:
            selected[record.logical_key] = record
            continue
        current_ordering = (
            current.available_at,
            current.published_at,
            current.ingested_at,
            current.revision_id,
        )
        if ordering > current_ordering:
            selected[record.logical_key] = record
    return tuple(selected[key] for key in sorted(selected))


def select_effective_as_of(
    records: Iterable[NormalizedRecord],
    *,
    event_at: datetime,
    signal_cutoff: datetime,
    valid_from_field: str = "valid_from",
    valid_to_field: str = "valid_to",
    require_ingested: bool = True,
) -> tuple[NormalizedRecord, ...]:
    """Replay bitemporal interval records by valid time and knowledge time."""

    known = select_as_of(records, signal_cutoff=signal_cutoff, require_ingested=require_ingested)
    effective = []
    for record in known:
        values = record.value_map()
        valid_from_raw = values.get(valid_from_field)
        valid_to_raw = values.get(valid_to_field)
        if valid_from_raw is None:
            continue
        valid_from = datetime.fromisoformat(str(valid_from_raw)).date()
        valid_to = datetime.fromisoformat(str(valid_to_raw)).date() if valid_to_raw else None
        if valid_from <= event_at.date() and (valid_to is None or event_at.date() <= valid_to):
            effective.append(record)
    return tuple(effective)
