from __future__ import annotations

from datetime import date

from alpha_research_os.portfolio import (
    DailyBarExecutionSpec,
    DailyBarLiquidity,
    FillStatus,
    OrderIntent,
    OrderSide,
    simulate_daily_bar_fill,
)


def _market(**updates: object) -> DailyBarLiquidity:
    values = {
        "reference_price": 10.0,
        "traded_amount_cny": 10_000_000.0,
        "up_limit": 11.0,
        "down_limit": 9.0,
        "is_suspended": False,
        "is_tradeable_bar": True,
    }
    values.update(updates)
    return DailyBarLiquidity.model_validate(values)


def test_buy_at_limit_is_conservatively_blocked() -> None:
    result = simulate_daily_bar_fill(
        OrderIntent(side=OrderSide.BUY, quantity=10_000),
        _market(reference_price=11.0),
        DailyBarExecutionSpec(),
    )
    assert result.status is FillStatus.LIMIT_BLOCKED


def test_sell_at_down_limit_is_conservatively_blocked() -> None:
    result = simulate_daily_bar_fill(
        OrderIntent(side=OrderSide.SELL, quantity=10_000),
        _market(reference_price=9.0),
        DailyBarExecutionSpec(),
    )
    assert result.status is FillStatus.LIMIT_BLOCKED


def test_capacity_is_checked_before_costs_are_charged() -> None:
    result = simulate_daily_bar_fill(
        OrderIntent(side=OrderSide.BUY, quantity=110_000),
        _market(),
        DailyBarExecutionSpec(maximum_participation_rate=0.10),
    )
    assert result.status is FillStatus.CAPACITY_BLOCKED
    assert result.total_cost_cny == 0


def test_filled_sell_includes_commission_stamp_and_adverse_slippage() -> None:
    result = simulate_daily_bar_fill(
        OrderIntent(side=OrderSide.SELL, quantity=10_000),
        _market(),
        DailyBarExecutionSpec(),
    )
    assert result.status is FillStatus.FILLED
    assert result.fill_price is not None and result.fill_price < 10.0
    assert result.commission_cny == 29.97
    assert result.stamp_duty_cny == 49.95
    assert result.transfer_fee_cny == 1.0
    assert result.total_cost_cny == 80.92


def test_commission_floor_and_historical_stamp_schedule() -> None:
    buy = simulate_daily_bar_fill(
        OrderIntent(side=OrderSide.BUY, quantity=100), _market(), DailyBarExecutionSpec(), date(2025, 1, 2)
    )
    old_sell = simulate_daily_bar_fill(
        OrderIntent(side=OrderSide.SELL, quantity=100), _market(), DailyBarExecutionSpec(), date(2023, 8, 25)
    )
    new_sell = simulate_daily_bar_fill(
        OrderIntent(side=OrderSide.SELL, quantity=100), _market(), DailyBarExecutionSpec(), date(2023, 8, 28)
    )

    assert buy.commission_cny == 5.0
    assert buy.stamp_duty_cny == 0.0
    assert old_sell.commission_cny == 5.0
    assert old_sell.stamp_duty_cny == 1.0
    assert new_sell.stamp_duty_cny == 0.5


def test_execution_identity_changes_with_capacity_or_cost() -> None:
    original = DailyBarExecutionSpec()
    assert original.spec_hash != original.model_copy(update={"maximum_participation_rate": 0.05}).spec_hash
    assert original.spec_hash != original.model_copy(update={"sell_stamp_duty_bps": 10.0}).spec_hash
