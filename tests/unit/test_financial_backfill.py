from __future__ import annotations

import json
from datetime import date, datetime

from alpha_research_os.data.providers.tushare import TushareProvider
from scripts.backfill_tushare_financials import backfill, financial_periods

RETRIEVED_AT = datetime.fromisoformat("2026-09-01T18:00:00+08:00")


class _FinancialTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, payload: bytes, *, timeout: float) -> bytes:
        request = json.loads(payload)
        self.calls.append(request)
        assert request["fields"] == ""
        offset = int(request["params"]["offset"])
        rows = [
            ["000001.SZ", "20240430", "20240430", "20231231", "1", 100.0],
            ["600000.SH", "20240427", "20240427", "20231231", "1", 200.0],
        ]
        if offset == 2:
            rows = [["000002.SZ", "20240430", "20240430", "20231231", "1", 300.0]]
        fields = ["ts_code", "ann_date", "f_ann_date", "end_date", "update_flag", "total_revenue"]
        return json.dumps({"code": 0, "msg": None, "data": {"fields": fields, "items": rows}}).encode()


def test_financial_periods_use_only_completed_quarter_ends() -> None:
    assert financial_periods(date(2025, 12, 31), date(2026, 9, 1)) == (
        "20251231",
        "20260331",
        "20260630",
    )


def test_financial_backfill_pages_until_short_page_and_resumes(tmp_path) -> None:
    transport = _FinancialTransport()
    provider = TushareProvider(
        token="financial-test-secret",
        api_base_url="https://gateway.example.invalid/",
        transport=transport,
        clock=lambda: RETRIEVED_AT,
    )
    archive = tmp_path / "financial"
    arguments = {
        "provider": provider,
        "start": date(2023, 12, 31),
        "end": date(2023, 12, 31),
        "output": archive,
        "apis": ("income_vip",),
        "periods": ("20231231",),
        "page_size": 2,
        "min_free_gb": 0,
        "sleep_seconds": 0,
    }
    first = backfill(**arguments)
    assert first["totals"]["income_vip"] == {"partitions": 2, "periods": 1, "rows": 3}
    assert [call["params"]["offset"] for call in transport.calls] == ["0", "2"]
    assert all(
        b"financial-test-secret" not in path.read_bytes()
        for path in archive.rglob("*")
        if path.is_file()
    )

    second = backfill(**arguments)
    assert second["fetched_this_run"] == 0
    assert len(transport.calls) == 2
