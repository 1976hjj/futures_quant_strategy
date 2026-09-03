from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import (
    AuditStatus,
    DataDomain,
    DatasetSpec,
    DateRange,
    EvaluatorSpec,
    ExperimentSpec,
    FactorDirection,
    FactorSpec,
    FeatureExpression,
    ImplementationType,
    LabelExpression,
    LabelSpec,
    MarketEvent,
    NamedValue,
    PartitionSpec,
    PricePoint,
    SearchDimension,
    SignalCutoff,
    SplitSpec,
    TemporalDependency,
    UniverseSpec,
)

HASH_A = "sha256:" + "a" * 64


def test_feature_expression_rejects_future_access() -> None:
    with pytest.raises(IntegrityViolation, match="FEATURE_FUTURE_ACCESS"):
        FeatureExpression(
            formula="lead(close, 1)",
            dependencies=(
                TemporalDependency(
                    field="close",
                    data_domain=DataDomain.MARKET,
                    relative_session=1,
                ),
            ),
        )


@pytest.mark.parametrize("domain", [DataDomain.LABEL, DataDomain.HOLDOUT])
def test_feature_expression_rejects_privileged_domains(domain: DataDomain) -> None:
    with pytest.raises(IntegrityViolation, match="FEATURE_DOMAIN_ACCESS"):
        FeatureExpression(
            formula="forbidden",
            dependencies=(TemporalDependency(field="x", data_domain=domain, relative_session=0),),
        )


def test_label_expression_requires_a_future_dependency() -> None:
    with pytest.raises(ValidationError, match="future dependency"):
        LabelExpression(
            formula="close / lag(close)",
            dependencies=(TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=0),),
        )


def test_same_close_label_entry_is_blocked() -> None:
    expression = LabelExpression(
        formula="Ref(close, -1) / close - 1",
        dependencies=(
            TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=0),
            TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=1),
        ),
    )
    with pytest.raises(IntegrityViolation, match="SAME_CLOSE_EXECUTION"):
        LabelSpec(
            label_id="forward-close",
            label_version="1.0.0",
            signal_cutoff=SignalCutoff.CLOSE,
            expression=expression,
            entry=PricePoint(event=MarketEvent.CLOSE, session_offset=0, price_field="close"),
            exit=PricePoint(event=MarketEvent.CLOSE, session_offset=1, price_field="close"),
            horizon_sessions=1,
            overlapping=False,
            suspension_handling="next_eligible_event",
            untradable_handling="no_fill",
            corporate_action_handling="cashflow_adjusted",
            delisting_return_handling="explicit_terminal_return",
            benchmark_rule="none",
            tail_truncation_rule="drop_incomplete",
        )


def test_dataset_and_universe_specs_encode_pit_lineage() -> None:
    dataset = DatasetSpec(
        dataset_id="a-share-synthetic",
        dataset_version="DS-20240901-001",
        schema_version="1.0.0",
        provider="synthetic",
        coverage_start=date(2024, 1, 1),
        coverage_end=date(2024, 8, 31),
        raw_snapshot_hashes=(HASH_A,),
        transformation_commit="1" * 40,
        pit_rule_version="1.0.0",
        adjustment_rule_version="1.0.0",
        partitions=(PartitionSpec(name="daily", row_count=42, content_hash=HASH_A),),
        audit_status=AuditStatus.PASSED,
        published_at=datetime(2024, 9, 1, tzinfo=UTC),
        publisher="m1-test",
    )
    universe = UniverseSpec(
        universe_id="a-share-all-history",
        universe_version="UV-001",
        dataset_version=dataset.dataset_version,
        membership_table="universe_membership",
        effective_start=dataset.coverage_start,
        effective_end=dataset.coverage_end,
        inclusion_rules=("listed_on_session",),
        exclusion_rules=("none",),
    )

    assert dataset.audit_status is AuditStatus.PASSED
    assert universe.as_of_field == "available_at"
    assert universe.preserve_delisted_history is True


def test_factor_spec_is_feature_only(feature_expression: FeatureExpression) -> None:
    factor = FactorSpec(
        factor_id="momentum-5d",
        factor_version="1.0.0",
        name="Five-day momentum",
        author="test",
        source="internal",
        economic_hypothesis="Short-horizon returns may persist.",
        expected_mechanism="Slow information diffusion.",
        implementation_type=ImplementationType.EXPRESSION,
        expression=feature_expression,
        required_fields=("close",),
        data_domains=(DataDomain.MARKET,),
        lookback_sessions=5,
        warmup_sessions=4,
        signal_cutoff=SignalCutoff.CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw",
        allowed_universe_ids=("a-share-all-history",),
        direction=FactorDirection.POSITIVE,
        implementation_hash=HASH_A,
        generation_process="manually specified M1 sentinel",
        test_references=("manual-case-momentum-001",),
    )

    assert factor.expression == feature_expression
    assert factor.implementation_type is ImplementationType.EXPRESSION


def test_overlapping_label_requires_sufficient_purge() -> None:
    with pytest.raises(IntegrityViolation, match="LABEL_BOUNDARY_OVERLAP") as error:
        SplitSpec(
            train=DateRange(start="2024-01-01", end="2024-06-30"),
            validation=DateRange(start="2024-07-01", end="2024-07-31"),
            test=DateRange(start="2024-08-01", end="2024-08-31"),
            labels_overlap=True,
            label_horizon_sessions=5,
            purge_sessions=0,
            embargo_sessions=1,
        )

    assert error.value.context == {"required": 4, "actual": 0}


def test_forged_model_copy_is_revalidated_at_a_trust_boundary(feature_expression: FeatureExpression) -> None:
    forged = feature_expression.model_copy(
        update={"dependencies": (TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=1),)}
    )

    with pytest.raises(IntegrityViolation, match="FEATURE_FUTURE_ACCESS"):
        FeatureExpression.model_validate(forged)


def test_experiment_spec_freezes_search_budget(
    split_spec: SplitSpec,
    clean_git_state,
    version_ref,
) -> None:
    experiment = ExperimentSpec(
        experiment_id="EXP-20240901-A234",
        hypothesis="The sentinel factor has non-zero out-of-sample rank IC.",
        constitution_version="1.0.0-draft",
        git_state=clean_git_state,
        dataset=version_ref("dataset"),
        universe=version_ref("universe"),
        factors=(version_ref("factor"),),
        label=version_ref("label"),
        preprocessing_versions=(),
        split=split_spec,
        evaluator=EvaluatorSpec(
            name="rank-ic",
            version="1.0.0",
            parameters=(NamedValue(name="min_coverage", value=0.8),),
        ),
        multiple_testing_family_id="MTF-001",
        execution_model_version="diagnostic-only-1",
        cost_model_version="not-applicable-1",
        capacity_model_version="not-applicable-1",
        search_space=(SearchDimension(name="window", values=(5, 10)),),
        search_budget=2,
        random_seed=7,
        promotion_gates=("no-lookahead", "oos-required"),
    )

    assert experiment.git_state.is_dirty is False
    with pytest.raises(ValidationError, match="frozen"):
        experiment.random_seed = 8
