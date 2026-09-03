from __future__ import annotations

from datetime import date, timedelta

import pytest

from alpha_research_os.evaluation import (
    benjamini_hochberg,
    moving_block_bootstrap_mean,
    newey_west_mean_test,
    stability_diagnostic,
)


def test_newey_west_matches_direct_bartlett_calculation() -> None:
    values = [0.1, 0.2, -0.1, 0.3, 0.0]
    result = newey_west_mean_test(values, max_lag=2)
    mean = sum(values) / len(values)
    centered = [value - mean for value in values]
    long_run = sum(value**2 for value in centered) / len(values)
    for lag in (1, 2):
        covariance = sum(centered[index] * centered[index - lag] for index in range(lag, len(values))) / len(
            values
        )
        long_run += 2 * (1 - lag / 3) * covariance

    assert result.mean == pytest.approx(mean)
    assert result.standard_error == pytest.approx((max(0, long_run) / len(values)) ** 0.5)
    assert result.p_value_two_sided is not None


def test_moving_block_bootstrap_is_seeded_and_detects_constant_nonzero_mean() -> None:
    first = moving_block_bootstrap_mean([1.0] * 20, block_length=5, resamples=999, seed=7)
    second = moving_block_bootstrap_mean([1.0] * 20, block_length=5, resamples=999, seed=7)

    assert first == second
    assert first.p_value_two_sided == pytest.approx(0.001)
    assert first.confidence_lower == pytest.approx(1.0)
    assert first.confidence_upper == pytest.approx(1.0)


def test_benjamini_hochberg_matches_hand_calculated_example_and_keeps_missing() -> None:
    adjusted = benjamini_hochberg([0.01, 0.04, None, 0.03])
    assert adjusted == pytest.approx([0.03, 0.04, None, 0.04])


def test_stability_uses_fixed_chronological_segments() -> None:
    start = date(2024, 1, 1)
    observations = [(start + timedelta(days=index), value) for index, value in enumerate([1, 1, -1, -1, 2, 2])]

    result = stability_diagnostic(observations, segment_count=3)

    assert [segment.mean for segment in result.segments] == [1.0, -1.0, 2.0]
    assert result.full_mean == pytest.approx(2 / 3)
    assert result.same_sign_fraction == pytest.approx(2 / 3)
    assert result.worst_segment_mean == -1.0
    assert result.segment_range == 3.0
