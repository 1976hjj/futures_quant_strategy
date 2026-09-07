from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from alpha_research_os.evaluation import forward_return_label_spec
from scripts.run_m4_1_evidence import _label_sql
from scripts.run_m4_6_execution import _selected_sql


@pytest.mark.parametrize("holding_sessions", [5, 10, 20, 30])
def test_forward_label_contract_tracks_selected_holding_period(holding_sessions: int) -> None:
    spec = forward_return_label_spec(holding_sessions)

    assert spec.horizon_sessions == holding_sessions
    assert spec.entry.session_offset == 1
    assert spec.exit.session_offset == holding_sessions + 1
    assert f"t+{holding_sessions + 1}" in spec.expression.formula


def test_unsupported_holding_period_is_rejected() -> None:
    with pytest.raises(ValueError, match="supported holding periods"):
        forward_return_label_spec(7)


def test_m4_1_and_m4_6_queries_use_dynamic_exit_offset() -> None:
    m4_1_sql = _label_sql(
        Path("scores.parquet"),
        Path("labels.parquet"),
        "label-release",
        "label-id",
        "1.0.0",
        date(2020, 1, 1),
        date(2021, 1, 1),
        31,
    )
    m4_6_sql = _selected_sql(Path("scores.parquet"), date(2020, 1, 1), date(2021, 1, 1), 0.2, 30)

    assert "session_number+31" in m4_1_sql
    assert "INTERVAL 74 DAYS" in m4_1_sql
    assert "session_number+31" in m4_6_sql
    assert "INTERVAL 74 DAYS" in m4_6_sql
