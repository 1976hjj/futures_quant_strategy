"""Forward-return label contracts and a clock-explicit reference builder."""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import date, datetime
from enum import StrEnum

from pydantic import field_validator, model_validator

from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import (
    DataDomain,
    FrozenSpec,
    LabelExpression,
    LabelSpec,
    MarketEvent,
    PricePoint,
    SignalCutoff,
    TemporalDependency,
)


class ExecutionConstraintLevel(StrEnum):
    BAR_AND_SUSPENSION_ONLY = "BAR_AND_SUSPENSION_ONLY"
    LIMIT_AWARE = "LIMIT_AWARE"


class LabelInvalidReason(StrEnum):
    SIGNAL_NOT_ELIGIBLE = "SIGNAL_NOT_ELIGIBLE"
    INSUFFICIENT_FUTURE_SESSIONS = "INSUFFICIENT_FUTURE_SESSIONS"
    ENTRY_OBSERVATION_MISSING = "ENTRY_OBSERVATION_MISSING"
    EXIT_OBSERVATION_MISSING = "EXIT_OBSERVATION_MISSING"
    ENTRY_UNTRADABLE = "ENTRY_UNTRADABLE"
    EXIT_UNTRADABLE = "EXIT_UNTRADABLE"
    ENTRY_LIMIT_UNKNOWN = "ENTRY_LIMIT_UNKNOWN"
    EXIT_LIMIT_UNKNOWN = "EXIT_LIMIT_UNKNOWN"
    ENTRY_BUY_BLOCKED = "ENTRY_BUY_BLOCKED"
    EXIT_SELL_BLOCKED = "EXIT_SELL_BLOCKED"
    ADJUSTMENT_MISSING = "ADJUSTMENT_MISSING"
    PRICE_INVALID = "PRICE_INVALID"


class SignalKey(FrozenSpec):
    session: date
    instrument_id: str


