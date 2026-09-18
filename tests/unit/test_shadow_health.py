import pytest
from pydantic import ValidationError

from alpha_research_os.portfolio.shadow_health import ShadowHealthSpec
from alpha_research_os.portfolio.strategy_backtest import StrategyBacktestRequest


def test_only_baseline_and_s4_v3_are_accepted() -> None:
    assert ShadowHealthSpec(experiment_variant="S0").experiment_variant == "S0"
    assert ShadowHealthSpec(experiment_variant="S4V3").experiment_variant == "S4V3"
    with pytest.raises(ValidationError):
        ShadowHealthSpec(experiment_variant="S4")


def test_s4_v3_thresholds_and_exposures_are_ordered() -> None:
    with pytest.raises(ValidationError):
        ShadowHealthSpec(external_weak_breadth=0.60, external_strong_breadth=0.60)
    with pytest.raises(ValidationError):
        ShadowHealthSpec(regime_ordinary_drawdown=0.15, regime_severe_drawdown=0.10)
    with pytest.raises(ValidationError):
        ShadowHealthSpec(regime_weak_exposure=0.50, regime_base_exposure=0.50)


def test_s4_v3_allows_actual_position_sequence() -> None:
    request = StrategyBacktestRequest(
        name="actual-position-external-regime-validation",
        start="2020-01-01",
        end="2020-12-31",
        selection_sequence_mode="ACTUAL_POSITIONS",
        score_rules=[
            {
                "factor_id": "test-factor",
                "release_id": "sha256:" + "a" * 64,
                "weight": 1,
            }
        ],
        shadow_health={"experiment_variant": "S4V3"},
    )
    assert request.shadow_health.experiment_variant == "S4V3"
