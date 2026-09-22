from __future__ import annotations

from scripts.sync_strategy_benchmark import _merge_incremental


def test_incremental_benchmark_merge_preserves_history_and_updates_overlap() -> None:
    existing = {
        "source": {"provider": "Eastmoney", "urls": ["old"]},
        "coverage": {"start": "2026-09-01", "end": "2026-09-02"},
        "daily": [{"session": "2026-09-01", "close": 4000.0}, {"session": "2026-09-02", "close": 4010.0}],
    }
    fresh = {
        "source": {"provider": "Eastmoney", "urls": ["new"]},
        "coverage": {"start": "2026-09-02", "end": "2026-09-03"},
        "daily": [{"session": "2026-09-02", "close": 4011.0}, {"session": "2026-09-03", "close": 4020.0}],
    }

    merged = _merge_incremental(existing, fresh)

    assert merged["coverage"] == {"start": "2026-09-01", "end": "2026-09-03"}
    assert merged["daily"] == [
        {"session": "2026-09-01", "close": 4000.0},
        {"session": "2026-09-02", "close": 4011.0},
        {"session": "2026-09-03", "close": 4020.0},
    ]
    assert merged["source"]["urls"] == ["old", "new"]