class MarketLabelRow(FrozenSpec):
    session: date
    instrument_id: str
    available_at: datetime
    open: float | None
    close: float | None
    adj_factor: float | None
    eligible_for_signal: bool
    is_suspended: bool
    is_tradeable_bar: bool
    can_buy_open: bool | None = None
    can_sell_close: bool | None = None

    @field_validator("available_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("available_at must include a timezone")
        return value

    @field_validator("open", "close", "adj_factor")
    @classmethod
    def finite_or_missing(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("market label inputs must be finite or missing")
        return value


class ForwardReturnLabel(FrozenSpec):
    signal_session: date
    instrument_id: str
    label_id: str
    label_version: str
    value: float | None
    entry_session: date | None
    exit_session: date | None
    entry_adjusted_price: float | None
    exit_adjusted_price: float | None
    available_at: datetime | None
    is_valid: bool
    invalid_reason: LabelInvalidReason | None
    constraint_level: ExecutionConstraintLevel

    @model_validator(mode="after")
    def valid_rows_are_complete(self) -> ForwardReturnLabel:
        required = (
            self.value,
            self.entry_session,
            self.exit_session,
            self.entry_adjusted_price,
            self.exit_adjusted_price,
            self.available_at,
        )
        if self.is_valid and (any(value is None for value in required) or self.invalid_reason is not None):
            raise ValueError("valid labels require complete prices, sessions, value, and availability")
        if not self.is_valid and (self.value is not None or self.invalid_reason is None):
            raise ValueError("invalid labels require one reason and no value")
        return self


def default_forward_5d_label_spec() -> LabelSpec:
    """T close signal; T+1 open entry; T+6 close exit; five-session horizon."""

    return LabelSpec(
        label_id="next-open-to-5d-close-total-return",
        label_version="1.0.0-provisional",
        signal_cutoff=SignalCutoff.POST_CLOSE,
        expression=LabelExpression(
            formula="adjusted_close[t+6] / adjusted_open[t+1] - 1",
            dependencies=(
                TemporalDependency(field="open", data_domain=DataDomain.MARKET, relative_session=1),
                TemporalDependency(field="adj_factor", data_domain=DataDomain.CORPORATE_ACTION, relative_session=1),
                TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=6),
                TemporalDependency(field="adj_factor", data_domain=DataDomain.CORPORATE_ACTION, relative_session=6),
                TemporalDependency(field="is_suspended", data_domain=DataDomain.SECURITY_STATUS, relative_session=1),
                TemporalDependency(field="is_suspended", data_domain=DataDomain.SECURITY_STATUS, relative_session=6),
            ),
        ),
        entry=PricePoint(event=MarketEvent.OPEN, session_offset=1, price_field="open"),
        exit=PricePoint(event=MarketEvent.CLOSE, session_offset=6, price_field="close"),
        horizon_sessions=5,
        overlapping=True,
        suspension_handling="invalidate_if_fixed_entry_or_exit_session_is_suspended",
        untradable_handling="bar_and_suspension_only_until_m2e_stk_limit_release",
        corporate_action_handling="entry_and_exit_price_times_point_in_time_adjustment_factor",
        delisting_return_handling="invalidate_and_audit_until_delisting_return_model_exists",
        benchmark_rule="none_raw_total_return",
        tail_truncation_rule="none_raw_label",
    )


class ForwardReturnLabelBuilder:
    """Build fixed-session labels without shifting around missing or suspended days."""

    def __init__(self, spec: LabelSpec, *, require_limit_flags: bool) -> None:
        self.spec = LabelSpec.model_validate(spec)
        self.require_limit_flags = require_limit_flags
        if self.spec.entry.event is not MarketEvent.OPEN or self.spec.exit.event is not MarketEvent.CLOSE:
            raise ValueError("M4.1 builder supports fixed OPEN entry and CLOSE exit only")
        if self.spec.entry.price_field != "open" or self.spec.exit.price_field != "close":
            raise ValueError("M4.1 builder requires open entry and close exit fields")

    @property
    def constraint_level(self) -> ExecutionConstraintLevel:
        return (
            ExecutionConstraintLevel.LIMIT_AWARE
            if self.require_limit_flags
            else ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY
        )

    def _invalid(
        self,
        key: SignalKey,
        reason: LabelInvalidReason,
        *,
        entry_session: date | None = None,
        exit_session: date | None = None,
    ) -> ForwardReturnLabel:
        return ForwardReturnLabel(
            signal_session=key.session,
            instrument_id=key.instrument_id,
            label_id=self.spec.label_id,
            label_version=self.spec.label_version,
            value=None,
            entry_session=entry_session,
            exit_session=exit_session,
            entry_adjusted_price=None,
            exit_adjusted_price=None,
            available_at=None,
            is_valid=False,
            invalid_reason=reason,
            constraint_level=self.constraint_level,
        )

    def build(
        self,
        signal_keys: Iterable[SignalKey],
        sessions: Iterable[date],
        market_rows: Iterable[MarketLabelRow],
    ) -> tuple[ForwardReturnLabel, ...]:
        ordered_sessions = tuple(sorted(set(sessions)))
        session_index = {session: index for index, session in enumerate(ordered_sessions)}
        validated_rows = tuple(MarketLabelRow.model_validate(row) for row in market_rows)
        row_by_key: dict[tuple[date, str], MarketLabelRow] = {}
        for row in validated_rows:
            key = (row.session, row.instrument_id)
            if key in row_by_key:
                raise ValueError(f"duplicate market label row: {row.instrument_id}@{row.session}")
            row_by_key[key] = row
        keys = tuple(SignalKey.model_validate(key) for key in signal_keys)
        if len({(key.session, key.instrument_id) for key in keys}) != len(keys):
            raise ValueError("duplicate signal keys")
        results: list[ForwardReturnLabel] = []
        for key in keys:
            index = session_index.get(key.session)
            signal_row = row_by_key.get((key.session, key.instrument_id))
            if signal_row is None or not signal_row.eligible_for_signal:
                results.append(self._invalid(key, LabelInvalidReason.SIGNAL_NOT_ELIGIBLE))
                continue
            if index is None or index + self.spec.exit.session_offset >= len(ordered_sessions):
                results.append(self._invalid(key, LabelInvalidReason.INSUFFICIENT_FUTURE_SESSIONS))
                continue
            entry_session = ordered_sessions[index + self.spec.entry.session_offset]
            exit_session = ordered_sessions[index + self.spec.exit.session_offset]
            entry = row_by_key.get((entry_session, key.instrument_id))
            exit_row = row_by_key.get((exit_session, key.instrument_id))
            if entry is None:
                results.append(
                    self._invalid(
                        key,
                        LabelInvalidReason.ENTRY_OBSERVATION_MISSING,
                        entry_session=entry_session,
                        exit_session=exit_session,
                    )
                )
                continue
            if exit_row is None:
                results.append(
                    self._invalid(
                        key,
                        LabelInvalidReason.EXIT_OBSERVATION_MISSING,
                        entry_session=entry_session,
                        exit_session=exit_session,
                    )
                )
                continue
            if entry.is_suspended or not entry.is_tradeable_bar:
                reason = LabelInvalidReason.ENTRY_UNTRADABLE
            elif exit_row.is_suspended or not exit_row.is_tradeable_bar:
                reason = LabelInvalidReason.EXIT_UNTRADABLE
            elif self.require_limit_flags and entry.can_buy_open is None:
                reason = LabelInvalidReason.ENTRY_LIMIT_UNKNOWN
            elif self.require_limit_flags and exit_row.can_sell_close is None:
                reason = LabelInvalidReason.EXIT_LIMIT_UNKNOWN
            elif entry.can_buy_open is False:
                reason = LabelInvalidReason.ENTRY_BUY_BLOCKED
            elif exit_row.can_sell_close is False:
                reason = LabelInvalidReason.EXIT_SELL_BLOCKED
            elif (
                entry.adj_factor is None
                or exit_row.adj_factor is None
                or entry.adj_factor <= 0
                or exit_row.adj_factor <= 0
            ):
                reason = LabelInvalidReason.ADJUSTMENT_MISSING
            elif entry.open is None or exit_row.close is None or entry.open <= 0 or exit_row.close <= 0:
                reason = LabelInvalidReason.PRICE_INVALID
            else:
                reason = None
            if reason is not None:
                results.append(self._invalid(key, reason, entry_session=entry_session, exit_session=exit_session))
                continue
            assert entry.open is not None and entry.adj_factor is not None
            assert exit_row.close is not None and exit_row.adj_factor is not None
            entry_adjusted = entry.open * entry.adj_factor
            exit_adjusted = exit_row.close * exit_row.adj_factor
            value = exit_adjusted / entry_adjusted - 1
            if not math.isfinite(value):
                raise IntegrityViolation(
                    "LABEL_NON_FINITE",
                    "valid label arithmetic produced a non-finite return",
                    rule_id="RULE-004",
                )
            results.append(
                ForwardReturnLabel(
                    signal_session=key.session,
                    instrument_id=key.instrument_id,
                    label_id=self.spec.label_id,
                    label_version=self.spec.label_version,
                    value=value,
                    entry_session=entry_session,
                    exit_session=exit_session,
                    entry_adjusted_price=entry_adjusted,
                    exit_adjusted_price=exit_adjusted,
                    available_at=exit_row.available_at,
                    is_valid=True,
                    invalid_reason=None,
                    constraint_level=self.constraint_level,
                )
            )
        return tuple(sorted(results, key=lambda item: (item.signal_session, item.instrument_id)))
