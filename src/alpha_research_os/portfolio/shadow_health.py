"""Configuration for the retained shadow-account market regime."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from alpha_research_os.kernel.specs import FrozenSpec


class ShadowHealthSpec(FrozenSpec):
    """S0 baseline or the point-in-time S4-V3 market/shadow regime."""

    experiment_variant: Literal["S0", "S4V3"] = "S0"
    drawdown_peak_lookback_sessions: int = Field(default=252, ge=20, le=504)
    external_trend_sessions: int = Field(default=200, ge=20, le=1000)
    external_breadth_return_sessions: int = Field(default=20, ge=2, le=252)
    external_strong_breadth: float = Field(default=0.60, ge=0, le=1)
    external_weak_breadth: float = Field(default=0.35, ge=0, le=1)
    regime_ordinary_drawdown: float = Field(default=0.08, gt=0, lt=1)
    regime_severe_drawdown: float = Field(default=0.15, gt=0, lt=1)
    regime_base_exposure: float = Field(default=0.50, ge=0, le=1)
    regime_weak_exposure: float = Field(default=0.30, ge=0, le=1)
    regime_strong_exposure: float = Field(default=1.00, ge=0, le=1)
    regime_down_confirmation_sessions: int = Field(default=2, ge=1, le=20)
    regime_up_confirmation_sessions: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def valid_regime_rules(self) -> ShadowHealthSpec:
        if self.external_weak_breadth >= self.external_strong_breadth:
            raise ValueError("external weak breadth must be below strong breadth")
        if self.regime_severe_drawdown <= self.regime_ordinary_drawdown:
            raise ValueError("regime severe drawdown must exceed ordinary drawdown")
        if not (
            0
            <= self.regime_weak_exposure
            < self.regime_base_exposure
            < self.regime_strong_exposure
            <= 1
        ):
            raise ValueError("external regime exposures must be ordered weak, base, strong")
        return self
