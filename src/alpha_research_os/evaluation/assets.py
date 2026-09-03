"""Immutable identities for label releases and M4 evidence bundles."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from alpha_research_os.factors.assets import DatasetLineage
from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import Digest, FrozenSpec, Identifier, Version

from .labels import ExecutionConstraintLevel


class LabelAssetRequest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    engine_version: Version
    label_id: Identifier
    label_version: Version
    label_spec_hash: Digest
    source_factor_release_id: Digest
    dataset_lineage: tuple[DatasetLineage, ...] = Field(min_length=1)
    universe_id: Identifier
    universe_version: Version
    start: date
    end: date
    constraint_level: ExecutionConstraintLevel

    @model_validator(mode="after")
    def valid_scope(self) -> LabelAssetRequest:
        if self.end < self.start:
            raise ValueError("label asset end must not precede start")
        tables = [item.manifest_table for item in self.dataset_lineage]
        if tables != sorted(set(tables)):
            raise ValueError("label dataset lineage must be sorted and unique")
        return self

    @property
    def computation_key(self) -> Digest:
        return content_hash(self)


class LabelReleaseManifest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    release_id: Digest
    request: LabelAssetRequest
    created_at: datetime
    parquet_relative_path: str
    parquet_hash: Digest
    row_count: int = Field(ge=0)
    valid_count: int = Field(ge=0)
    invalid_count: int = Field(ge=0)
    quality_status: Literal["PASS"]

    @model_validator(mode="after")
    def identity_and_counts_match(self) -> LabelReleaseManifest:
        if self.release_id != self.request.computation_key:
            raise ValueError("label release_id must equal its computation key")
        if self.valid_count + self.invalid_count != self.row_count:
            raise ValueError("valid and invalid label counts must equal total rows")
        return self


class BasicEvidenceRequest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    evaluator_version: Version
    factor_release_id: Digest
    label_release_id: Digest
    quantile_count: int = Field(default=5, ge=2, le=20)
    minimum_pairs_per_session: int = Field(default=20, ge=3)
    factor_variant: Identifier = "RAW"

    @property
    def evidence_id(self) -> Digest:
        return content_hash(self)


class EvidenceFile(FrozenSpec):
    name: Identifier
    relative_path: str
    artifact_hash: Digest
    row_count: int = Field(ge=0)


class EvidenceBundleManifest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    evidence_id: Digest
    request: BasicEvidenceRequest
    created_at: datetime
    files: tuple[EvidenceFile, ...] = Field(min_length=1)
    factor_count: int = Field(ge=1)
    quality_status: Literal["PASS"]
    limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def identity_matches(self) -> EvidenceBundleManifest:
        if self.evidence_id != self.request.evidence_id:
            raise ValueError("evidence_id must equal its immutable request identity")
        names = [item.name for item in self.files]
        if names != sorted(set(names)):
            raise ValueError("evidence files must be sorted and unique")
        return self
