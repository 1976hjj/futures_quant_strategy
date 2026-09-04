"""Machine-validated configuration for reusable M4 evidence pipelines."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import Digest, FrozenSpec, Identifier, Version


class M4PipelinePaths(FrozenSpec):
    database: str = "data/warehouse/alpha_research.duckdb"
    factor_store: str = "data/factor_store"
    evidence_store: str = "data/evidence_store"
    report: str


class M4DirectionOverride(FrozenSpec):
    multiplier: Literal[-1, 1]
    direction_source: Identifier
    focus_hypothesis: Identifier = "NONE"


class M4WalkForwardFold(FrozenSpec):
    fold_id: Identifier
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date
    exposure_status: Literal["RETROSPECTIVE_DIAGNOSTIC", "FROZEN_RESEARCH_UNSEEN"]
    label_horizon_sessions: int = Field(default=5, ge=1)
    purge_sessions: int = Field(default=5, ge=0)
    embargo_sessions: int = Field(default=5, ge=0)

    @model_validator(mode="after")
    def valid_ranges(self) -> M4WalkForwardFold:
        if not (
            self.train_start
            <= self.train_end
            < self.validation_start
            <= self.validation_end
            < self.test_start
            <= self.test_end
        ):
            raise ValueError("walk-forward fold ranges must be ordered and disjoint")
        if self.purge_sessions < self.label_horizon_sessions:
            raise ValueError("purge must cover the label horizon")
        return self


class M4WalkForwardConfig(FrozenSpec):
    family_id: Identifier
    engine_version: Version = "duckdb-python-walk-forward-1.1.0"
    source_walk_forward_id: Digest | None = None
    window_start: date
    window_end: date
    folds: tuple[M4WalkForwardFold, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_window(self) -> M4WalkForwardConfig:
        if self.window_end < self.window_start:
            raise ValueError("walk-forward window end must not precede start")
        if any(fold.train_start < self.window_start or fold.test_end > self.window_end for fold in self.folds):
            raise ValueError("all folds must remain inside the walk-forward window")
        return self


class M4RobustnessConfig(FrozenSpec):
    family_id: Identifier
    evidence_ids: tuple[Digest, ...] = ()
    source_robustness_id: Digest | None = None


class M4RedundancyConfig(FrozenSpec):
    family_id: Identifier
    source_walk_forward_id: Digest | None = None
    source_redundancy_id: Digest | None = None
    candidate_policy: Literal["ALL_CANONICAL", "CLUSTER_REPRESENTATIVES_AND_FOCUS"] = (
        "CLUSTER_REPRESENTATIVES_AND_FOCUS"
    )
    bind_configuration_to_asset_identity: bool = True
    direction_overrides: dict[str, M4DirectionOverride] = Field(default_factory=dict)


class M4FactorExplorerConfig(FrozenSpec):
    report_name: Identifier
    title: str = "Alpha Research OS · Factor Evidence Explorer"
    output_root: str = "reports/factor_explorer"
    robustness_id: Digest | None = None
    basic_evidence_ids: tuple[Digest, ...] = ()
    maximum_compare_entities: int = Field(default=6, ge=2, le=12)


class M4PipelineConfig(FrozenSpec):
    schema_version: Literal["1"] = "1"
    batch_id: Identifier
    paths: M4PipelinePaths
    stages: tuple[
        Literal[
            "processed",
            "basic_evidence",
            "audit_basic_evidence",
            "robustness",
            "audit_robustness",
            "walk_forward",
            "redundancy",
            "audit_walk_forward",
            "audit_redundancy",
            "factor_explorer",
            "audit_factor_explorer",
        ],
        ...,
    ] = Field(min_length=1)
    raw_factor_release_id: Digest
    processed_factor_release_ids: tuple[Digest, ...] = ()
    processed_variants: tuple[Literal["WINSORIZED_ZSCORE", "SIZE_NEUTRALIZED"], ...] = ()
    robustness: M4RobustnessConfig | None = None
    walk_forward: M4WalkForwardConfig | None = None
    redundancy: M4RedundancyConfig | None = None
    factor_explorer: M4FactorExplorerConfig | None = None

    @model_validator(mode="after")
    def valid_stage_dependencies(self) -> M4PipelineConfig:
        if len(set(self.stages)) != len(self.stages):
            raise ValueError("pipeline stages must be unique")
        if "processed" in self.stages and not self.processed_variants:
            raise ValueError("processed stage requires processed_variants")
        if any(stage in self.stages for stage in ("robustness", "audit_robustness")) and self.robustness is None:
            raise ValueError("robustness stages require robustness configuration")
        if "audit_basic_evidence" in self.stages and "basic_evidence" not in self.stages:
            if self.robustness is None or not self.robustness.evidence_ids:
                raise ValueError("basic evidence audit requires a producing stage or robustness.evidence_ids")
        if "audit_robustness" in self.stages and "robustness" not in self.stages:
            if self.robustness is None or self.robustness.source_robustness_id is None:
                raise ValueError("robustness audit requires a producing stage or source_robustness_id")
        if (
            any(stage in self.stages for stage in ("walk_forward", "audit_walk_forward", "factor_explorer"))
            and self.walk_forward is None
        ):
            raise ValueError("walk-forward stages require walk_forward configuration")
        if "audit_walk_forward" in self.stages and "walk_forward" not in self.stages:
            if self.walk_forward is None or self.walk_forward.source_walk_forward_id is None:
                raise ValueError("walk-forward audit requires a producing stage or source_walk_forward_id")
        if (
            any(stage in self.stages for stage in ("redundancy", "audit_redundancy", "factor_explorer"))
            and self.redundancy is None
        ):
            raise ValueError("redundancy stages require redundancy configuration")
        if "audit_redundancy" in self.stages and "redundancy" not in self.stages:
            if self.redundancy is None or self.redundancy.source_redundancy_id is None:
                raise ValueError("redundancy audit requires a producing stage or source_redundancy_id")
        if "redundancy" in self.stages and "walk_forward" not in self.stages:
            if self.redundancy is None or self.redundancy.source_walk_forward_id is None:
                raise ValueError("redundancy requires walk_forward stage or source_walk_forward_id")
        if any(stage in self.stages for stage in ("factor_explorer", "audit_factor_explorer")):
            if self.factor_explorer is None:
                raise ValueError("factor Explorer stages require factor_explorer configuration")
            if "walk_forward" not in self.stages and self.walk_forward is not None:
                if self.walk_forward.source_walk_forward_id is None:
                    raise ValueError("factor_explorer requires a producing walk_forward stage or source ID")
            if "redundancy" not in self.stages and self.redundancy is not None:
                if self.redundancy.source_redundancy_id is None:
                    raise ValueError("factor_explorer requires a producing redundancy stage or source ID")
        if "audit_factor_explorer" in self.stages and "factor_explorer" not in self.stages:
            raise ValueError("factor Explorer audit requires its producing stage")
        return self

    @property
    def config_id(self) -> Digest:
        return content_hash(self)
