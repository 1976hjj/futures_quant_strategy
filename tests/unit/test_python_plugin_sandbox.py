from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alpha_research_os.factors import FactorRegistry, FeatureInputRow, FeatureValue
from alpha_research_os.factors.plugin import (
    PluginSandboxLimits,
    PythonPluginRuntime,
    PythonPluginSource,
    publish_python_plugin,
)
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import (
    DataDomain,
    FactorDirection,
    FactorSpec,
    ImplementationType,
    SignalCutoff,
)

SAFE_SOURCE = """
def factor():
    spread = current("high") - current("low")
    if is_missing(spread) or spread == 0:
        return None
    location = safe_div(current("close") - current("low"), spread)
    if location > 0.8:
        return 1.0
    if location < 0.2:
        return -1.0
    return 0.0
"""


def _plugin(source: str = SAFE_SOURCE) -> PythonPluginSource:
    return PythonPluginSource(plugin_id="conditional-close-location", plugin_version="1.0.0", source=source)


def _registered(
    plugin: PythonPluginSource,
    *,
    implementation_hash: str | None = None,
    lookback: int = 1,
    required_fields: tuple[str, ...] = ("close", "high", "low"),
):
    spec = FactorSpec(
        factor_id="conditional-close-location-python",
        factor_version="1.0.0",
        name="Conditional close location Python factor",
        author="test",
        source="internal-plugin-test",
        economic_hypothesis="Close location can summarize intraday pressure.",
        expected_mechanism="Map extreme close locations to a signed discrete signal.",
        implementation_type=ImplementationType.PYTHON,
        python_entrypoint=plugin.entrypoint_ref,
        required_fields=required_fields,
        data_domains=(DataDomain.MARKET,),
        lookback_sessions=lookback,
        warmup_sessions=lookback - 1,
        signal_cutoff=SignalCutoff.POST_CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw",
        allowed_universe_ids=("ALL-A-PIT",),
        direction=FactorDirection.TRAIN_FIT,
        implementation_hash=implementation_hash or plugin.implementation_hash,
        generation_process="M3.3 sandbox verification.",
        test_references=("m3-3-python-plugin-sandbox",),
    )
    return FactorRegistry().register(spec)


def _rows() -> tuple[FeatureInputRow, ...]:
    return (
        FeatureInputRow(
            session=date(2024, 1, 2),
            instrument_id="600036.SH",
            available_at=datetime(2024, 1, 2, 15, tzinfo=UTC),
            values=(
                FeatureValue(name="close", value=10.9),
                FeatureValue(name="high", value=11.0),
                FeatureValue(name="low", value=10.0),
                FeatureValue(name="unused", value=999),
            ),
        ),
        FeatureInputRow(
            session=date(2024, 1, 3),
            instrument_id="600036.SH",
            available_at=datetime(2024, 1, 3, 15, tzinfo=UTC),
            values=(
                FeatureValue(name="close", value=10.5),
                FeatureValue(name="high", value=11.0),
                FeatureValue(name="low", value=10.0),
            ),
        ),
    )


def test_restricted_plugin_runs_out_of_process_and_is_deterministic() -> None:
    plugin = _plugin()
    factor = _registered(plugin)
    runtime = PythonPluginRuntime({"close": DataDomain.MARKET, "high": DataDomain.MARKET, "low": DataDomain.MARKET})

    first = runtime.run(factor, plugin, _rows())
    second = runtime.run(factor, plugin, _rows())

    assert [item.value for item in first.values] == [1.0, 0.0]
    assert first.input_hash == second.input_hash
    assert first.plugin_hash == plugin.implementation_hash
    assert first.policy_version == "restricted-python-factor-v1"


@pytest.mark.parametrize(
    "source",
    [
        "import os\ndef factor():\n    return 1\n",
        'def factor():\n    return open("secret.txt")\n',
        "def factor():\n    return ().__class__\n",
        'def factor():\n    return history("close", 1)[0]\n',
        "def factor():\n    return TUSHARE_TOKEN\n",
        "def factor():\n    while True:\n        pass\n",
        "def factor(value):\n    return value\n",
    ],
)
def test_restricted_plugin_rejects_escape_surfaces(source: str) -> None:
    plugin = _plugin(source)
    factor = _registered(plugin)
    runtime = PythonPluginRuntime({"close": DataDomain.MARKET, "high": DataDomain.MARKET, "low": DataDomain.MARKET})

    with pytest.raises(IntegrityViolation, match="PLUGIN_SOURCE_REJECTED"):
        runtime.run(factor, plugin, _rows())


def test_plugin_source_is_bound_to_factor_implementation_hash() -> None:
    plugin = _plugin()
    factor = _registered(plugin, implementation_hash="sha256:" + "a" * 64)
    runtime = PythonPluginRuntime({"close": DataDomain.MARKET, "high": DataDomain.MARKET, "low": DataDomain.MARKET})

    with pytest.raises(IntegrityViolation, match="PLUGIN_IMPLEMENTATION_HASH_MISMATCH"):
        runtime.run(factor, plugin, _rows())


def test_plugin_version_is_bound_to_one_immutable_source(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = _plugin()
    publish_python_plugin(store, first)
    publish_python_plugin(store, first)
    conflicting = _plugin(SAFE_SOURCE.replace("return 0.0", "return 0.1"))

    with pytest.raises(IntegrityViolation, match="ARTIFACT_IMMUTABILITY"):
        publish_python_plugin(store, conflicting)


def test_plugin_runtime_rejects_privileged_feature_views() -> None:
    with pytest.raises(IntegrityViolation, match="PLUGIN_PRIVILEGED_DOMAIN"):
        PythonPluginRuntime({"future_return": DataDomain.LABEL})


def test_plugin_timeout_kills_expensive_computation() -> None:
    plugin = _plugin(
        """
def factor():
    total = 0
    for left in history("close", 5000):
        for right in history("close", 5000):
            total += 1
    return total
"""
    )
    factor = _registered(plugin, lookback=5000, required_fields=("close",))
    runtime = PythonPluginRuntime(
        {"close": DataDomain.MARKET, "high": DataDomain.MARKET, "low": DataDomain.MARKET},
        limits=PluginSandboxLimits(timeout_ms=100),
    )

    with pytest.raises(IntegrityViolation, match="PLUGIN_TIMEOUT"):
        runtime.run(factor, plugin, _rows()[:1])
