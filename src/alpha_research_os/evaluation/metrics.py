"""Deterministic M4.1 cross-sectional factor diagnostics."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from datetime import date

from pydantic import Field

from alpha_research_os.factors.runtime import RawFactorValue
from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import FrozenSpec

from .labels import ForwardReturnLabel


class QuantileReturn(FrozenSpec):
    quantile: int = Field(ge=1)
    count: int = Field(ge=1)
    mean_return: float


class DailyFactorEvidence(FrozenSpec):
    session: date
    universe_count: int = Field(ge=0)
    factor_present_count: int = Field(ge=0)
    valid_label_count: int = Field(ge=0)
    paired_count: int = Field(ge=0)
    coverage: float = Field(ge=0, le=1)
    pearson_ic: float | None
    rank_ic: float | None
    quantile_returns: tuple[QuantileReturn, ...]


class BasicFactorEvidence(FrozenSpec):
    factor_id: str
    factor_version: str
    label_id: str
    label_version: str
    quantile_count: int = Field(ge=2)
    sessions: int = Field(ge=0)
    paired_observations: int = Field(ge=0)
    mean_coverage: float | None
    mean_pearson_ic: float | None
    mean_rank_ic: float | None
    raw_q_high_minus_low: float | None
    top_quantile_turnover: float | None
    bottom_quantile_turnover: float | None
    daily: tuple[DailyFactorEvidence, ...]


def _mean(values: Iterable[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _pearson(left: list[float], right: list[float]) -> float | None:
    if len(left) < 3 or len(left) != len(right):
        return None
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    denominator = math.sqrt(
        sum(value * value for value in left_centered) * sum(value * value for value in right_centered)
    )
    if denominator == 0:
        return None
    result = sum(a * b for a, b in zip(left_centered, right_centered, strict=True)) / denominator
    return max(-1.0, min(1.0, result))


def _average_ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        average = ((index + 1) + end) / 2
        for original_index, _ in ordered[index:end]:
            ranks[original_index] = average
        index = end
    return ranks


def _quantile_assignments(
    pairs: list[tuple[str, float, float]], quantile_count: int
) -> tuple[dict[str, int], tuple[QuantileReturn, ...]]:
    factor_ranks = _average_ranks([item[1] for item in pairs])
    assignments: dict[str, int] = {}
    returns: dict[int, list[float]] = defaultdict(list)
    for (instrument_id, _, label), rank in zip(pairs, factor_ranks, strict=True):
        quantile = min(quantile_count, int((rank - 1) * quantile_count / len(pairs)) + 1)
        assignments[instrument_id] = quantile
        returns[quantile].append(label)
    summaries = tuple(
        QuantileReturn(quantile=quantile, count=len(values), mean_return=sum(values) / len(values))
        for quantile, values in sorted(returns.items())
    )
    return assignments, summaries


def _turnover(previous: set[str], current: set[str]) -> float | None:
    if not previous or not current:
        return None
    retained_weight = len(previous.intersection(current)) * min(1 / len(previous), 1 / len(current))
    return 1 - retained_weight


def evaluate_basic_factor(
    factor_values: Iterable[RawFactorValue],
    labels: Iterable[ForwardReturnLabel],
    *,
    quantile_count: int = 5,
) -> BasicFactorEvidence:
    if quantile_count < 2:
        raise ValueError("quantile_count must be at least two")
    factors = tuple(RawFactorValue.model_validate(item) for item in factor_values)
    label_rows = tuple(ForwardReturnLabel.model_validate(item) for item in labels)
    factor_identities = {(item.factor_id, item.factor_version) for item in factors}
    label_identities = {(item.label_id, item.label_version) for item in label_rows}
    if len(factor_identities) != 1 or len(label_identities) != 1:
        raise ValueError("one evaluation call must contain exactly one factor and one label identity")
    factor_keys = [(item.session, item.instrument_id) for item in factors]
    label_keys = [(item.signal_session, item.instrument_id) for item in label_rows]
    if len(factor_keys) != len(set(factor_keys)) or len(label_keys) != len(set(label_keys)):
        raise ValueError("factor and label inputs must have unique session/instrument keys")
    labels_by_key = {(item.signal_session, item.instrument_id): item for item in label_rows}
    by_session: dict[date, list[RawFactorValue]] = defaultdict(list)
    for factor in factors:
        by_session[factor.session].append(factor)

    daily: list[DailyFactorEvidence] = []
    assignments_by_session: dict[date, dict[str, int]] = {}
    for session in sorted(by_session):
        session_factors = by_session[session]
        pairs: list[tuple[str, float, float]] = []
        valid_label_count = 0
        for factor in session_factors:
            label = labels_by_key.get((factor.session, factor.instrument_id))
            if label is not None and label.is_valid:
                valid_label_count += 1
            if factor.value is None or label is None or not label.is_valid or label.value is None:
                continue
            if label.available_at is None or label.available_at <= factor.available_at:
                raise IntegrityViolation(
                    "LABEL_NOT_FORWARD",
                    "evaluation received a label available no later than its factor",
                    rule_id="RULE-005",
                    context={"instrument_id": factor.instrument_id, "session": factor.session.isoformat()},
                )
            pairs.append((factor.instrument_id, factor.value, label.value))
        factor_values_present = [item.value for item in session_factors if item.value is not None]
        left = [item[1] for item in pairs]
        right = [item[2] for item in pairs]
        if pairs:
            assignments, quantile_returns = _quantile_assignments(pairs, quantile_count)
        else:
            assignments, quantile_returns = {}, ()
        assignments_by_session[session] = assignments
        daily.append(
            DailyFactorEvidence(
                session=session,
                universe_count=len(session_factors),
                factor_present_count=len(factor_values_present),
                valid_label_count=valid_label_count,
                paired_count=len(pairs),
                coverage=len(factor_values_present) / len(session_factors) if session_factors else 0,
                pearson_ic=_pearson(left, right),
                rank_ic=_pearson(_average_ranks(left), _average_ranks(right)) if pairs else None,
                quantile_returns=quantile_returns,
            )
        )

    quantile_daily: dict[int, list[float]] = defaultdict(list)
    for item in daily:
        for quantile in item.quantile_returns:
            quantile_daily[quantile.quantile].append(quantile.mean_return)
    high = _mean(quantile_daily.get(quantile_count, ()))
    low = _mean(quantile_daily.get(1, ()))
    top_turnover: list[float | None] = []
    bottom_turnover: list[float | None] = []
    ordered_sessions = sorted(assignments_by_session)
    for previous_session, current_session in zip(ordered_sessions[:-1], ordered_sessions[1:], strict=True):
        previous = assignments_by_session[previous_session]
        current = assignments_by_session[current_session]
        top_turnover.append(
            _turnover(
                {key for key, value in previous.items() if value == quantile_count},
                {key for key, value in current.items() if value == quantile_count},
            )
        )
        bottom_turnover.append(
            _turnover(
                {key for key, value in previous.items() if value == 1},
                {key for key, value in current.items() if value == 1},
            )
        )
    factor_id, factor_version = next(iter(factor_identities))
    label_id, label_version = next(iter(label_identities))
    return BasicFactorEvidence(
        factor_id=factor_id,
        factor_version=factor_version,
        label_id=label_id,
        label_version=label_version,
        quantile_count=quantile_count,
        sessions=len(daily),
        paired_observations=sum(item.paired_count for item in daily),
        mean_coverage=_mean(item.coverage for item in daily),
        mean_pearson_ic=_mean(item.pearson_ic for item in daily),
        mean_rank_ic=_mean(item.rank_ic for item in daily),
        raw_q_high_minus_low=None if high is None or low is None else high - low,
        top_quantile_turnover=_mean(top_turnover),
        bottom_quantile_turnover=_mean(bottom_turnover),
        daily=tuple(daily),
    )
