"""Auditable seed plugins used to verify the M3.3 sandbox path."""

from __future__ import annotations

from alpha_research_os.kernel.specs import (
    DataDomain,
    FactorDirection,
    FactorSpec,
    ImplementationType,
    SignalCutoff,
)

from .catalog import FactorCatalogEntry, FactorLifecycle
from .library import INTERNAL_SOURCE
from .plugin import PythonPluginSource

CONDITIONAL_CLOSE_LOCATION_SOURCE = """
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


def conditional_close_location_plugin() -> tuple[FactorCatalogEntry, PythonPluginSource]:
    """Return a branch-based factor that the expression DSL cannot represent."""

    plugin = PythonPluginSource(
        plugin_id="conditional-close-location",
        plugin_version="1.0.0",
        source=CONDITIONAL_CLOSE_LOCATION_SOURCE,
    )
    spec = FactorSpec(
        factor_id="conditional-close-location-python",
        factor_version="1.0.0",
        name="Conditional close location Python factor",
        author="alpha-research-os",
        source=INTERNAL_SOURCE.source_id,
        economic_hypothesis="Extreme close location may summarize persistent intraday order imbalance.",
        expected_mechanism="Map closes near the daily high or low to a discrete signed pressure signal.",
        implementation_type=ImplementationType.PYTHON,
        python_entrypoint=plugin.entrypoint_ref,
        required_fields=("close", "high", "low"),
        data_domains=(DataDomain.MARKET,),
        lookback_sessions=1,
        warmup_sessions=0,
        signal_cutoff=SignalCutoff.POST_CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw",
        allowed_universe_ids=("ALL-A-PIT",),
        direction=FactorDirection.TRAIN_FIT,
        implementation_hash=plugin.implementation_hash,
        generation_process="M3.3 sandbox sentinel fixed before real-data execution.",
        test_references=("m3-3-python-plugin-sandbox",),
    )
    return (
        FactorCatalogEntry(
            spec=spec,
            family="price-behavior",
            source_reference=INTERNAL_SOURCE,
            adaptation_notes="Native sandbox sentinel; no external formula adaptation.",
            lifecycle=FactorLifecycle.CANDIDATE,
        ),
        plugin,
    )
