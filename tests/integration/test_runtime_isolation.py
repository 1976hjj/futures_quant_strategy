from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from alpha_research_os.kernel.capabilities import CapabilityAuthority, CapabilityScope
from alpha_research_os.kernel.errors import CapabilityDeniedError, IntegrityViolation
from alpha_research_os.kernel.runtime import FeatureRuntime, LabelRuntime, PITRecord
from alpha_research_os.kernel.specs import DataDomain, LabelExpression, TemporalDependency


def _record(
    *,
    field: str,
    domain: DataDomain,
    available_offset: int = 0,
    ingested_offset: int = 0,
    record_id: str,
) -> PITRecord:
    cutoff = datetime(2024, 4, 29, 15, tzinfo=UTC)
    return PITRecord(
        instrument_id="CN-EQ-000001",
        field=field,
        value=1.0,
        event_time=cutoff,
        published_at=cutoff,
        available_at=cutoff + timedelta(days=available_offset),
        ingested_at=cutoff + timedelta(days=ingested_offset),
        data_domain=domain,
        source_record_id=record_id,
    )


def test_feature_runtime_filters_records_by_available_and_ingested_time(feature_expression) -> None:
    runtime = FeatureRuntime()
    cutoff = datetime(2024, 4, 29, 15, tzinfo=UTC)
    records = (
        _record(field="close", domain=DataDomain.MARKET, record_id="safe"),
        _record(field="close", domain=DataDomain.MARKET, available_offset=1, record_id="future"),
        _record(field="close", domain=DataDomain.MARKET, ingested_offset=1, record_id="late-ingest"),
    )

    selected = runtime.records_as_of(feature_expression, records, signal_cutoff=cutoff)

    assert [record.source_record_id for record in selected] == ["safe"]


def test_feature_runtime_rejects_label_record_even_if_formula_does_not_reference_it(feature_expression) -> None:
    with pytest.raises(IntegrityViolation, match="FEATURE_DOMAIN_ACCESS"):
        FeatureRuntime().records_as_of(
            feature_expression,
            (_record(field="forward_return", domain=DataDomain.LABEL, record_id="label"),),
            signal_cutoff=datetime(2024, 4, 29, 15, tzinfo=UTC),
        )


def test_feature_runtime_rejects_impossible_availability_chronology(feature_expression) -> None:
    cutoff = datetime(2024, 4, 29, 15, tzinfo=UTC)
    record = PITRecord(
        instrument_id="CN-EQ-000001",
        field="close",
        value=1.0,
        event_time=cutoff,
        published_at=cutoff + timedelta(hours=1),
        available_at=cutoff,
        ingested_at=cutoff,
        data_domain=DataDomain.MARKET,
        source_record_id="premature",
    )

    with pytest.raises(IntegrityViolation, match="AVAILABILITY_PRECEDES_PUBLICATION"):
        FeatureRuntime().records_as_of(feature_expression, (record,), signal_cutoff=cutoff)


def test_label_runtime_requires_evaluator_bound_capability() -> None:
    authority = CapabilityAuthority()
    runtime = LabelRuntime(authority)
    expression = LabelExpression(
        formula="future(close, 5) / close - 1",
        dependencies=(
            TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=0),
            TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=5),
        ),
    )

    with pytest.raises(CapabilityDeniedError, match="LABEL_ACCESS_DENIED"):
        runtime.authorize(expression, actor="evaluator", experiment_id="EXP-1", capability=None)

    capability = authority.issue(
        actor="evaluator",
        scope=CapabilityScope.READ_LABEL,
        resource="EXP-1",
        now=datetime(2024, 9, 1, tzinfo=UTC),
    )
    assert (
        runtime.authorize(
            expression,
            actor="evaluator",
            experiment_id="EXP-1",
            capability=capability,
        )
        == expression
    )
