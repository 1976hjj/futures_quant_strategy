from __future__ import annotations

import json
from datetime import date, datetime

from alpha_research_os.data.providers.tushare import TushareProvider
from scripts.backfill_tushare_corporate_actions import FIELDS, backfill

RETRIEVED_AT = datetime.fromisoformat("2026-09-01T18:00:00+08:00")


class _DividendTransport:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, payload: bytes, *, timeout: float) -> bytes:
        request = json.loads(payload)
        self.calls.append(request)
        code = request["params"]["ts_code"]
        row = {
            "ts_code": code,
            "end_date": "20231231",
            "ann_date": "20240315",
            "div_proc": "实施",
            "stk_div": 0.1,
            "stk_bo_rate": 0.0,
            "stk_co_rate": 0.1,
            "cash_div": 0.18,
            "cash_div_tax": 0.2,
            "record_date": "20240610",
            "ex_date": "20240611",
            "pay_date": "20240611",
            "div_listdate": "20240611",
            "imp_ann_date": "20240603",
            "base_date": "20231231",
            "base_share": 1000.0,
        }
        items = [[row.get(field) for field in FIELDS]]
        return json.dumps({"code": 0, "msg": None, "data": {"fields": list(FIELDS), "items": items}}).encode()


def test_corporate_action_backfill_is_per_security_checkpointed_and_secret_free(tmp_path) -> None:
    transport = _DividendTransport()
    provider = TushareProvider(
        token="corporate-action-test-secret",
        api_base_url="https://gateway.example.invalid/",
        transport=transport,
        clock=lambda: RETRIEVED_AT,
    )
    archive = tmp_path / "corporate-actions"
    arguments = {
        "provider": provider,
        "start": date(1990, 12, 19),
        "end": date(2026, 9, 1),
        "output": archive,
        "codes": ("600000.SH", "000001.SZ"),
        "min_free_gb": 0,
        "sleep_seconds": 0,
    }

    first = backfill(**arguments)
    assert first["totals"]["dividend"] == {"partitions": 2, "rows": 2}
    assert [call["params"] for call in transport.calls] == [
        {"ts_code": "000001.SZ"},
        {"ts_code": "600000.SH"},
    ]
    assert all(
        b"corporate-action-test-secret" not in path.read_bytes()
        for path in archive.rglob("*")
        if path.is_file()
    )

    second = backfill(**arguments)
    assert second["fetched_this_run"] == 0
    assert second["skipped_this_run"] == 2
    assert len(transport.calls) == 2
