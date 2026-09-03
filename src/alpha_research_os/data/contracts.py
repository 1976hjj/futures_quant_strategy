"""Provider, raw snapshot, and normalized PIT record contracts."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import Field, StringConstraints, field_validator, model_validator

from alpha_research_os.kernel.specs import DataDomain, Digest, FrozenSpec, Identifier, Version


class PITGrade(StrEnum):
    NATIVE_PIT = "NATIVE_PIT"
    RECONSTRUCTED_PIT = "RECONSTRUCTED_PIT"
    CURRENT_ONLY = "CURRENT_ONLY"
    UNVERIFIED = "UNVERIFIED"


class RevisionBehavior(StrEnum):
    APPEND_WITH_HISTORY = "APPEND_WITH_HISTORY"
    OVERWRITES_HISTORY = "OVERWRITES_HISTORY"
    NO_REVISIONS = "NO_REVISIONS"
    UNVERIFIED = "UNVERIFIED"


class LicenseSpec(FrozenSpec):
    license_id: Identifier
    terms_url: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    retrieval_allowed: bool
    local_raw_storage_allowed: bool
    derived_storage_allowed: bool
    redistribution_allowed: bool
    commercial_use_allowed: bool | None
    credential_required: bool
    attribution_required: bool
    notes: str


class DomainCapability(FrozenSpec):
    data_domain: DataDomain
    fields: tuple[Identifier, ...] = Field(min_length=1)
    coverage_start: date | None
    coverage_end: date | None
    pit_grade: PITGrade
    revision_behavior: RevisionBehavior
    time_fields: tuple[Identifier, ...]
    preserves_delisted_history: bool | None = None
    preserves_historical_status: bool | None = None
    units: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_coverage(self) -> DomainCapability:
        if self.coverage_start and self.coverage_end and self.coverage_end < self.coverage_start:
            raise ValueError("capability coverage_end must not precede coverage_start")
        if self.pit_grade is PITGrade.NATIVE_PIT and "available_at" not in self.time_fields:
            raise ValueError("NATIVE_PIT capability must expose available_at")
        return self


class ProviderSpec(FrozenSpec):
    provider_id: Identifier
    provider_version: Version
    adapter_version: Version
    api_base_url: str
    documentation_url: str
    assessed_at: datetime
    assessor: Identifier
    timezone: Identifier
    license: LicenseSpec
    capabilities: tuple[DomainCapability, ...] = Field(min_length=1)
    response_backfill_policy: Identifier
    rate_limit_notes: str

    @field_validator("assessed_at")
    @classmethod
    def assessed_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("assessed_at must include a timezone")
        return value

    @model_validator(mode="after")
    def unique_domains(self) -> ProviderSpec:
        domains = [item.data_domain for item in self.capabilities]
        if len(domains) != len(set(domains)):
            raise ValueError("provider capabilities must contain each data domain at most once")
        return self


class FetchRequest(FrozenSpec):
    request_id: Identifier
    data_domain: DataDomain
    start: date
    end: date
    fields: tuple[Identifier, ...] = Field(min_length=1)
    instrument_ids: tuple[Identifier, ...] = ()
    parameters: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_range(self) -> FetchRequest:
        if self.end < self.start:
            raise ValueError("fetch end must not precede start")
        return self


class ProviderResponse(FrozenSpec):
    request: FetchRequest
    provider_request_id: Identifier
    retrieved_at: datetime
    media_type: Identifier
    payload: bytes

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("retrieved_at must include a timezone")
        return value


class RawSnapshotRef(FrozenSpec):
    snapshot_id: Digest
    payload_artifact_id: Digest
    payload_manifest_id: Digest
    payload_encoding: Identifier
    uncompressed_payload_hash: Digest
    uncompressed_byte_size: int = Field(ge=0)
    snapshot_manifest_artifact_id: Digest
    provider_spec_artifact_id: Digest
    license_spec_artifact_id: Digest
    provider_id: Identifier
    request_id: Identifier
    retrieved_at: datetime

    @field_validator("retrieved_at")
    @classmethod
    def retrieved_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("snapshot retrieved_at must include a timezone")
        return value


ScalarValue = str | int | float | bool | None


class FieldValue(FrozenSpec):
    name: Identifier
    value: ScalarValue


class NormalizedRecord(FrozenSpec):
    logical_key: Identifier
    record_type: DataDomain
    instrument_id: Identifier | None
    event_time: datetime
    published_at: datetime
    available_at: datetime
    ingested_at: datetime
    source: Identifier
    source_record_id: Identifier
    revision_id: Identifier
    raw_snapshot_id: Digest
    values: tuple[FieldValue, ...] = Field(min_length=1)
    record_hash: Digest | None = None

    @field_validator("event_time", "published_at", "available_at", "ingested_at")
    @classmethod
    def times_are_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("normalized record times must include a timezone")
        return value

    @model_validator(mode="after")
    def values_are_unique(self) -> NormalizedRecord:
        names = [item.name for item in self.values]
        if len(names) != len(set(names)):
            raise ValueError("normalized record field names must be unique")
        return self

    def value_map(self) -> dict[str, ScalarValue]:
        return {item.name: item.value for item in self.values}


class DatasetRelease(FrozenSpec):
    dataset_manifest_hash: Digest
    dataset_spec_artifact_id: Digest
    partition_artifact_ids: tuple[Digest, ...] = Field(min_length=1)
    record_count: int = Field(ge=0)
