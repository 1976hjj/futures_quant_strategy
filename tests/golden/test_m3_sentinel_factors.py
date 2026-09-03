from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from alpha_research_os.factors import (
    FactorRegistry,
    FeatureInputRow,
    FeatureRuntime,
    FeatureValue,
    compile_feature_expression,
)
from alpha_research_os.kernel.specs import (
    DataDomain,
    FactorDirection,
    FactorSpec,
    FeatureExpression,
    ImplementationType,
    SignalCutoff,
    TemporalDependency,
)

CHINA_STANDARD_TIME = timezone(timedelta(hours=8))


def _factor(factor_id: str, formula: str, *, direction: FactorDirection) -> FactorSpec:
    compiled = compile_feature_expression(formula)
    fields = tuple(sorted(compiled.fields))
    return FactorSpec(
        factor_id=factor_id,
        factor_version="1.0.0",
        name=f"M3 sentinel {factor_id}",
        author="m3-golden-test",
        source="manual",
        economic_hypothesis="A deliberately simple expression exercises the factor factory contract.",
        expected_mechanism="This is a sentinel implementation test, not evidence of an investable anomaly.",
        implementation_type=ImplementationType.EXPRESSION,
        expression=FeatureExpression(
            formula=formula,
            dependencies=tuple(
                TemporalDependency(
                    field=item.field,
                    data_domain=DataDomain.MARKET,
                    relative_session=item.relative_session,
                )
                for item in compiled.dependencies
            ),
        ),
        required_fields=fields,
        data_domains=(DataDomain.MARKET,),
        lookback_sessions=compiled.required_history + 1,
        warmup_sessions=compiled.required_history,
        signal_cutoff=SignalCutoff.CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw",
        allowed_universe_ids=("M3-GOLDEN-A-SHARE",),
        direction=direction,
        implementation_hash=compiled.implementation_hash,
        generation_process="pre-registered hand-calculated M3 sentinel",
        test_references=(f"golden-{factor_id}-001",),
    )


def _rows() -> tuple[FeatureInputRow, ...]:
    start = date(2024, 1, 2)
    closes = (10, 11, 12, 13, 14, 15)
    opens = (9, 10, 11, 12, 13, 14)
    volumes = (100, 200, 300, 400, 500, 600)
    return tuple(
        FeatureInputRow(
            session=start + timedelta(days=index),
            instrument_id="CN-EQ-GOLDEN-001",
            available_at=datetime(2024, 1, 2 + index, 15, tzinfo=CHINA_STANDARD_TIME),
            values=(
                FeatureValue(name="open", value=opens[index]),
                FeatureValue(name="close", value=closes[index]),
                FeatureValue(name="volume", value=volumes[index]),
            ),
        )
        for index in range(6)
    )


@pytest.mark.parametrize(
    ("factor_id", "formula", "direction", "expected"),
    [
        ("momentum-5", "close / Ref(close, 5) - 1", FactorDirection.POSITIVE, 0.5),
        ("reversal-1", "-(close / Ref(close, 1) - 1)", FactorDirection.POSITIVE, -(1 / 14)),
        ("overnight-gap", "open / Ref(close, 1) - 1", FactorDirection.POSITIVE, 0.0),
        ("intraday-return", "close / open - 1", FactorDirection.POSITIVE, 1 / 14),
        ("volume-ratio-5", "volume / Mean(volume, 5) - 1", FactorDirection.NEGATIVE, 0.5),
    ],
)
def test_five_sentinel_factors_match_hand_calculation(
    factor_id: str,
    formula: str,
    direction: FactorDirection,
    expected: float,
) -> None:
    registry = FactorRegistry()
    registered = registry.register(_factor(factor_id, formula, direction=direction))
    runtime = FeatureRuntime(
        {"open": DataDomain.MARKET, "close": DataDomain.MARKET, "volume": DataDomain.MARKET}
    )

    values = runtime.run(registered, _rows())

    assert values[-1].value == pytest.approx(expected)
    assert values[-1].variant == "RAW"
    assert values[-1].implementation_hash == registered.spec.implementation_hash


def test_missing_session_is_not_compressed_into_a_shorter_lookback() -> None:
    registry = FactorRegistry()
    registered = registry.register(
        _factor("momentum-1", "close / Ref(close, 1) - 1", direction=FactorDirection.POSITIVE)
    )
    runtime = FeatureRuntime({"close": DataDomain.MARKET})
    day_one = date(2024, 1, 2)
    day_two = date(2024, 1, 3)
    common_time = datetime(2024, 1, 3, 15, tzinfo=CHINA_STANDARD_TIME)
    rows = (
        FeatureInputRow(
            session=day_one,
            instrument_id="A",
            available_at=common_time,
            values=(FeatureValue(name="close", value=10),),
        ),
        FeatureInputRow(
            session=day_one,
            instrument_id="B",
            available_at=common_time,
            values=(FeatureValue(name="close", value=20),),
        ),
        FeatureInputRow(
            session=day_two,
            instrument_id="B",
            available_at=common_time,
            values=(FeatureValue(name="close", value=22),),
        ),
    )

    values = runtime.run(registered, rows)

    assert [(item.instrument_id, item.session, item.value) for item in values] == [
        ("A", day_one, None),
        ("B", day_one, None),
        ("B", day_two, pytest.approx(0.1)),
    ]
