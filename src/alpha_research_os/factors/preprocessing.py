"""Deterministic cross-sectional preprocessing with explicit missing-value behavior."""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True, slots=True)
class CrossSectionRow:
    instrument_id: str
    value: float | None
    industry: str | None = None
    log_size: float | None = None


@dataclass(frozen=True, slots=True)
class ProcessedCrossSectionRow:
    instrument_id: str
    raw: float | None
    winsorized: float | None
    standardized: float | None
    neutralized: float | None


def _finite(value: float | None) -> bool:
    return value is not None and math.isfinite(value)


def _median_absolute_deviation(values: list[float]) -> tuple[float, float]:
    center = median(values)
    return center, median(abs(value - center) for value in values)


def _zscore(values: list[float | None]) -> list[float | None]:
    present = [value for value in values if value is not None]
    if len(present) < 2:
        return [None for _ in values]
    mean = sum(present) / len(present)
    variance = sum((value - mean) ** 2 for value in present) / len(present)
    if variance <= 0:
        return [None if value is not None else None for value in values]
    scale = math.sqrt(variance)
    return [None if value is None else (value - mean) / scale for value in values]


def process_cross_section(
    rows: list[CrossSectionRow],
    *,
    mad_scale: float = 5.0,
    neutralize_industry: bool = True,
    neutralize_log_size: bool = True,
) -> tuple[ProcessedCrossSectionRow, ...]:
    """MAD-winsorize, z-score, then regress out PIT industry fixed effects and log size."""

    if mad_scale <= 0:
        raise ValueError("mad_scale must be positive")
    if len({row.instrument_id for row in rows}) != len(rows):
        raise ValueError("cross-section contains duplicate instruments")
    finite_values = [float(row.value) for row in rows if _finite(row.value)]
    if not finite_values:
        return tuple(ProcessedCrossSectionRow(row.instrument_id, row.value, None, None, None) for row in rows)
    center, mad = _median_absolute_deviation(finite_values)
    lower, upper = center - mad_scale * mad, center + mad_scale * mad
    winsorized = [None if not _finite(row.value) else min(upper, max(lower, float(row.value))) for row in rows]
    standardized = _zscore(winsorized)

    industry_means: dict[str, float] = {}
    if neutralize_industry:
        grouped: dict[str, list[float]] = {}
        for row, value in zip(rows, standardized, strict=True):
            if value is not None and row.industry is not None:
                grouped.setdefault(row.industry, []).append(value)
        industry_means = {key: sum(values) / len(values) for key, values in grouped.items()}
    demeaned_y = [
        None
        if value is None or (neutralize_industry and row.industry is None)
        else value - industry_means.get(row.industry, 0.0)
        for row, value in zip(rows, standardized, strict=True)
    ]

    residuals = list(demeaned_y)
    if neutralize_log_size:
        groups: dict[str | None, list[float]] = {}
        for row in rows:
            if _finite(row.log_size) and (not neutralize_industry or row.industry is not None):
                groups.setdefault(row.industry if neutralize_industry else None, []).append(float(row.log_size))
        size_means = {key: sum(values) / len(values) for key, values in groups.items()}
        centered_x: list[float | None] = []
        for row in rows:
            group = row.industry if neutralize_industry else None
            if not _finite(row.log_size) or group not in size_means:
                centered_x.append(None)
            else:
                centered_x.append(float(row.log_size) - size_means[group])
        pairs = [(x, y) for x, y in zip(centered_x, demeaned_y, strict=True) if x is not None and y is not None]
        denominator = sum(x * x for x, _ in pairs)
        beta = sum(x * y for x, y in pairs) / denominator if denominator > 0 else 0.0
        residuals = [
            None if x is None or y is None else y - beta * x for x, y in zip(centered_x, demeaned_y, strict=True)
        ]
    neutralized = _zscore(residuals)
    return tuple(
        ProcessedCrossSectionRow(
            instrument_id=row.instrument_id,
            raw=row.value,
            winsorized=win,
            standardized=std,
            neutralized=neutral,
        )
        for row, win, std, neutral in zip(rows, winsorized, standardized, neutralized, strict=True)
    )
