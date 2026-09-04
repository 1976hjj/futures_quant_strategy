"""Deterministic clustering and conditional RankIC primitives for M4.5."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class ClusterMerge:
    step: int
    left_members: tuple[str, ...]
    right_members: tuple[str, ...]
    distance: float
    merged_members: tuple[str, ...]


@dataclass(frozen=True)
class PartialRankMetrics:
    control_count: int
    conditional_rank_ic: float
    orthogonal_rank_ic: float
    baseline_r_squared: float
    full_r_squared: float
    incremental_r_squared: float
    condition_number: float


def _pair_distance(left: str, right: str, distances: Mapping[tuple[str, str], float]) -> float:
    if left == right:
        return 0.0
    key = tuple(sorted((left, right)))
    try:
        value = float(distances[key])
    except KeyError as error:
        raise ValueError(f"missing distance for pair {key}") from error
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"distance for pair {key} must be finite and within [0, 1]")
    return value


def _cluster_distance(
    left: tuple[str, ...], right: tuple[str, ...], distances: Mapping[tuple[str, str], float]
) -> float:
    values = [_pair_distance(a, b, distances) for a in left for b in right]
    return sum(values) / len(values)


def hierarchical_average_linkage(
    labels: Sequence[str], distances: Mapping[tuple[str, str], float]
) -> tuple[ClusterMerge, ...]:
    """Build a full deterministic average-linkage dendrogram without SciPy."""

    ordered = tuple(sorted(set(labels)))
    if len(ordered) != len(labels):
        raise ValueError("cluster labels must be unique")
    clusters: list[tuple[str, ...]] = [(label,) for label in ordered]
    merges: list[ClusterMerge] = []
    while len(clusters) > 1:
        candidates = []
        for left_index, left in enumerate(clusters[:-1]):
            for right_index in range(left_index + 1, len(clusters)):
                right = clusters[right_index]
                candidates.append((_cluster_distance(left, right, distances), left, right, left_index, right_index))
        distance, left, right, left_index, right_index = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        merged = tuple(sorted((*left, *right)))
        merges.append(ClusterMerge(len(merges) + 1, left, right, distance, merged))
        clusters = [cluster for index, cluster in enumerate(clusters) if index not in {left_index, right_index}]
        clusters.append(merged)
        clusters.sort()
    return tuple(merges)


def average_linkage_clusters(
    labels: Sequence[str], distances: Mapping[tuple[str, str], float], *, threshold: float
) -> tuple[tuple[str, ...], ...]:
    """Cut a deterministic average-linkage hierarchy at a frozen distance threshold."""

    if not 0 <= threshold <= 1:
        raise ValueError("cluster threshold must be within [0, 1]")
    ordered = tuple(sorted(set(labels)))
    if len(ordered) != len(labels):
        raise ValueError("cluster labels must be unique")
    clusters: list[tuple[str, ...]] = [(label,) for label in ordered]
    while len(clusters) > 1:
        candidates = []
        for left_index, left in enumerate(clusters[:-1]):
            for right_index in range(left_index + 1, len(clusters)):
                right = clusters[right_index]
                candidates.append((_cluster_distance(left, right, distances), left, right, left_index, right_index))
        distance, left, right, left_index, right_index = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        if distance > threshold:
            break
        merged = tuple(sorted((*left, *right)))
        clusters = [cluster for index, cluster in enumerate(clusters) if index not in {left_index, right_index}]
        clusters.append(merged)
        clusters.sort()
    return tuple(sorted(clusters))


def partial_rank_metrics(correlation: np.ndarray, *, ridge: float = 1e-8) -> PartialRankMetrics:
    """Return semi-partial, partial, and incremental R-squared from a rank-correlation matrix.

    Variables must be ordered as candidate, label, then controls. The same complete
    cross-section must have been used for every element of the matrix.
    """

    matrix = np.asarray(correlation, dtype=float)
    if matrix.ndim != 2 or matrix.shape[0] != matrix.shape[1] or matrix.shape[0] < 2:
        raise ValueError("correlation must be a square matrix containing candidate and label")
    if not np.all(np.isfinite(matrix)) or not np.allclose(matrix, matrix.T, atol=1e-10):
        raise ValueError("correlation matrix must be finite and symmetric")
    if ridge <= 0:
        raise ValueError("ridge must be positive")
    control_count = matrix.shape[0] - 2
    raw = float(matrix[0, 1])
    if control_count == 0:
        value = max(0.0, min(1.0, raw * raw))
        return PartialRankMetrics(0, raw, raw, 0.0, value, value, 1.0)
    controls = matrix[2:, 2:]
    regularized = controls + np.eye(control_count) * ridge
    condition_number = float(np.linalg.cond(regularized))
    candidate_controls = matrix[0, 2:]
    label_controls = matrix[1, 2:]
    candidate_beta = np.linalg.solve(regularized, candidate_controls)
    label_beta = np.linalg.solve(regularized, label_controls)
    candidate_residual_variance = max(ridge, 1.0 - float(candidate_controls @ candidate_beta))
    label_residual_variance = max(ridge, 1.0 - float(label_controls @ label_beta))
    residual_covariance = raw - float(candidate_controls @ label_beta)
    conditional = residual_covariance / math.sqrt(candidate_residual_variance)
    orthogonal = residual_covariance / math.sqrt(candidate_residual_variance * label_residual_variance)
    baseline_r_squared = max(0.0, min(1.0, float(label_controls @ label_beta)))
    incremental = max(0.0, residual_covariance * residual_covariance / candidate_residual_variance)
    full_r_squared = max(0.0, min(1.0, baseline_r_squared + incremental))
    return PartialRankMetrics(
        control_count,
        max(-1.0, min(1.0, conditional)),
        max(-1.0, min(1.0, orthogonal)),
        baseline_r_squared,
        full_r_squared,
        max(0.0, full_r_squared - baseline_r_squared),
        condition_number,
    )
