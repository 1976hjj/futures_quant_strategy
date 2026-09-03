from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alpha_research_os.factors import (
    FactorRegistry,
    FeatureInputRow,
    FeatureRuntime,
    FeatureValue,
    compile_feature_expression,
)
from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import (
    DataDomain,
    FactorDirection,
    FactorSpec,
    FeatureExpression,
    ImplementationType,
    SignalCutoff,
    TemporalDependency,
)


def _expression_spec(
    formula: str = "close / Ref(close, 1) - 1",
    *,
    factor_id: str = "sentinel",
    implementation_hash: str | None = None,
    dependencies: tuple[TemporalDependency, ...] | None = None,
) -> FactorSpec:
    compiled = compile_feature_expression(formula)
    declared = dependencies or tuple(
        TemporalDependency(
            field=item.field,
            data_domain=DataDomain.MARKET,
            relative_session=item.relative_session,
        )
        for item in compiled.dependencies
    )
    return FactorSpec(
        factor_id=factor_id,
        factor_version="1.0.0",
        name="Sentinel",
        author="test",
        source="manual",
        economic_hypothesis="Test hypothesis.",
        expected_mechanism="Test mechanism.",
        implementation_type=ImplementationType.EXPRESSION,
        expression=FeatureExpression(formula=formula, dependencies=declared),
        required_fields=tuple(sorted({item.field for item in declared})),
        data_domains=tuple(sorted({item.data_domain for item in declared}, key=lambda item: item.value)),
        lookback_sessions=compiled.required_history + 1,
        warmup_sessions=compiled.required_history,
        signal_cutoff=SignalCutoff.CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw",
        allowed_universe_ids=("ALL-A",),
        direction=FactorDirection.POSITIVE,
        implementation_hash=implementation_hash or compiled.implementation_hash,
        generation_process="manual test",
        test_references=("factor-factory-test",),
    )


@pytest.mark.parametrize(
    "formula",
    [
        "Lead(close, 1)",
        "close.__class__",
        "data['close']",
        "[close for close in values]",
        "__import__('os')",
        "Mean(close, window=5)",
        "Ref(close, -1)",
        "close ** 2",
    ],
)
def test_compiler_rejects_non_whitelisted_or_ambiguous_syntax(formula: str) -> None:
    with pytest.raises(IntegrityViolation, match="FACTOR_EXPRESSION_REJECTED"):
        compile_feature_expression(formula)


def test_canonical_ast_hash_ignores_formatting() -> None:
    compact = compile_feature_expression("close/Ref(close,1)-1")
    spaced = compile_feature_expression("close / Ref(close, 1) - 1")

    assert compact.implementation_hash == spaced.implementation_hash
    assert compact.dependencies == spaced.dependencies


def test_registry_rejects_hidden_temporal_dependency() -> None:
    dependencies = (
        TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=0),
    )
    spec = _expression_spec(dependencies=dependencies)

    with pytest.raises(IntegrityViolation, match="FACTOR_DEPENDENCY_MISMATCH") as error:
        FactorRegistry().register(spec)

    assert ("close", -1) in error.value.context["compiled"]


def test_registry_rejects_forged_implementation_hash() -> None:
    forged = "sha256:" + "f" * 64

    with pytest.raises(IntegrityViolation, match="FACTOR_IMPLEMENTATION_HASH_MISMATCH"):
        FactorRegistry().register(_expression_spec(implementation_hash=forged))


def test_registry_is_idempotent_but_version_identity_is_immutable() -> None:
    registry = FactorRegistry()
    first = registry.register(_expression_spec())
    second = registry.register(_expression_spec())
    conflicting = _expression_spec(formula="close / Ref(close, 2) - 1")

    assert first is second
    with pytest.raises(IntegrityViolation, match="FACTOR_VERSION_CONFLICT"):
        registry.register(conflicting)


def test_feature_runtime_rejects_privileged_domain_at_construction() -> None:
    with pytest.raises(IntegrityViolation, match="FEATURE_VIEW_PRIVILEGED_DOMAIN"):
        FeatureRuntime({"future_return": DataDomain.LABEL})


def test_feature_runtime_propagates_missing_and_zero_division() -> None:
    registry = FactorRegistry()
    factor = registry.register(_expression_spec(formula="close / open - 1"))
    runtime = FeatureRuntime({"close": DataDomain.MARKET, "open": DataDomain.MARKET})
    rows = (
        FeatureInputRow(
            session=date(2024, 1, 2),
            instrument_id="A",
            available_at=datetime(2024, 1, 2, 15, tzinfo=UTC),
            values=(FeatureValue(name="close", value=10), FeatureValue(name="open", value=0)),
        ),
    )

    assert runtime.run(factor, rows)[0].value is None


def test_python_factor_cannot_escape_into_in_process_runtime() -> None:
    python_spec = FactorSpec(
        factor_id="python-sentinel",
        factor_version="1.0.0",
        name="Python sentinel",
        author="test",
        source="manual",
        economic_hypothesis="Test hypothesis.",
        expected_mechanism="Test mechanism.",
        implementation_type=ImplementationType.PYTHON,
        python_entrypoint="untrusted.module:factor",
        required_fields=("close",),
        data_domains=(DataDomain.MARKET,),
        lookback_sessions=1,
        warmup_sessions=0,
        signal_cutoff=SignalCutoff.CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw",
        allowed_universe_ids=("ALL-A",),
        direction=FactorDirection.POSITIVE,
        implementation_hash="sha256:" + "a" * 64,
        generation_process="manual test",
        test_references=("python-sandbox-test",),
    )
    factor = FactorRegistry().register(python_spec)

    with pytest.raises(IntegrityViolation, match="PYTHON_PLUGIN_SANDBOX_REQUIRED"):
        FeatureRuntime({"close": DataDomain.MARKET}).run(factor, ())
