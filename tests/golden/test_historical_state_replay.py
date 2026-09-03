from __future__ import annotations

from datetime import datetime

from alpha_research_os.data.pit import select_effective_as_of
from alpha_research_os.kernel.specs import DataDomain


def test_historical_st_status_replays_without_current_state_backfill(m2_records) -> None:
    statuses = tuple(record for record in m2_records if record.record_type is DataDomain.SECURITY_STATUS)

    april = select_effective_as_of(
        statuses,
        event_at=datetime.fromisoformat("2024-04-29T15:00:00+08:00"),
        signal_cutoff=datetime.fromisoformat("2024-04-29T15:00:00+08:00"),
    )
    june = select_effective_as_of(
        statuses,
        event_at=datetime.fromisoformat("2024-06-10T15:00:00+08:00"),
        signal_cutoff=datetime.fromisoformat("2024-06-10T15:00:00+08:00"),
    )

    assert len(april) == len(june) == 1
    assert april[0].value_map()["is_st"] is False
    assert june[0].value_map()["is_st"] is True


def test_delisted_security_remains_in_historical_universe(m2_records) -> None:
    universe = tuple(record for record in m2_records if record.record_type is DataDomain.UNIVERSE)

    historical = select_effective_as_of(
        universe,
        event_at=datetime.fromisoformat("2024-04-29T15:00:00+08:00"),
        signal_cutoff=datetime.fromisoformat("2024-04-29T15:00:00+08:00"),
    )
    after_delisting = select_effective_as_of(
        universe,
        event_at=datetime.fromisoformat("2024-07-01T15:00:00+08:00"),
        signal_cutoff=datetime.fromisoformat("2024-07-01T15:00:00+08:00"),
    )

    assert {record.instrument_id for record in historical} == {
        "CN-EQ-000001",
        "CN-EQ-DELISTED-001",
    }
    assert {record.instrument_id for record in after_delisting} == {"CN-EQ-000001"}
