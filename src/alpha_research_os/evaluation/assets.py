"""Immutable identities for label releases and M4 evidence bundles."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from alpha_research_os.factors.assets import DatasetLineage
from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import DateRange, Digest, FrozenSpec, Identifier, Version

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


class StatisticalInferenceSpec(FrozenSpec):
    """Pre-registered M4.3 inference choices; no parameter is fitted from results."""

    schema_version: Literal["1"] = "1"
    spec_id: Identifier = "m4-3-rank-ic-inference"
    spec_version: Version = "1.0.0"
    primary_metric: Literal["RANK_IC"] = "RANK_IC"
    alternative: Literal["TWO_SIDED"] = "TWO_SIDED"
    hac_kernel: Literal["BARTLETT"] = "BARTLETT"
    hac_max_lag: int = Field(default=5, ge=0)
    hac_reference_distribution: Literal["STANDARD_NORMAL"] = "STANDARD_NORMAL"
    bootstrap_method: Literal["CIRCULAR_MOVING_BLOCK"] = "CIRCULAR_MOVING_BLOCK"
    bootstrap_block_length: int = Field(default=5, ge=1)
    bootstrap_resamples: int = Field(default=10_000, ge=999)
    bootstrap_confidence_level: float = Field(default=0.95, gt=0, lt=1)
    random_seed: int = Field(default=20_260_904, ge=0)
    stability_segments: int = Field(default=3, ge=2)
    multiple_testing_method: Literal["BENJAMINI_HOCHBERG"] = "BENJAMINI_HOCHBERG"
    fdr_alpha: float = Field(default=0.05, gt=0, lt=1)

    @property
    def spec_hash(self) -> Digest:
        return content_hash(self)


class EvidenceInputRef(FrozenSpec):
    evidence_id: Digest
    evidence_manifest_hash: Digest
    factor_release_id: Digest
    factor_variant: Identifier


class RobustnessEvidenceRequest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    engine_version: Version
    multiple_testing_family_id: Identifier
    label_release_id: Digest
    evidence_inputs: tuple[EvidenceInputRef, ...] = Field(min_length=2)
    inference: StatisticalInferenceSpec

    @model_validator(mode="after")
    def validate_family(self) -> RobustnessEvidenceRequest:
        keys = [(item.factor_variant, item.evidence_id) for item in self.evidence_inputs]
        if keys != sorted(set(keys)):
            raise ValueError("evidence inputs must be sorted and unique by variant and identity")
        return self

    @property
    def robustness_id(self) -> Digest:
        return content_hash(self)


class RobustnessEvidenceManifest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    robustness_id: Digest
    request: RobustnessEvidenceRequest
    created_at: datetime
    files: tuple[EvidenceFile, ...] = Field(min_length=1)
    hypothesis_count: int = Field(ge=1)
    quality_status: Literal["PASS"]
    decision_status: Literal["DIAGNOSTIC_ONLY_NOT_OOS"]
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identity_matches(self) -> RobustnessEvidenceManifest:
        if self.robustness_id != self.request.robustness_id:
            raise ValueError("robustness_id must equal its immutable request identity")
        names = [item.name for item in self.files]
        if names != sorted(set(names)):
            raise ValueError("robustness files must be sorted and unique")
        return self


class WalkForwardFoldSpec(FrozenSpec):
    fold_id: Identifier
    train: DateRange
    validation: DateRange
    test: DateRange
    label_horizon_sessions: int = Field(default=5, ge=1)
    purge_sessions: int = Field(default=5, ge=0)
    embargo_sessions: int = Field(default=5, ge=0)
    exposure_status: Literal["RETROSPECTIVE_DIAGNOSTIC", "FROZEN_RESEARCH_UNSEEN"]

    @model_validator(mode="after")
    def validate_order_and_protection(self) -> WalkForwardFoldSpec:
        if not (self.train.end < self.validation.start <= self.validation.end < self.test.start):
            raise ValueError("walk-forward train, validation, and test ranges must be ordered and disjoint")
        if self.purge_sessions < self.label_horizon_sessions:
            raise ValueError("purge must cover the complete forward-label horizon")
        return self


class WalkForwardEvaluationSpec(FrozenSpec):
    schema_version: Literal["1"] = "1"
    spec_id: Identifier = "m4-4-expanding-walk-forward"
    spec_version: Version = "1.0.0"
    primary_metric: Literal["RANK_IC"] = "RANK_IC"
    direction_rule: Literal["DECLARED_OR_TRAIN_MEAN_SIGN"] = "DECLARED_OR_TRAIN_MEAN_SIGN"
    folds: tuple[WalkForwardFoldSpec, ...] = Field(min_length=1)
    market_return_aggregation: Literal["EQUAL_WEIGHT_DAILY_MEAN"] = "EQUAL_WEIGHT_DAILY_MEAN"
    trend_lookback_sessions: int = Field(default=60, ge=20)
    volatility_lookback_sessions: int = Field(default=20, ge=10)
    volatility_threshold_rule: Literal["TRAIN_MEDIAN"] = "TRAIN_MEDIAN"
    minimum_regime_sessions: int = Field(default=20, ge=5)
    inference: StatisticalInferenceSpec = Field(default_factory=StatisticalInferenceSpec)
    exposure_ledger_version: Identifier = "m4-4-exposure-ledger-2026-09-04"

    @model_validator(mode="after")
    def validate_folds(self) -> WalkForwardEvaluationSpec:
        fold_ids = [item.fold_id for item in self.folds]
        if fold_ids != sorted(set(fold_ids)):
            raise ValueError("walk-forward folds must have sorted unique identities")
        if any(current.train.start != self.folds[0].train.start for current in self.folds):
            raise ValueError("M4.4 requires a common expanding-window training start")
        if any(
            current.train.end <= previous.train.end or current.test.start <= previous.test.start
            for previous, current in zip(self.folds[:-1], self.folds[1:], strict=True)
        ):
            raise ValueError("walk-forward train ends and test starts must expand in chronological order")
        return self


class FactorVariantReleaseRef(FrozenSpec):
    release_id: Digest
    manifest_hash: Digest
    parquet_hash: Digest
    variant: Identifier


class WalkForwardEvidenceRequest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    engine_version: Version
    multiple_testing_family_id: Identifier
    factor_inputs: tuple[FactorVariantReleaseRef, ...] = Field(min_length=1)
    label_release_id: Digest
    label_manifest_hash: Digest
    window: DateRange
    evaluation: WalkForwardEvaluationSpec

    @model_validator(mode="after")
    def validate_inputs_and_window(self) -> WalkForwardEvidenceRequest:
        keys = [(item.variant, item.release_id) for item in self.factor_inputs]
        if keys != sorted(set(keys)):
            raise ValueError("factor inputs must be sorted and unique by variant and identity")
        if any(
            fold.train.start < self.window.start or fold.test.end > self.window.end
            for fold in self.evaluation.folds
        ):
            raise ValueError("walk-forward folds must remain inside the published factor window")
        return self

    @property
    def walk_forward_id(self) -> Digest:
        return content_hash(self)


class WalkForwardEvidenceManifest(FrozenSpec):
    schema_version: Literal["1"] = "1"
    walk_forward_id: Digest
    request: WalkForwardEvidenceRequest
    created_at: datetime
    files: tuple[EvidenceFile, ...] = Field(min_length=1)
    daily_row_count: int = Field(ge=1)
    fold_hypothesis_count: int = Field(ge=1)
    regime_row_count: int = Field(ge=1)
    quality_status: Literal["PASS"]
    decision_status: Literal["NO_PROMOTION_DIAGNOSTIC_AND_PSEUDO_OOS"]
    limitations: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def identity_matches(self) -> WalkForwardEvidenceManifest:
        if self.walk_forward_id != self.request.walk_forward_id:
            raise ValueError("walk_forward_id must equal its immutable request identity")
        names = [item.name for item in self.files]
        if names != sorted(set(names)):
            raise ValueError("walk-forward files must be sorted and unique")
        return self
