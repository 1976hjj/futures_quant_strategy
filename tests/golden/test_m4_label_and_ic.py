from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from alpha_research_os.evaluation import (
    ExecutionConstraintLevel,
    ForwardReturnLabel,
    ForwardReturnLabelBuilder,
    LabelInvalidReason,
    MarketLabelRow,
    SignalKey,
    default_forward_5d_label_spec,
    evaluate_basic_factor,
)
from alpha_research_os.factors import RawFactorValue
from alpha_research_os.kernel.errors import IntegrityViolation


def _market_row(
    session: date,
    *,
    open_price: float = 10,
    close_price: float = 10,
    adj_factor: float = 1,
    suspended: bool = False,
    tradeable: bool = True,
    can_buy: bool | None = None,
    can_sell: bool | None = None,
) -> MarketLabelRow:
    return MarketLabelRow(
        session=session,
        instrument_id="A",
        available_at=datetime.combine(session, datetime.min.time(), UTC) + timedelta(hours=15),
        open=open_price,
        close=close_price,
        adj_factor=adj_factor,
        eligible_for_signal=True,
        is_suspended=suspended,
        is_tradeable_bar=tradeable,
        can_buy_open=can_buy,
        can_sell_close=can_sell,
    )


def test_forward_label_uses_fixed_sessions_and_adjusted_total_return() -> None:
    sessions = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(7))
    rows = [_market_row(session) for session in sessions]
    rows[1] = _market_row(sessions[1], open_price=10, adj_factor=1)
    rows[6] = _market_row(sessions[6], close_price=11, adj_factor=1.1)
    builder = ForwardReturnLabelBuilder(default_forward_5d_label_spec(), require_limit_flags=False)

    label = builder.build((SignalKey(session=sessions[0], instrument_id="A"),), sessions, rows)[0]

    assert label.is_valid
    assert label.entry_session == sessions[1]
    assert label.exit_session == sessions[6]
    assert label.value == pytest.approx(0.21)
    assert label.constraint_level is ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY


def test_forward_label_does_not_shift_past_a_missing_entry_session() -> None:
    sessions = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(7))
    rows = [_market_row(session) for session in sessions if session != sessions[1]]
    builder = ForwardReturnLabelBuilder(default_forward_5d_label_spec(), require_limit_flags=False)

    label = builder.build((SignalKey(session=sessions[0], instrument_id="A"),), sessions, rows)[0]

    assert not label.is_valid
    assert label.invalid_reason is LabelInvalidReason.ENTRY_OBSERVATION_MISSING
    assert label.entry_session == sessions[1]


@pytest.mark.parametrize(
    ("entry_changes", "exit_changes", "expected"),
    [
        ({"suspended": True}, {}, LabelInvalidReason.ENTRY_UNTRADABLE),
        ({"can_buy": False}, {}, LabelInvalidReason.ENTRY_BUY_BLOCKED),
        ({"can_buy": None}, {"can_sell": True}, LabelInvalidReason.ENTRY_LIMIT_UNKNOWN),
        ({"can_buy": True}, {"can_sell": False}, LabelInvalidReason.EXIT_SELL_BLOCKED),
    ],
)
def test_limit_aware_label_rejects_unexecutable_boundaries(entry_changes, exit_changes, expected) -> None:
    sessions = tuple(date(2024, 1, 2) + timedelta(days=index) for index in range(7))
    rows = [_market_row(session, can_buy=True, can_sell=True) for session in sessions]
    rows[1] = _market_row(sessions[1], **({"can_buy": True, "can_sell": True} | entry_changes))
    rows[6] = _market_row(sessions[6], **({"can_buy": True, "can_sell": True} | exit_changes))
    builder = ForwardReturnLabelBuilder(default_forward_5d_label_spec(), require_limit_flags=True)

    label = builder.build((SignalKey(session=sessions[0], instrument_id="A"),), sessions, rows)[0]

    assert not label.is_valid
    assert label.invalid_reason is expected


def _factor(session: date, instrument: str, value: float | None) -> RawFactorValue:
    return RawFactorValue(
        session=session,
        instrument_id=instrument,
        factor_id="golden-factor",
        factor_version="1.0.0",
        value=value,
        available_at=datetime.combine(session, datetime.min.time(), UTC) + timedelta(hours=15),
        implementation_hash="sha256:" + "a" * 64,
    )


def _label(session: date, instrument: str, value: float, *, available_days: int = 6) -> ForwardReturnLabel:
    return ForwardReturnLabel(
        signal_session=session,
        instrument_id=instrument,
        label_id="golden-label",
        label_version="1.0.0",
        value=value,
        entry_session=session + timedelta(days=1),
        exit_session=session + timedelta(days=available_days),
        entry_adjusted_price=10,
        exit_adjusted_price=10 * (1 + value),
        available_at=datetime.combine(session + timedelta(days=available_days), datetime.min.time(), UTC)
        + timedelta(hours=15),
        is_valid=True,
        invalid_reason=None,
        constraint_level=ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY,
    )


def test_ic_and_quantile_spread_match_hand_calculation() -> None:
    session = date(2024, 1, 2)
    instruments = tuple("ABCDE")
    factors = tuple(_factor(session, instrument, index) for index, instrument in enumerate(instruments, 1))
    labels = tuple(_label(session, instrument, index / 100) for index, instrument in enumerate(instruments, 1))

    evidence = evaluate_basic_factor(factors, labels)

    assert evidence.mean_pearson_ic == pytest.approx(1)
    assert evidence.mean_rank_ic == pytest.approx(1)
    assert evidence.raw_q_high_minus_low == pytest.approx(0.04)
    assert evidence.paired_observations == 5


def test_reverse_and_shuffled_controls_do_not_look_positive() -> None:
    sessions = (date(2024, 1, 2), date(2024, 1, 3))
    instruments = tuple("ABCDE")
    factors = tuple(
        _factor(session, instrument, index) for session in sessions for index, instrument in enumerate(instruments, 1)
    )
    labels = tuple(
        _label(session, instrument, label / 100)
        for session, values in zip(sessions, ((5, 4, 3, 2, 1), (1, 2, 3, 4, 5)), strict=True)
        for instrument, label in zip(instruments, values, strict=True)
    )

    evidence = evaluate_basic_factor(factors, labels)

    assert evidence.daily[0].rank_ic == pytest.approx(-1)
    assert evidence.daily[1].rank_ic == pytest.approx(1)
    assert evidence.mean_rank_ic == pytest.approx(0)


def test_evaluator_rejects_a_label_that_is_not_forward() -> None:
    session = date(2024, 1, 2)
    factor = _factor(session, "A", 1)
    label = _label(session, "A", 0.1, available_days=0)

    with pytest.raises(IntegrityViolation, match="LABEL_NOT_FORWARD"):
        evaluate_basic_factor((factor,), (label,))


def test_coverage_keeps_missing_factor_rows_in_denominator() -> None:
    session = date(2024, 1, 2)
    factors = (_factor(session, "A", 1), _factor(session, "B", None))
    labels = (_label(session, "A", 0.1), _label(session, "B", 0.2))

    evidence = evaluate_basic_factor(factors, labels)

    assert evidence.mean_coverage == pytest.approx(0.5)
    assert evidence.paired_observations == 1
    assert evidence.mean_pearson_ic is None
