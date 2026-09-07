"""Generic daily-bar order, fill, transaction-cost, and capacity semantics."""

from __future__ import annotations

import math
from enum import StrEnum

from pydantic import Field, field_validator

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import FrozenSpec


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class FillStatus(StrEnum):
    FILLED = "FILLED"
    MISSING_BAR = "MISSING_BAR"
    SUSPENDED = "SUSPENDED"
    LIMIT_BLOCKED = "LIMIT_BLOCKED"
    INVALID_PRICE = "INVALID_PRICE"
    INVALID_LIQUIDITY = "INVALID_LIQUIDITY"
    CAPACITY_BLOCKED = "CAPACITY_BLOCKED"


class DailyBarExecutionSpec(FrozenSpec):
    """Frozen assumptions shared by factor scores and future model predictions."""

    spec_id: str = "cn-a-daily-bar-execution"
    spec_version: str = "1.0.0"
    buy_commission_bps: float = Field(default=3.0, ge=0)
    sell_commission_bps: float = Field(default=3.0, ge=0)
    sell_stamp_duty_bps: float = Field(default=5.0, ge=0)
    base_slippage_bps: float = Field(default=2.0, ge=0)
    square_root_impact_bps: float = Field(default=20.0, ge=0)
    maximum_slippage_bps: float = Field(default=100.0, ge=0)
    maximum_participation_rate: float = Field(default=0.10, gt=0, le=1)
    limit_price_tolerance: float = Field(default=1e-6, ge=0, le=0.01)
    lot_size: int = Field(default=100, ge=1)
    limit_fill_policy: str = "CONSERVATIVE_NO_FILL_AT_DAILY_LIMIT"
    delisting_policy: str = "OBSERVED_DELISTING_SESSION_CLOSE_ELSE_INVALID"

    @property
    def spec_hash(self) -> str:
        return content_hash(self)


class OrderIntent(FrozenSpec):
    side: OrderSide
    notional_cny: float = Field(gt=0)


class DailyBarLiquidity(FrozenSpec):
    reference_price: float | None
    traded_amount_cny: float | None
    up_limit: float | None
    down_limit: float | None
    is_suspended: bool
    is_tradeable_bar: bool

    @field_validator("reference_price", "traded_amount_cny", "up_limit", "down_limit")
    @classmethod
    def finite_or_missing(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("daily bar inputs must be finite or missing")
        return value


class FillResult(FrozenSpec):
    status: FillStatus
    side: OrderSide
    requested_notional_cny: float
    participation_rate: float | None = None
    slippage_bps: float | None = None
    fill_price: float | None = None
    commission_cny: float = 0.0
    stamp_duty_cny: float = 0.0
    total_cost_cny: float = 0.0


def simulate_daily_bar_fill(
    order: OrderIntent,
    market: DailyBarLiquidity,
    spec: DailyBarExecutionSpec,
) -> FillResult:
    """Apply deterministic, pessimistic daily-bar fill rules to one order."""

    base = {"side": order.side, "requested_notional_cny": order.notional_cny}
    if market.reference_price is None:
        return FillResult(status=FillStatus.MISSING_BAR, **base)
    if market.reference_price <= 0:
        return FillResult(status=FillStatus.INVALID_PRICE, **base)
    if market.is_suspended or not market.is_tradeable_bar:
        return FillResult(status=FillStatus.SUSPENDED, **base)
    if market.up_limit is None or market.down_limit is None:
        return FillResult(status=FillStatus.MISSING_BAR, **base)
    tolerance = spec.limit_price_tolerance
    if order.side is OrderSide.BUY and market.reference_price >= market.up_limit * (1 - tolerance):
        return FillResult(status=FillStatus.LIMIT_BLOCKED, **base)
    if order.side is OrderSide.SELL and market.reference_price <= market.down_limit * (1 + tolerance):
        return FillResult(status=FillStatus.LIMIT_BLOCKED, **base)
    if market.traded_amount_cny is None or market.traded_amount_cny <= 0:
        return FillResult(status=FillStatus.INVALID_LIQUIDITY, **base)
    participation = order.notional_cny / market.traded_amount_cny
    if participation > spec.maximum_participation_rate:
        return FillResult(
            status=FillStatus.CAPACITY_BLOCKED,
            participation_rate=participation,
            **base,
        )
    slippage_bps = min(
        spec.maximum_slippage_bps,
        spec.base_slippage_bps + spec.square_root_impact_bps * math.sqrt(participation),
    )
    direction = 1.0 if order.side is OrderSide.BUY else -1.0
    fill_price = market.reference_price * (1 + direction * slippage_bps / 10_000)
    commission_bps = spec.buy_commission_bps if order.side is OrderSide.BUY else spec.sell_commission_bps
    commission = order.notional_cny * commission_bps / 10_000
    stamp = order.notional_cny * spec.sell_stamp_duty_bps / 10_000 if order.side is OrderSide.SELL else 0.0
    return FillResult(
        status=FillStatus.FILLED,
        participation_rate=participation,
        slippage_bps=slippage_bps,
        fill_price=fill_price,
        commission_cny=commission,
        stamp_duty_cny=stamp,
        total_cost_cny=commission + stamp,
        **base,
    )
