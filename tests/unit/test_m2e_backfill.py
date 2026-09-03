from __future__ import annotations

from datetime import date

from alpha_research_os.data.providers.tushare import TushareProvider
from alpha_research_os.kernel.specs import DataDomain
from scripts.backfill_tushare_m2e import Task, _request, _tasks


def test_m2e_universe_request_uses_domain_contract_field() -> None:
    task = Task("index_basic", "market=SSE", date(2024, 1, 1), date(2024, 1, 1), ("market=SSE",))
    request = _request(task, 0)
    assert request.data_domain is DataDomain.UNIVERSE
    assert request.fields == ("index_code",)
    assert "_all_fields=true" in request.parameters


def test_m2e_hk_hold_stops_before_daily_disclosure_ended() -> None:
    sessions = ["20240819", "20240820", "20240821"]
    tasks = _tasks(
        date(2024, 8, 19),
        date(2024, 8, 21),
        sessions,
        periods=[],
        securities=["600036.SH"],
    )
    hk_dates = [task.key for task in tasks if task.api == "hk_hold"]
    assert hk_dates == ["20240819"]


def test_m2e_all_market_month_range_keeps_explicit_dates() -> None:
    task = Task(
        "index_weight",
        "000300.SH:202401",
        date(2024, 1, 1),
        date(2024, 1, 31),
        ("index_code=000300.SH", "start_date=20240101", "end_date=20240131"),
    )

    params = TushareProvider._build_params(
        "index_weight",
        _request(task, 0),
        {"index_code": "000300.SH", "start_date": "20240101", "end_date": "20240131"},
    )

    assert params["index_code"] == "000300.SH"
    assert params["start_date"] == "20240101"
    assert params["end_date"] == "20240131"
