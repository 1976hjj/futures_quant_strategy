from datetime import date

import pandas as pd
import pytest
from pydantic import ValidationError

from scripts import data_update
from scripts.sync_bank_industry import resolve_field


def test_industry_isolated_and_factors_rejected():
    with pytest.raises(ValidationError):
        data_update.DataUpdateRequest(source_id="industry", groups=["bank_industry", "market"])
    with pytest.raises(ValidationError, match="因子计算"):
        data_update.DataUpdateRequest(groups=["factors"])
    assert "factors" not in data_update.DataUpdateRequest().groups


def test_field_uses_update_flag_scope_and_available_date():
    frame = pd.DataFrame(
        [
            {"ann_date": "2026-03-01", "bps": 10.1, "update_flag": "0", "report_type": "1"},
            {"ann_date": "2026-03-01", "bps": 10.0, "update_flag": "1", "report_type": "1"},
            {"ann_date": "2026-03-02", "bps": 99, "update_flag": "1", "report_type": "4"},
            {"ann_date": "2026-04-01", "bps": 20, "update_flag": "1", "report_type": "1"},
        ]
    )
    assert resolve_field(frame, "bps", date(2026, 3, 31)) == (10.0, False, "2026-03-01")
    frame.loc[0, "update_flag"] = "1"
    assert resolve_field(frame, "bps", date(2026, 3, 31)) == (None, True, "2026-03-01")


def test_industry_plan_never_requires_unrelated_market(monkeypatch, tmp_path):
    today = date.today().isoformat()
    groups = [{"id": g, "name": g, "end": None, "published": False} for g in data_update.LEGACY_GROUPS]
    industry = {"id": "bank_industry", "name": "bank", "end": None, "published": False, "last_collected_at": today}
    monkeypatch.setattr(data_update, "inventory", lambda _: {"groups": groups, "industries": [industry], "factors": []})
    monkeypatch.setattr(data_update, "tushare_token", lambda _: "configured")
    result = data_update.plan(tmp_path, data_update.DataUpdateRequest(source_id="industry", groups=["bank_industry"]))
    assert result["required_unselected"] == []
    assert result["can_start"] and result["token_required"]
    assert not result["strategy_ready_if_complete"]


def test_sync_publishes_versions_and_failed_fetch_keeps_pointer(monkeypatch, tmp_path):
    import json

    import duckdb

    from scripts import sync_bank_industry as module
    from scripts import sync_eastmoney_banks

    warehouse = tmp_path / "data/warehouse"
    warehouse.mkdir(parents=True)
    (tmp_path / "config").mkdir()
    (tmp_path / "config/eastmoney_banks.json").write_text(json.dumps({"banks": [{"code": "600036.SH"}]}))
    free = duckdb.connect(str(warehouse / "eastmoney_bank.duckdb"))
    free.execute(
        "create table bank_financial_research_panel_2010_verified_all as "
        "select '600036.SH' code, '2026-06-30' report_date, 'eastmoney' source_id, 9.0 vendor_bvps"
    )
    free.close()
    monkeypatch.setattr(module, "tushare_token", lambda _: "test")
    monkeypatch.setattr(module, "_archive_endpoint", lambda *_: "https://test.example/")
    monkeypatch.setattr(
        sync_eastmoney_banks,
        "bank_inventory",
        lambda _: {"snapshot_day": date.today().isoformat(), "snapshot_through": "2026-09-01"},
    )

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.content = json.dumps(payload).encode()

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    def fetch(*_, **kwargs):
        api = kwargs["json"]["api_name"]
        if api in module.APIS[:3]:
            data = {"ts_code": "600036.SH", "trade_date": "20260630", "close": 10.0}
        else:
            data = {
                "ts_code": "600036.SH",
                "end_date": "20260630",
                "ann_date": "20260801",
                "update_flag": "1",
                "report_type": "1",
            }
            data.update({field: 10.0 for field in module.FIELDS.get(api, {})})
        return Response({"code": 0, "data": {"fields": list(data), "items": [list(data.values())]}})

    monkeypatch.setattr(module.requests, "post", fetch)
    summary = module.sync(tmp_path, date(2026, 9, 1), 2, 1)
    pointer = (warehouse / "bank_token_summary.json").read_bytes()
    with duckdb.connect(str(warehouse / "bank_token.duckdb"), read_only=True) as conn:
        assert conn.execute(
            "select selected_bvps, selected_bvps_source from bank_primary_research_panel where report_date='2026-06-30'"
        ).fetchone() == (10.0, "tushare_compatible_token")
        assert conn.execute("select count(*) from daily_complete").fetchone()[0] == 1
    assert summary["groups"]["market"]["banks"] == 1
    monkeypatch.setattr(module.requests, "post", lambda *_, **__: Response({"code": -1}))
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="source error"):
        module.sync(tmp_path, date(2026, 9, 1), 2, 1)
    assert (warehouse / "bank_token_summary.json").read_bytes() == pointer


