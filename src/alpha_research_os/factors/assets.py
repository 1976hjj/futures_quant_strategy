"""Immutable contracts for experiment-scoped factor computation and release assets."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import Digest, FrozenSpec, Identifier, Version


class DatasetLineage(FrozenSpec):
    manifest_table: Identifier
    checkpoint_hashes: tuple[Digest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def hashes_are_canonical(self) -> DatasetLineage:
        if tuple(sorted(set(self.checkpoint_hashes))) != self.checkpoint_hashes:
            raise ValueError("checkpoint_hashes must be sorted and unique")
        return self


class FactorAssetRef(FrozenSpec):
    factor_id: Identifier
    factor_version: Version
    spec_hash: Digest
    implementation_hash: Digest
    catalog_entry_hash: Digest


class FactorAssetRequest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    engine_version: Version
    factors: tuple[FactorAssetRef, ...] = Field(min_length=1)
    dataset_lineage: tuple[DatasetLineage, ...] = Field(min_length=1)
    universe_id: Identifier
    universe_version: Version
    start: date
    end: date
    variant: Literal["RAW"] = "RAW"
    preprocessing_version: Literal["NONE"] = "NONE"
    signal_clock_version: Version

    @model_validator(mode="after")
    def validate_identity(self) -> FactorAssetRequest:
        if self.end < self.start:
            raise ValueError("factor asset end must not precede start")
        factor_keys = [(item.factor_id, item.factor_version) for item in self.factors]
        if factor_keys != sorted(set(factor_keys)):
            raise ValueError("factor references must be sorted and unique")
        tables = [item.manifest_table for item in self.dataset_lineage]
        if tables != sorted(set(tables)):
            raise ValueError("dataset lineage tables must be sorted and unique")
        return self

    @property
    def computation_key(self) -> Digest:
        return content_hash(self)


class FactorReleaseManifest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    release_id: Digest
    request: FactorAssetRequest
    created_at: datetime
    parquet_relative_path: str
    parquet_hash: Digest
    row_count: int = Field(ge=0)
    session_count: int = Field(ge=0)
    instrument_count: int = Field(ge=0)
    factor_count: int = Field(ge=1)
    quality_status: Literal["PASS"]
    quality_summary_hash: Digest

    @model_validator(mode="after")
    def release_matches_request(self) -> FactorReleaseManifest:
        if self.release_id != self.request.computation_key:
            raise ValueError("release_id must equal the immutable request computation key")
        if self.factor_count != len(self.request.factors):
            raise ValueError("factor_count must equal the request factor count")
        return self
