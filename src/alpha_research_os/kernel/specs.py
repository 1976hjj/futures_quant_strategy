"""Frozen, machine-validated contracts used by the research kernel."""

from __future__ import annotations

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .errors import IntegrityViolation

Identifier = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Version = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]
Digest = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]


class FrozenSpec(BaseModel):
    """Strict and immutable at the model-field boundary."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
        revalidate_instances="always",
    )


def _require_aware(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must include a timezone")
    return value


class AuditStatus(StrEnum):
    PASSED = "PASSED"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"


class DataDomain(StrEnum):
    MARKET = "market"
    FUNDAMENTAL = "fundamental"
    UNIVERSE = "universe"
    SECURITY_STATUS = "security_status"
    SECURITY_MASTER = "security_master"
    TRADING_CALENDAR = "trading_calendar"
    CORPORATE_ACTION = "corporate_action"
    LABEL = "label"
    HOLDOUT = "holdout"


class SignalCutoff(StrEnum):
    PRE_OPEN = "PRE_OPEN"
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    POST_CLOSE = "POST_CLOSE"


class MarketEvent(StrEnum):
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    NEXT_ELIGIBLE_OPEN = "NEXT_ELIGIBLE_OPEN"
    NEXT_ELIGIBLE_CLOSE = "NEXT_ELIGIBLE_CLOSE"


class FactorDirection(StrEnum):
    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    TRAIN_FIT = "TRAIN_FIT"


class ImplementationType(StrEnum):
    EXPRESSION = "expression"
    PYTHON = "python"


class TemporalDependency(FrozenSpec):
    field: Identifier
    data_domain: DataDomain
    relative_session: int = Field(description="0=current, negative=past, positive=future")


class FeatureExpression(FrozenSpec):
    expression_type: Literal["feature"] = "feature"
    formula: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    dependencies: tuple[TemporalDependency, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_future_and_privileged_domains(self) -> FeatureExpression:
        future = [item for item in self.dependencies if item.relative_session > 0]
        if future:
            raise IntegrityViolation(
                "FEATURE_FUTURE_ACCESS",
                "FeatureExpression cannot declare future dependencies",
                rule_id="RULE-001",
                context={"fields": [item.field for item in future]},
            )
        privileged = [item for item in self.dependencies if item.data_domain in {DataDomain.LABEL, DataDomain.HOLDOUT}]
        if privileged:
            raise IntegrityViolation(
                "FEATURE_DOMAIN_ACCESS",
                "FeatureExpression cannot access Label or Holdout domains",
                rule_id="RULE-005",
                context={"fields": [item.field for item in privileged]},
            )
        return self


class LabelExpression(FrozenSpec):
    expression_type: Literal["label"] = "label"
    formula: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    dependencies: tuple[TemporalDependency, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def require_forward_dependency(self) -> LabelExpression:
        if not any(item.relative_session > 0 for item in self.dependencies):
            raise ValueError("LabelExpression must declare at least one future dependency")
        if any(item.data_domain is DataDomain.HOLDOUT for item in self.dependencies):
            raise ValueError("LabelExpression cannot declare Holdout as an input domain")
        return self


class ExecutionExpression(FrozenSpec):
    expression_type: Literal["execution"] = "execution"
    event: MarketEvent
    session_offset: int = Field(ge=0)
    price_field: Identifier


class PartitionSpec(FrozenSpec):
    name: Identifier
    row_count: int = Field(ge=0)
    content_hash: Digest


class DatasetSpec(FrozenSpec):
    dataset_id: Identifier
    dataset_version: Version
    schema_version: Version
    provider: Identifier
    coverage_start: date
    coverage_end: date
    raw_snapshot_hashes: tuple[Digest, ...] = Field(min_length=1)
    transformation_commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,64}$")]
    pit_rule_version: Version
    adjustment_rule_version: Version
    partitions: tuple[PartitionSpec, ...] = Field(min_length=1)
    audit_status: AuditStatus
    published_at: datetime
    publisher: Identifier
    supersedes: Version | None = None

    _published_at_aware = field_validator("published_at")(lambda value: _require_aware(value, "published_at"))

    @model_validator(mode="after")
    def validate_coverage_and_partitions(self) -> DatasetSpec:
        if self.coverage_end < self.coverage_start:
            raise ValueError("coverage_end must not precede coverage_start")
        names = [item.name for item in self.partitions]
        if len(names) != len(set(names)):
            raise ValueError("partition names must be unique")
        return self


class UniverseSpec(FrozenSpec):
    universe_id: Identifier
    universe_version: Version
    dataset_version: Version
    membership_table: Identifier
    effective_start: date
    effective_end: date
    as_of_field: Literal["available_at"] = "available_at"
    status_as_of_field: Literal["available_at"] = "available_at"
    inclusion_rules: tuple[str, ...] = Field(min_length=1)
    exclusion_rules: tuple[str, ...] = ()
    preserve_delisted_history: Literal[True] = True

    @model_validator(mode="after")
    def validate_effective_range(self) -> UniverseSpec:
        if self.effective_end < self.effective_start:
            raise ValueError("effective_end must not precede effective_start")
        return self


class PricePoint(FrozenSpec):
    event: MarketEvent
    session_offset: int = Field(ge=0)
    price_field: Identifier


class LabelSpec(FrozenSpec):
    label_id: Identifier
    label_version: Version
    signal_cutoff: SignalCutoff
    expression: LabelExpression
    entry: PricePoint
    exit: PricePoint
    horizon_sessions: int = Field(ge=1)
    overlapping: bool
    suspension_handling: Identifier
    untradable_handling: Identifier
    corporate_action_handling: Identifier
    delisting_return_handling: Identifier
    benchmark_rule: Identifier
    tail_truncation_rule: Identifier

    @model_validator(mode="after")
    def validate_clock(self) -> LabelSpec:
        if self.exit.session_offset - self.entry.session_offset != self.horizon_sessions:
            raise ValueError("horizon_sessions must equal exit offset minus entry offset")
        if (
            self.signal_cutoff in {SignalCutoff.CLOSE, SignalCutoff.POST_CLOSE}
            and self.entry.session_offset == 0
            and self.entry.event is MarketEvent.CLOSE
        ):
            raise IntegrityViolation(
                "SAME_CLOSE_EXECUTION",
                "a close-derived signal cannot enter at the same session close",
                rule_id="RULE-003",
            )
        return self


class VersionRef(FrozenSpec):
    object_id: Identifier
    version: Version
    manifest_hash: Digest


class FactorSpec(FrozenSpec):
    factor_id: Identifier
    factor_version: Version
    name: Identifier
    author: Identifier
    source: Identifier
    economic_hypothesis: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    expected_mechanism: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    implementation_type: ImplementationType
    expression: FeatureExpression | None = None
    python_entrypoint: str | None = None
    required_fields: tuple[Identifier, ...] = Field(min_length=1)
    data_domains: tuple[DataDomain, ...] = Field(min_length=1)
    lookback_sessions: int = Field(ge=0)
    warmup_sessions: int = Field(ge=0)
    signal_cutoff: SignalCutoff
    missing_value_policy: Identifier
    infinite_value_policy: Identifier
    outlier_policy: Identifier
    allowed_universe_ids: tuple[Identifier, ...] = Field(min_length=1)
    direction: FactorDirection
    implementation_hash: Digest
    parent_factors: tuple[VersionRef, ...] = ()
    generation_process: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    test_references: tuple[Identifier, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_implementation_contract(self) -> FactorSpec:
        if self.implementation_type is ImplementationType.EXPRESSION:
            if self.expression is None or self.python_entrypoint is not None:
                raise ValueError("expression factors require expression and forbid python_entrypoint")
            dependency_fields = {item.field for item in self.expression.dependencies}
            dependency_domains = {item.data_domain for item in self.expression.dependencies}
            if dependency_fields != set(self.required_fields):
                raise ValueError("required_fields must exactly match expression dependencies")
            if dependency_domains != set(self.data_domains):
                raise ValueError("data_domains must exactly match expression dependencies")
        elif self.expression is not None or not self.python_entrypoint:
            raise ValueError("python factors require python_entrypoint and forbid expression")
        if self.warmup_sessions < max(0, self.lookback_sessions - 1):
            raise ValueError("warmup_sessions must cover the declared lookback")
        forbidden = {DataDomain.LABEL, DataDomain.HOLDOUT}.intersection(self.data_domains)
        if forbidden:
            raise IntegrityViolation(
                "FEATURE_DOMAIN_ACCESS",
                "FactorSpec cannot require Label or Holdout data domains",
                rule_id="RULE-005",
                context={"domains": sorted(item.value for item in forbidden)},
            )
        return self


class DateRange(FrozenSpec):
    start: date
    end: date

    @model_validator(mode="after")
    def validate_range(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("range end must not precede start")
        return self


class SplitSpec(FrozenSpec):
    train: DateRange
    validation: DateRange
    test: DateRange
    labels_overlap: bool
    label_horizon_sessions: int = Field(ge=1)
    purge_sessions: int = Field(ge=0)
    embargo_sessions: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_order_and_purge(self) -> SplitSpec:
        if not (self.train.end < self.validation.start <= self.validation.end < self.test.start):
            raise ValueError("train, validation, and test ranges must be ordered and disjoint")
        required_purge = self.label_horizon_sessions - 1 if self.labels_overlap else 0
        if self.purge_sessions < required_purge:
            raise IntegrityViolation(
                "LABEL_BOUNDARY_OVERLAP",
                "purge_sessions is too short for the declared overlapping label",
                rule_id="RULE-013",
                context={"required": required_purge, "actual": self.purge_sessions},
            )
        return self


class GitStateSpec(FrozenSpec):
    commit: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{40,64}$")]
    is_dirty: bool
    status_entries: tuple[str, ...]
    worktree_fingerprint: Digest

    @model_validator(mode="after")
    def validate_dirty_state(self) -> GitStateSpec:
        if self.is_dirty != bool(self.status_entries):
            raise ValueError("is_dirty must agree with status_entries")
        return self


class NamedValue(FrozenSpec):
    name: Identifier
    value: str | int | float | bool | None


class EvaluatorSpec(FrozenSpec):
    name: Identifier
    version: Version
    parameters: tuple[NamedValue, ...] = ()


class SearchDimension(FrozenSpec):
    name: Identifier
    values: tuple[str | int | float | bool, ...] = Field(min_length=1)


class ExperimentSpec(FrozenSpec):
    experiment_id: Annotated[str, StringConstraints(pattern=r"^EXP-[0-9]{8}-[A-Z2-7]{4}$")]
    parent_experiment_id: str | None = None
    hypothesis: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    constitution_version: Version
    git_state: GitStateSpec
    dataset: VersionRef
    universe: VersionRef
    factors: tuple[VersionRef, ...] = Field(min_length=1)
    label: VersionRef
    preprocessing_versions: tuple[VersionRef, ...]
    split: SplitSpec
    evaluator: EvaluatorSpec
    multiple_testing_family_id: Identifier
    execution_model_version: Version
    cost_model_version: Version
    capacity_model_version: Version
    search_space: tuple[SearchDimension, ...]
    search_budget: int = Field(ge=1)
    random_seed: int = Field(ge=0)
    promotion_gates: tuple[Identifier, ...] = Field(min_length=1)
    allowed_holdout_vintage: Identifier | None = None

    @field_validator("parent_experiment_id")
    @classmethod
    def validate_parent_id(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"EXP-[0-9]{8}-[A-Z2-7]{4}", value):
            raise ValueError("parent_experiment_id has an invalid format")
        return value

    @model_validator(mode="after")
    def validate_unique_references(self) -> ExperimentSpec:
        factor_keys = [(item.object_id, item.version) for item in self.factors]
        if len(factor_keys) != len(set(factor_keys)):
            raise ValueError("factor references must be unique")
        search_trials = 1
        for dimension in self.search_space:
            search_trials *= len(dimension.values)
        if search_trials > self.search_budget:
            raise ValueError("declared search space exceeds search_budget")
        return self