def test_mixed_pdf_lineage_and_units_are_explicit():
    import json

    from scripts.bank_data_lineage import add_lineage

    frame = pd.DataFrame(
        [
            {
                "code": "600036.SH",
                "report_date": "2026-06-30",
                "source_id": "eastmoney",
                "vendor_bvps": 9.0,
                "net_interest_margin": 2.1,
                "tushare_bps": 10.0,
                "selected_bvps": 10.0,
                "tushare_report_shares": 100.0,
                "supplement_details": json.dumps(
                    [
                        {
                            "metric": "net_interest_margin",
                            "value": 2.1,
                            "source_id": "issuer_report_verified",
                            "source_file": "original.pdf",
                            "pdf_page": 8,
                        }
                    ]
                ),
            }
        ]
    )
    panel, records = add_lineage(frame)
    assert panel.iloc[0].net_interest_margin_source == "issuer_report_verified"
    assert panel.iloc[0].selected_bvps_source == "tushare_compatible_token"
    assert records[records.metric == "tushare_report_shares"].iloc[0].normalized_value == 100.0
    assert not records.historical_pit_verified.any()
    frame.loc[0, "net_interest_margin"] = 2.2
    with pytest.raises(ValueError, match="lineage value mismatch"):
        add_lineage(frame)


def test_dividend_event_revisions_are_not_double_counted():
    from scripts.bank_data_lineage import implemented_dividend_events

    frame = pd.DataFrame(
        [
            {
                "ts_code": "600036.SH",
                "end_date": pd.Timestamp("2025-12-31"),
                "ex_date": pd.Timestamp("2026-07-01"),
                "ann_date": pd.Timestamp("2026-03-01"),
                "imp_ann_date": pd.Timestamp("2026-06-20"),
                "retrieved_at": "2026-10-08",
                "cash_div_tax": 1.0,
                "stk_div": 0.0,
                "div_proc": stage,
            }
            for stage in ["实施", "实施", "预案"]
        ]
    )
    events, conflicts = implemented_dividend_events(frame, date(2026, 10, 8))
    assert len(events) == 1 and not conflicts
    frame.loc[1, "cash_div_tax"] = 1.1
    events, conflicts = implemented_dividend_events(frame, date(2026, 10, 8))
    assert events.empty and len(conflicts) == 1


def test_supplement_metric_name_does_not_override_selected_bps_identity():
    import json

    from scripts.bank_data_lineage import add_lineage

    frame = pd.DataFrame(
        [
            {
                "code": "601187.SH",
                "report_date": "2019-09-30",
                "source_id": "eastmoney",
                "vendor_bvps": 9.0,
                "tushare_bps": float("nan"),
                "selected_bvps": 9.0,
                "supplement_details": json.dumps(
                    [{"metric": "vendor_bvps", "value": 9.0, "source_id": "free_financial_source_reconciled"}]
                ),
            }
        ]
    )
    _, records = add_lineage(frame)
    assert not records.duplicated(["code", "report_date", "metric"]).any()
    assert set(records.metric) == {"vendor_bvps", "selected_bvps"}
