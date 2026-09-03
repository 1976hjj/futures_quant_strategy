"""Deterministic time-series inference used by the M4.3 evidence stage."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True)
class HACMeanTest:
    observations: int
    mean: float | None
    standard_error: float | None
    z_statistic: float | None
    p_value_two_sided: float | None
    max_lag: int


@dataclass(frozen=True, slots=True)
class MovingBlockBootstrap:
    observations: int
    block_length: int
    resamples: int
    seed: int
    p_value_two_sided: float | None
    confidence_lower: float | None
    confidence_upper: float | None


@dataclass(frozen=True, slots=True)
class StabilitySegment:
    segment: int
    start_session: date
    end_session: date
    observations: int
    mean: float


@dataclass(frozen=True, slots=True)
class StabilityDiagnostic:
    full_mean: float | None
    same_sign_fraction: float | None
    worst_segment_mean: float | None
    segment_range: float | None
    segments: tuple[StabilitySegment, ...]


def _finite_values(values: list[float | None]) -> list[float]:
    return [float(value) for value in values if value is not None and math.isfinite(value)]


def newey_west_mean_test(values: list[float | None], *, max_lag: int) -> HACMeanTest:
    """Test a zero mean with Bartlett-kernel Newey-West standard errors.

    The normal tail is reported deliberately: it is a transparent asymptotic
    diagnostic and is paired with the finite-sample moving-block bootstrap.
    """

    present = _finite_values(values)
    n = len(present)
    if max_lag < 0:
        raise ValueError("max_lag must be non-negative")
    if n == 0:
        return HACMeanTest(0, None, None, None, None, max_lag)
    mean = sum(present) / n
    centered = [value - mean for value in present]
    lag = min(max_lag, n - 1)
    long_run_variance = sum(value * value for value in centered) / n
    for offset in range(1, lag + 1):
        covariance = sum(centered[index] * centered[index - offset] for index in range(offset, n)) / n
        weight = 1 - offset / (lag + 1)
        long_run_variance += 2 * weight * covariance
    long_run_variance = max(0.0, long_run_variance)
    standard_error = math.sqrt(long_run_variance / n)
    if standard_error == 0:
        statistic = 0.0 if mean == 0 else math.copysign(math.inf, mean)
        p_value = 1.0 if mean == 0 else 0.0
    else:
        statistic = mean / standard_error
        p_value = math.erfc(abs(statistic) / math.sqrt(2))
    return HACMeanTest(n, mean, standard_error, statistic, p_value, max_lag)


def _percentile(sorted_values: list[float], probability: float) -> float:
    if not 0 <= probability <= 1:
        raise ValueError("probability must be within [0, 1]")
    if len(sorted_values) == 1:
        return sorted_values[0]
    position = probability * (len(sorted_values) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    weight = position - lower
    return sorted_values[lower] * (1 - weight) + sorted_values[upper] * weight


def moving_block_bootstrap_mean(
    values: list[float | None],
    *,
    block_length: int,
    resamples: int,
    seed: int,
    confidence_level: float = 0.95,
) -> MovingBlockBootstrap:
    """Circular moving-block bootstrap for the mean with a centered-null p-value."""

    present = _finite_values(values)
    n = len(present)
    if block_length < 1:
        raise ValueError("block_length must be positive")
    if resamples < 1:
        raise ValueError("resamples must be positive")
    if not 0 < confidence_level < 1:
        raise ValueError("confidence_level must be between zero and one")
    if n == 0:
        return MovingBlockBootstrap(0, block_length, resamples, seed, None, None, None)
    rng = random.Random(seed)
    observed = sum(present) / n
    bootstrap_means: list[float] = []
    extreme = 0
    blocks_needed = math.ceil(n / block_length)
    for _ in range(resamples):
        total = 0.0
        taken = 0
        for _ in range(blocks_needed):
            start = rng.randrange(n)
            for offset in range(block_length):
                if taken == n:
                    break
                total += present[(start + offset) % n]
                taken += 1
        replicate = total / n
        bootstrap_means.append(replicate)
        if abs(replicate - observed) >= abs(observed):
            extreme += 1
    bootstrap_means.sort()
    tail = (1 - confidence_level) / 2
    return MovingBlockBootstrap(
        observations=n,
        block_length=block_length,
        resamples=resamples,
        seed=seed,
        p_value_two_sided=(extreme + 1) / (resamples + 1),
        confidence_lower=_percentile(bootstrap_means, tail),
        confidence_upper=_percentile(bootstrap_means, 1 - tail),
    )


def stability_diagnostic(
    observations: list[tuple[date, float | None]], *, segment_count: int
) -> StabilityDiagnostic:
    """Split an ordered series into fixed chronological segments without fitting cut points."""

    if segment_count < 2:
        raise ValueError("segment_count must be at least two")
    present = [(session, float(value)) for session, value in observations if value is not None and math.isfinite(value)]
    if not present:
        return StabilityDiagnostic(None, None, None, None, ())
    ordered = sorted(present)
    segments: list[StabilitySegment] = []
    for index in range(segment_count):
        start = index * len(ordered) // segment_count
        end = (index + 1) * len(ordered) // segment_count
        chunk = ordered[start:end]
        if not chunk:
            continue
        values = [item[1] for item in chunk]
        segments.append(
            StabilitySegment(
                segment=index + 1,
                start_session=chunk[0][0],
                end_session=chunk[-1][0],
                observations=len(chunk),
                mean=sum(values) / len(values),
            )
        )
    full_mean = sum(item[1] for item in ordered) / len(ordered)
    if full_mean == 0:
        same_sign = sum(segment.mean == 0 for segment in segments) / len(segments)
    else:
        same_sign = sum(segment.mean * full_mean > 0 for segment in segments) / len(segments)
    segment_means = [segment.mean for segment in segments]
    return StabilityDiagnostic(
        full_mean=full_mean,
        same_sign_fraction=same_sign,
        worst_segment_mean=min(segment_means) if full_mean >= 0 else max(segment_means),
        segment_range=max(segment_means) - min(segment_means),
        segments=tuple(segments),
    )


def benjamini_hochberg(p_values: list[float | None]) -> list[float | None]:
    """Return monotone BH adjusted p-values while preserving input positions."""

    indexed = [(index, float(value)) for index, value in enumerate(p_values) if value is not None]
    if any(not 0 <= value <= 1 or not math.isfinite(value) for _, value in indexed):
        raise ValueError("p-values must be finite and within [0, 1]")
    ordered = sorted(indexed, key=lambda item: (item[1], item[0]))
    adjusted: list[float | None] = [None] * len(p_values)
    running = 1.0
    total = len(ordered)
    for reverse_index in range(total - 1, -1, -1):
        original_index, value = ordered[reverse_index]
        rank = reverse_index + 1
        running = min(running, value * total / rank)
        adjusted[original_index] = min(1.0, running)
    return adjusted
