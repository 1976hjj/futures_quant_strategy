from __future__ import annotations

import json
from datetime import date

from alpha_research_os.data.providers.tushare import TushareProvider
from alpha_research_os.kernel.specs import DataDomain
from scripts.backfill_tushare_m2e import (
    Task,
    _adaptive_split,
    _expand_adaptive_splits,
    _offset_parameter_rejected,
    _request,
    _tasks,
    backfill,
    _new_checkpoint,
)


def test_m2e_universe_request_uses_domain_contract_field() -> None:
    task = Task("index_basic", "market=SSE", date(2024, 1, 1), date(2024, 1, 1), ("market=SSE",))
    request = _request(task, 0)
    assert request.data_domain is DataDomain.UNIVERSE
    assert request.fields == ("index_code",)
    assert "_all_fields=true" in request.parameters


def test_m2e_limit_only_update_extends_existing_archive(monkeypatch, tmp_path) -> None:
    class Transport:
        def post(self, url: str, payload: bytes, *, timeout: float) -> bytes:
            request = json.loads(payload)
            fields = ["ts_code", "trade_date", "up_limit", "down_limit"]
            row = ["000001.SZ", "20260902", "11", "9"]
            return json.dumps({"code": 0, "data": {"fields": fields, "items": [row]}}).encode()

    from scripts import backfill_tushare_m2e as module
    monkeypatch.setattr(module, "_load_inputs", lambda *_: (["20260902"], [], []))
    monkeypatch.setattr(module, "_tasks", lambda *_: [
        Task("stk_limit", "20260902", date(2026, 9, 2), date(2026, 9, 2), page_size=5800)
    ])
    output = tmp_path / "m2e"
    output.mkdir()
    state = _new_checkpoint("https://gateway.example.invalid/", date(1990, 12, 31), date(2026, 9, 1), 0)
    (output / "checkpoint.json").write_text(json.dumps(state), encoding="utf-8")
    provider = TushareProvider(token="test-secret", api_base_url="https://gateway.example.invalid/",
                               transport=Transport())
    result = backfill(provider=provider, start=date(1990, 12, 31), end=date(2026, 9, 2),
                      output=output, reference=tmp_path, financial=tmp_path, database=tmp_path / "db",
                      min_free_gb=0, sleep_seconds=0, apis=("stk_limit",))
    assert result["fetched_this_run"] == 1
    assert json.loads((output / "checkpoint.json").read_bytes())["coverage"]["end"] == "2026-09-02"


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


def test_m2e_fetches_current_and_historical_industry_memberships() -> None:
    tasks = _tasks(
        date(2024, 1, 1),
        date(2024, 1, 2),
        sessions=["20240102"],
        periods=[],
        securities=["600036.SH"],
    )
    memberships = [task for task in tasks if task.api == "index_member_all"]

    assert [(task.key, task.params) for task in memberships] == [
        ("SW2021:current", ("is_new=Y",)),
        ("SW2021:all-history", ("is_new=N",)),
    ]


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


def test_m2e_share_float_high_offset_falls_back_to_daily_partitions() -> None:
    parent = Task(
        "share_float",
        "201701",
        date(2017, 1, 1),
        date(2017, 1, 31),
        ("start_date=20170101", "end_date=20170131"),
        page_size=6000,
    )

    children = _adaptive_split(parent, ["600036.SH"])

    assert len(children) == 31
    assert children[0].key == "201701:day=20170101"
    assert children[-1].params == ("start_date=20170131", "end_date=20170131")
    assert _offset_parameter_rejected(RuntimeError("code=50101 msg=参数校验失败, offset"))


def test_m2e_recorded_daily_split_can_fall_back_to_instruments() -> None:
    parent = Task(
        "share_float",
        "201701",
        date(2017, 1, 1),
        date(2017, 1, 1),
        ("start_date=20170101", "end_date=20170101"),
        page_size=6000,
    )
    splits = {"share_float": {"201701": {"reason": "test"}}}

    expanded = _expand_adaptive_splits([parent], splits, ["600036.SH", "000001.SZ"])

    assert [task.instrument for task in expanded] == ["600036.SH", "000001.SZ"]
