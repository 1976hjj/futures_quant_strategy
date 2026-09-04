from __future__ import annotations

import numpy as np
import pytest

from alpha_research_os.evaluation import (
    average_linkage_clusters,
    hierarchical_average_linkage,
    partial_rank_metrics,
)


def test_average_linkage_is_deterministic_and_respects_cut() -> None:
    labels = ["c", "a", "b"]
    distances = {("a", "b"): 0.1, ("a", "c"): 0.8, ("b", "c"): 0.6}

    merges = hierarchical_average_linkage(labels, distances)
    clusters = average_linkage_clusters(labels, distances, threshold=0.35)

    assert merges[0].left_members == ("a",)
    assert merges[0].right_members == ("b",)
    assert merges[1].distance == pytest.approx(0.7)
    assert clusters == (("a", "b"), ("c",))


def test_partial_rank_metrics_matches_direct_residual_regression() -> None:
    rng = np.random.default_rng(7)
    control = rng.normal(size=(500, 2))
    candidate = 0.8 * control[:, 0] + rng.normal(size=500)
    label = 0.5 * candidate + 0.4 * control[:, 1] + rng.normal(size=500)
    matrix = np.column_stack((candidate, label, control))
    correlation = np.corrcoef(matrix, rowvar=False)

    result = partial_rank_metrics(correlation, ridge=1e-10)
    design = np.column_stack((np.ones(len(control)), control))
    candidate_residual = candidate - design @ np.linalg.lstsq(design, candidate, rcond=None)[0]
    label_residual = label - design @ np.linalg.lstsq(design, label, rcond=None)[0]
    expected_partial = np.corrcoef(candidate_residual, label_residual)[0, 1]

    assert result.control_count == 2
    assert result.orthogonal_rank_ic == pytest.approx(expected_partial, abs=1e-9)
    assert result.full_r_squared >= result.baseline_r_squared
    assert result.incremental_r_squared == pytest.approx(result.full_r_squared - result.baseline_r_squared)


def test_partial_rank_metrics_without_controls_reduces_to_raw_correlation() -> None:
    result = partial_rank_metrics(np.array([[1.0, -0.25], [-0.25, 1.0]]))

    assert result.conditional_rank_ic == -0.25
    assert result.orthogonal_rank_ic == -0.25
    assert result.incremental_r_squared == pytest.approx(0.0625)


def test_clustering_rejects_missing_pair_distance() -> None:
    with pytest.raises(ValueError, match="missing distance"):
        average_linkage_clusters(["a", "b"], {}, threshold=0.5)
