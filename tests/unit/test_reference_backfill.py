from __future__ import annotations

import json
from datetime import datetime

import duckdb

from alpha_research_os.data.providers.tushare import TushareProvider
from scripts.audit_reference_warehouse import audit
from scripts.backfill_tushare_reference import backfill
from scripts.build_reference_warehouse import build

RETRIEVED_AT = datetime.fromisoformat("2026-09-01T18:00:00+08:00")


class _ReferenceTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, payload: bytes, *, timeout: float) -> bytes:
        request = json.loads(payload)
        self.calls.append(request)
        api_name = request["api_name"]
        params = request["params"]
        fields = request["fields"].split(",")
        rows: list[dict[str, object]] = []
        if api_name == "trade_cal":
            rows = [
                {"exchange": "SSE", "cal_date": "19991231", "is_open": "1", "pretrade_date": "19991230"},
                {"exchange": "SSE", "cal_date": "20000103", "is_open": "1", "pretrade_date": "19991231"},
            ]
        elif api_name == "stock_basic" and params["list_status"] == "L":
            rows = [
                {
                    "ts_code": "000001.SZ",
                    "symbol": "000001",
                    "name": "平安银行",
                    "area": "深圳",
                    "industry": "银行",
                    "fullname": "平安银行股份有限公司",
                    "enname": "Ping An Bank",
                    "cnspell": "payh",
                    "market": "主板",
                    "exchange": "SZSE",
                    "curr_type": "CNY",
                    "list_status": "L",
                    "list_date": "19910403",
                    "delist_date": None,
                    "is_hs": "S",
                    "act_name": None,
                    "act_ent_type": None,
                }
            ]
        elif api_name == "namechange":
            if params == {} or params.get("start_date") == "19991231":
                rows = [
                    {
                        "ts_code": "000001.SZ",
                        "name": "深发展A",
                        "start_date": "19910403",
                        "end_date": "20120131",
                        "ann_date": "19991231",
                        "change_reason": "上市",
                    }
                ]
        elif api_name == "suspend_d" and params["trade_date"] == "20000103":
            rows = [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": "20000103",
                    "suspend_type": "S",
                    "suspend_timing": None,
                }
            ]
        elif api_name == "stock_st":
            rows = [
                {
                    "ts_code": "000001.SZ",
                    "name": "ST深发展",
                    "trade_date": "20000103",
                    "type": "ST",
                    "type_name": "其他风险警示",
                }
            ]
        items = [[row.get(field) for field in fields] for row in rows]
        return json.dumps({"code": 0, "msg": None, "data": {"fields": fields, "items": items}}).encode()


def test_reference_backfill_is_checkpointed_and_keeps_token_transient(tmp_path) -> None:
    transport = _ReferenceTransport()
    provider = TushareProvider(
        token="reference-test-secret",
        api_base_url="https://gateway.example.invalid/",
        transport=transport,
        clock=lambda: RETRIEVED_AT,
    )
    archive = tmp_path / "reference"

    first = backfill(
        provider=provider,
        start=datetime.fromisoformat("1999-12-31").date(),
        end=datetime.fromisoformat("2000-01-03").date(),
        output=archive,
        apis=("stock_basic", "namechange", "stock_st", "suspend_d"),
        max_sessions=None,
        min_free_gb=0,
        sleep_seconds=0,
    )

    assert len(transport.calls) == 12
    assert first["totals"]["stock_basic"]["partitions"] == 5
    assert first["totals"]["suspend_d"]["partitions"] == 2
    assert first["totals"]["stock_st"]["partitions"] == 1
    assert first["totals"]["namechange"]["rows"] == 2
    assert all(b"reference-test-secret" not in path.read_bytes() for path in archive.rglob("*") if path.is_file())

    second = backfill(
        provider=provider,
        start=datetime.fromisoformat("1999-12-31").date(),
        end=datetime.fromisoformat("2000-01-03").date(),
        output=archive,
        apis=("stock_basic", "namechange", "stock_st", "suspend_d"),
        max_sessions=None,
        min_free_gb=0,
        sleep_seconds=0,
    )

    assert len(transport.calls) == 12
    assert second["fetched_this_run"] == 0
    assert second["skipped_this_run"] == 12


def test_reference_warehouse_replays_status_without_survivorship(tmp_path) -> None:
    transport = _ReferenceTransport()
    provider = TushareProvider(
        token="reference-test-secret",
        api_base_url="https://gateway.example.invalid/",
        transport=transport,
        clock=lambda: RETRIEVED_AT,
    )
    archive = tmp_path / "reference"
    warehouse = tmp_path / "warehouse"
    backfill(
        provider=provider,
        start=datetime.fromisoformat("1999-12-31").date(),
        end=datetime.fromisoformat("2000-01-03").date(),
        output=archive,
        apis=("stock_basic", "namechange", "stock_st", "suspend_d"),
        max_sessions=None,
        min_free_gb=0,
        sleep_seconds=0,
    )
    warehouse.mkdir()
    connection = duckdb.connect(str(warehouse / "alpha_research.duckdb"))
    connection.execute("CREATE SCHEMA research")
    connection.execute(
        """
        CREATE TABLE research.market_daily (
            trade_date DATE,
            ts_code VARCHAR,
            is_tradeable_bar BOOLEAN
        )
        """
    )
    connection.execute(
        """
        INSERT INTO research.market_daily VALUES
            (DATE '1999-12-31', '000001.SZ', true),
            (DATE '2000-01-03', '000001.SZ', true)
        """
    )
    connection.close()

    summary = build(archive, warehouse)

    assert summary["row_counts"]["security_master"] == 1
    connection = duckdb.connect(str(warehouse / "alpha_research.duckdb"), read_only=True)
    states = connection.execute(
        """
        SELECT trade_date, security_name, is_st, st_source, is_suspended, eligible_for_signal
        FROM research.security_session_state
        ORDER BY trade_date
        """
    ).fetchall()
    connection.close()
    assert states == [
        (
            datetime.fromisoformat("1999-12-31").date(),
            "深发展A",
            False,
            "NAME_HISTORY",
            False,
            True,
        ),
        (
            datetime.fromisoformat("2000-01-03").date(),
            "深发展A",
            True,
            "STOCK_ST",
            True,
            False,
        ),
    ]
    assert audit(warehouse / "alpha_research.duckdb")["status"] == "PASSED"
