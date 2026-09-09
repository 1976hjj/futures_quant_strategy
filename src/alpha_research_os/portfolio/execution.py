"""Generic daily-bar order, fill, transaction-cost, and capacity semantics."""

from __future__ import annotations

import math
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal
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
    spec_version: str = "2.0.0"
    buy_commission_bps: float = Field(default=3.0, ge=0)
    sell_commission_bps: float = Field(default=3.0, ge=0)
    sell_stamp_duty_bps: float = Field(default=5.0, ge=0)
    historical_sell_stamp_duty_bps: float = Field(default=10.0, ge=0)
    stamp_duty_cutover: date = date(2023, 8, 28)
    minimum_commission_cny: float = Field(default=5.0, ge=0)
    transfer_fee_bps: float = Field(default=0.1, ge=0)
    historical_transfer_fee_bps: float = Field(default=0.2, ge=0)
    transfer_fee_cutover: date = date(2022, 4, 29)
    base_slippage_bps: float = Field(default=2.0, ge=0)
    square_root_impact_bps: float = Field(default=20.0, ge=0)
    maximum_slippage_bps: float = Field(default=100.0, ge=0)
    maximum_participation_rate: float = Field(default=0.10, gt=0, le=1)
    limit_price_tolerance: float = Field(default=1e-6, ge=0, le=0.01)
    lot_size: int = Field(default=100, ge=1)
    price_tick_cny: float = Field(default=0.01, gt=0)
    limit_fill_policy: str = "CONSERVATIVE_NO_FILL_AT_DAILY_LIMIT"
    delisting_policy: str = "OBSERVED_DELISTING_SESSION_CLOSE_ELSE_INVALID"

    @property
    def spec_hash(self) -> str:
        return content_hash(self)


class OrderIntent(FrozenSpec):
    side: OrderSide
    quantity: int = Field(gt=0)


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
    quantity: int
    participation_rate: float | None = None
    slippage_bps: float | None = None
    fill_price: float | None = None
    commission_cny: float = 0.0
    stamp_duty_cny: float = 0.0
    transfer_fee_cny: float = 0.0
    total_cost_cny: float = 0.0


def simulate_daily_bar_fill(
    order: OrderIntent,
    market: DailyBarLiquidity,
    spec: DailyBarExecutionSpec,
    trade_date: date | None = None,
) -> FillResult:
    """Apply deterministic, pessimistic daily-bar fill rules to one order."""

    reference_notional = order.quantity * (market.reference_price or 0.0)
    base = {
        "side": order.side,
        "requested_notional_cny": reference_notional,
        "quantity": order.quantity,
    }
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
    participation = reference_notional / market.traded_amount_cny
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
    raw_fill_price = market.reference_price * (1 + direction * slippage_bps / 10_000)
    tick = Decimal(str(spec.price_tick_cny))
    rounding = ROUND_CEILING if order.side is OrderSide.BUY else ROUND_FLOOR
    fill_price = float((Decimal(str(raw_fill_price)) / tick).to_integral_value(rounding=rounding) * tick)
    fill_price = min(fill_price, market.up_limit) if order.side is OrderSide.BUY else max(fill_price, market.down_limit)
    amount = _money(order.quantity * fill_price)
    commission_bps = spec.buy_commission_bps if order.side is OrderSide.BUY else spec.sell_commission_bps
    commission = _money(max(spec.minimum_commission_cny, amount * commission_bps / 10_000))
    effective_date = trade_date or spec.stamp_duty_cutover
    stamp_bps = (
        spec.historical_sell_stamp_duty_bps
        if effective_date < spec.stamp_duty_cutover
        else spec.sell_stamp_duty_bps
    )
    stamp = _money(amount * stamp_bps / 10_000) if order.side is OrderSide.SELL else 0.0
    transfer_bps = (
        spec.historical_transfer_fee_bps
        if effective_date < spec.transfer_fee_cutover
        else spec.transfer_fee_bps
    )
    transfer = _money(amount * transfer_bps / 10_000)
    return FillResult(
        status=FillStatus.FILLED,
        participation_rate=participation,
        slippage_bps=slippage_bps,
        fill_price=fill_price,
        commission_cny=commission,
        stamp_duty_cny=stamp,
        transfer_fee_cny=transfer,
        total_cost_cny=_money(commission + stamp + transfer),
        **base,
    )


def _money(value: float) -> float:
    """Round a cash amount to fen without binary-float banker's rounding."""

    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
