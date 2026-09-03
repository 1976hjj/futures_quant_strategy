"""Deterministic synthetic Provider used to prove the M2 boundary."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.kernel.specs import DataDomain

from .contracts import (
    DomainCapability,
    FetchRequest,
    FieldValue,
    LicenseSpec,
    NormalizedRecord,
    PITGrade,
    ProviderResponse,
    ProviderSpec,
    RawSnapshotRef,
    RevisionBehavior,
)
from .pit import seal_record


class SyntheticProvider:
    def __init__(self, rows: tuple[dict[str, Any], ...], *, retrieved_at: datetime) -> None:
        self._rows = rows
        self._retrieved_at = retrieved_at
        domains = sorted({DataDomain(row["record_type"]) for row in rows}, key=lambda item: item.value)
        self._spec = ProviderSpec(
            provider_id="synthetic-m2",
            provider_version="1.0.0",
            adapter_version="1.0.0",
            api_base_url="synthetic://m2",
            documentation_url="docs/m2_pit_data_factory.md",
            assessed_at=datetime(2026, 9, 1, tzinfo=UTC),
            assessor="alpha-research-os",
            timezone="Asia/Shanghai",
            license=LicenseSpec(
                license_id="internal-synthetic",
                terms_url="internal://synthetic-test-data",
                retrieval_allowed=True,
                local_raw_storage_allowed=True,
                derived_storage_allowed=True,
                redistribution_allowed=True,
                commercial_use_allowed=True,
                credential_required=False,
                attribution_required=False,
                notes="Artificial records with no third-party rights.",
            ),
            capabilities=tuple(
                DomainCapability(
                    data_domain=domain,
                    fields=tuple(
                        sorted({field for row in rows if row["record_type"] == domain.value for field in row["values"]})
                    ),
                    coverage_start=min(
                        date.fromisoformat(row["event_time"][:10]) for row in rows if row["record_type"] == domain.value
                    ),
                    coverage_end=max(
                        date.fromisoformat(row["event_time"][:10]) for row in rows if row["record_type"] == domain.value
                    ),
                    pit_grade=PITGrade.NATIVE_PIT,
                    revision_behavior=RevisionBehavior.APPEND_WITH_HISTORY,
                    time_fields=("event_time", "published_at", "available_at", "ingested_at"),
                    preserves_delisted_history=True,
                    preserves_historical_status=True,
                    limitations=("synthetic-only",),
                )
                for domain in domains
            ),
            response_backfill_policy="append-only fixture",
            rate_limit_notes="none",
        )

    @property
    def spec(self) -> ProviderSpec:
        return self._spec

    def fetch(self, request: FetchRequest) -> ProviderResponse:
        rows = [
            row
            for row in self._rows
            if row["record_type"] == request.data_domain.value
            and request.start <= date.fromisoformat(row["event_time"][:10]) <= request.end
            and (not request.instrument_ids or row.get("instrument_id") in request.instrument_ids)
        ]
        return ProviderResponse(
            request=request,
            provider_request_id=f"SYN-{request.request_id}",
            retrieved_at=self._retrieved_at,
            media_type="application/json",
            payload=canonical_json_bytes(rows),
        )


def normalize_synthetic_response(response: ProviderResponse, snapshot: RawSnapshotRef) -> tuple[NormalizedRecord, ...]:
    rows = json.loads(response.payload)
    normalized = []
    for row in rows:
        record = NormalizedRecord(
            logical_key=row["logical_key"],
            record_type=DataDomain(row["record_type"]),
            instrument_id=row.get("instrument_id"),
            event_time=datetime.fromisoformat(row["event_time"]),
            published_at=datetime.fromisoformat(row["published_at"]),
            available_at=datetime.fromisoformat(row["available_at"]),
            ingested_at=datetime.fromisoformat(row["ingested_at"]),
            source="synthetic-m2",
            source_record_id=row["source_record_id"],
            revision_id=row["revision_id"],
            raw_snapshot_id=snapshot.snapshot_id,
            values=tuple(FieldValue(name=name, value=value) for name, value in sorted(row["values"].items())),
        )
        normalized.append(seal_record(record))
    return tuple(normalized)
