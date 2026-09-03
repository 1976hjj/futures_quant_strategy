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


class PreprocessingSpec(FrozenSpec):
    """Frozen cross-sectional transformation contract for a processed factor asset."""

    schema_version: Literal["1"] = "1"
    preprocessing_id: Identifier
    preprocessing_version: Version
    mad_multiplier: float = Field(default=5.0, gt=0)
    mad_consistency_scale: float = Field(default=1.4826, gt=0)
    zero_mad_policy: Literal["PRESERVE_FINITE"] = "PRESERVE_FINITE"
    minimum_cross_section: int = Field(default=20, ge=3)
    standardization_ddof: Literal[0] = 0
    neutralize_log_size: bool
    neutralize_industry: bool = False
    size_field: Identifier = "total_mv"
    industry_classification_release_id: Digest | None = None
    missing_exposure_policy: Literal["TO_MISSING"] = "TO_MISSING"

    @model_validator(mode="after")
    def validate_neutralization_inputs(self) -> PreprocessingSpec:
        if self.neutralize_industry and self.industry_classification_release_id is None:
            raise ValueError("industry neutralization requires a classification release")
        if not self.neutralize_industry and self.industry_classification_release_id is not None:
            raise ValueError("industry classification release is invalid when industry neutralization is disabled")
        return self

    @property
    def spec_hash(self) -> Digest:
        return content_hash(self)


class ProcessedFactorAssetRequest(FrozenSpec):
    """Identity of one processed variant derived from one immutable RAW release."""

    schema_version: Literal["1"] = "1"
    engine_version: Version
    parent_release_id: Digest
    parent_parquet_hash: Digest
    dataset_lineage: tuple[DatasetLineage, ...] = Field(min_length=1)
    universe_id: Identifier
    universe_version: Version
    start: date
    end: date
    variant: Literal["WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED"]
    preprocessing: PreprocessingSpec

    @model_validator(mode="after")
    def validate_identity(self) -> ProcessedFactorAssetRequest:
        if self.end < self.start:
            raise ValueError("processed factor asset end must not precede start")
        tables = [item.manifest_table for item in self.dataset_lineage]
        if tables != sorted(set(tables)):
            raise ValueError("dataset lineage tables must be sorted and unique")
        expected_size = self.variant == "SIZE_NEUTRALIZED"
        if self.preprocessing.neutralize_log_size is not expected_size:
            raise ValueError("variant and log-size neutralization setting disagree")
        if self.preprocessing.neutralize_industry:
            raise ValueError("M4.2 processed variants do not permit industry neutralization")
        return self

    @property
    def computation_key(self) -> Digest:
        return content_hash(self)


class ProcessedFactorReleaseManifest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    release_id: Digest
    request: ProcessedFactorAssetRequest
    created_at: datetime
    parquet_relative_path: str
    parquet_hash: Digest
    row_count: int = Field(ge=0)
    present_count: int = Field(ge=0)
    session_count: int = Field(ge=0)
    instrument_count: int = Field(ge=0)
    factor_count: int = Field(ge=1)
    quality_status: Literal["PASS"]
    quality_summary_hash: Digest

    @model_validator(mode="after")
    def release_matches_request(self) -> ProcessedFactorReleaseManifest:
        if self.release_id != self.request.computation_key:
            raise ValueError("processed release_id must equal its computation key")
        if self.present_count > self.row_count:
            raise ValueError("present_count cannot exceed row_count")
        return self
