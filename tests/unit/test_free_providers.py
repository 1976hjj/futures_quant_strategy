from __future__ import annotations

import json
from datetime import datetime

import pytest

from alpha_research_os.data.audit import audit_cross_source_market
from alpha_research_os.data.contracts import FetchRequest, FieldValue, PITGrade
from alpha_research_os.data.pit import seal_record
from alpha_research_os.data.providers import (
    AKShareProvider,
    BaoStockProvider,
    TushareProvider,
    normalize_akshare_market,
    normalize_baostock_market,
    normalize_baostock_status,
)
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.specs import DataDomain

RETRIEVED_AT = datetime.fromisoformat("2024-05-10T18:00:00+08:00")


class FakeAKShare:
    __version__ = "test"

    def stock_zh_a_hist(self, **kwargs):
        assert kwargs["adjust"] == ""
        return [
            {
                "日期": "2024-05-10",
                "开盘": 10.0,
                "收盘": 10.2,
                "最高": 10.3,
                "最低": 9.9,
                "成交量": 1_000,
                "成交额": 1_015_000.0,
            }
        ]

    def tool_trade_date_hist_sina(self):
        return [{"trade_date": "2024-05-10"}, {"trade_date": "2024-05-13"}]

    def stock_info_a_code_name(self):
        return [{"code": "600000", "name": "浦发银行"}]


class FallbackAKShare(FakeAKShare):
    def stock_zh_a_hist(self, **kwargs):
        raise ConnectionError("primary upstream unavailable")

    def stock_zh_a_daily(self, **kwargs):
        assert kwargs["symbol"] == "sh600000"
        return [
            {
                "date": "2024-05-10",
                "open": 10.0,
                "close": 10.2,
                "high": 10.3,
                "low": 9.9,
                "volume": 100_000,
                "amount": 1_015_000.0,
            }
        ]


class FakeResult:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.error_code = "0"
        self.error_msg = "success"
        self.fields = list(rows[0]) if rows else []
        self._rows = [[row[field] for field in self.fields] for row in rows]
        self._index = -1

    def next(self) -> bool:
        self._index += 1
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        return self._rows[self._index]


class FakeLogin:
    error_code = "0"
    error_msg = "success"


class FakeBaoStock:
    __version__ = "test"

    def __init__(self) -> None:
        self.logout_count = 0

    def login(self):
        return FakeLogin()

    def logout(self):
        self.logout_count += 1

    def query_history_k_data_plus(self, code, fields, **kwargs):
        assert code == "sh.600000"
        assert kwargs["adjustflag"] == "3"
        if "tradestatus" in fields:
            return FakeResult([{"date": "2024-05-10", "code": code, "tradestatus": "1", "isST": "0"}])
        return FakeResult(
            [
                {
                    "date": "2024-05-10",
                    "code": code,
                    "open": "10.0",
                    "high": "10.3",
                    "low": "9.9",
                    "close": "10.2",
                    "preclose": "10.1",
                    "volume": "100000",
                    "amount": "1015000.0",
                }
            ]
        )


class FakeTushareTransport:
    def __init__(self) -> None:
        self.request_document = None

    def post(self, url: str, payload: bytes, *, timeout: float) -> bytes:
        self.request_document = json.loads(payload)
        fields = self.request_document["fields"].split(",")
        return json.dumps({"code": 0, "msg": None, "data": {"fields": fields, "items": []}}).encode()


def _request(domain: DataDomain, fields: tuple[str, ...]) -> FetchRequest:
    return FetchRequest(
        request_id=f"TEST-{domain.value}",
        data_domain=domain,
        start="2024-05-10",
        end="2024-05-10",
        fields=fields,
        instrument_ids=("600000.SH",),
    )


def test_akshare_and_baostock_daily_bars_share_a_canonical_identity(tmp_path) -> None:
    akshare = AKShareProvider(FakeAKShare(), clock=lambda: RETRIEVED_AT)
    baostock_client = FakeBaoStock()
    baostock = BaoStockProvider(baostock_client, clock=lambda: RETRIEVED_AT)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    raw = RawSnapshotStore(artifacts)

    ak_fields = next(item.fields for item in akshare.spec.capabilities if item.data_domain is DataDomain.MARKET)
    ak_response = akshare.fetch(_request(DataDomain.MARKET, ak_fields))
    ak_snapshot = raw.capture(akshare.spec, ak_response)
    ak_records = normalize_akshare_market(ak_response, ak_snapshot.reference)

    bs_fields = next(item.fields for item in baostock.spec.capabilities if item.data_domain is DataDomain.MARKET)
    bs_response = baostock.fetch(_request(DataDomain.MARKET, bs_fields))
    bs_snapshot = raw.capture(baostock.spec, bs_response)
    bs_records = normalize_baostock_market(bs_response, bs_snapshot.reference)

    assert ak_records[0].logical_key == bs_records[0].logical_key == "bar:CN-EQ-600000:2024-05-10"
    for field in ("open", "high", "low", "close", "volume", "amount"):
        assert ak_records[0].value_map()[field] == bs_records[0].value_map()[field]
    assert audit_cross_source_market((*ak_records, *bs_records)) == ()
    assert ak_records[0].raw_snapshot_id != bs_records[0].raw_snapshot_id
    assert baostock_client.logout_count == 1


def test_baostock_historical_status_is_normalized_conservatively(tmp_path) -> None:
    client = FakeBaoStock()
    provider = BaoStockProvider(client, clock=lambda: RETRIEVED_AT)
    fields = next(item.fields for item in provider.spec.capabilities if item.data_domain is DataDomain.SECURITY_STATUS)
    response = provider.fetch(_request(DataDomain.SECURITY_STATUS, fields))
    snapshot = RawSnapshotStore(ArtifactStore(tmp_path / "artifacts")).capture(provider.spec, response)

    record = normalize_baostock_status(response, snapshot.reference)[0]

    assert record.value_map() == {
        "can_buy": True,
        "can_sell": True,
        "is_st": False,
        "is_suspended": False,
        "provider_code": "sh.600000",
        "valid_from": "2024-05-10",
        "valid_to": "2024-05-10",
    }
    assert record.available_at.hour == 15


def test_free_provider_capabilities_do_not_overclaim_pit() -> None:
    akshare = AKShareProvider(FakeAKShare(), clock=lambda: RETRIEVED_AT)
    baostock = BaoStockProvider(FakeBaoStock(), clock=lambda: RETRIEVED_AT)
    tushare = TushareProvider(clock=lambda: RETRIEVED_AT)

    ak_grades = {item.data_domain: item.pit_grade for item in akshare.spec.capabilities}
    bs_grades = {item.data_domain: item.pit_grade for item in baostock.spec.capabilities}

    assert ak_grades[DataDomain.MARKET] is PITGrade.RECONSTRUCTED_PIT
    assert ak_grades[DataDomain.SECURITY_MASTER] is PITGrade.CURRENT_ONLY
    assert bs_grades[DataDomain.SECURITY_STATUS] is PITGrade.RECONSTRUCTED_PIT
    assert tushare.spec.capabilities[0].pit_grade is PITGrade.UNVERIFIED

    request = _request(DataDomain.MARKET, tushare.spec.capabilities[0].fields)
    with pytest.raises(RuntimeError, match="disabled"):
        tushare.fetch(request)


def test_provider_payload_is_canonical_json() -> None:
    provider = AKShareProvider(FakeAKShare(), clock=lambda: RETRIEVED_AT)
    fields = next(item.fields for item in provider.spec.capabilities if item.data_domain is DataDomain.MARKET)

    response = provider.fetch(_request(DataDomain.MARKET, fields))
    document = json.loads(response.payload)

    assert document["schema"] == "provider-tabular-v1"
    assert document["endpoint"] == "stock_zh_a_hist"
    assert document["rows"][0]["provider_code"] == "600000"
    assert document["rows"][0]["raw_volume_unit"] == "lot_100_shares"


def test_akshare_records_and_normalizes_sina_fallback(tmp_path) -> None:
    provider = AKShareProvider(FallbackAKShare(), clock=lambda: RETRIEVED_AT)
    fields = next(item.fields for item in provider.spec.capabilities if item.data_domain is DataDomain.MARKET)
    response = provider.fetch(_request(DataDomain.MARKET, fields))
    snapshot = RawSnapshotStore(ArtifactStore(tmp_path / "artifacts")).capture(provider.spec, response)

    document = json.loads(response.payload)
    record = normalize_akshare_market(response, snapshot.reference)[0]

    assert document["endpoint"] == "stock_zh_a_daily"
    assert document["rows"][0]["upstream"] == "sina"
    assert record.value_map()["volume"] == 100_000


def test_cross_source_market_audit_reports_the_disagreeing_field(tmp_path) -> None:
    provider = AKShareProvider(FakeAKShare(), clock=lambda: RETRIEVED_AT)
    fields = next(item.fields for item in provider.spec.capabilities if item.data_domain is DataDomain.MARKET)
    response = provider.fetch(_request(DataDomain.MARKET, fields))
    snapshot = RawSnapshotStore(ArtifactStore(tmp_path / "artifacts")).capture(provider.spec, response)
    baseline = normalize_akshare_market(response, snapshot.reference)[0]
    poisoned_values = tuple(
        FieldValue(name=item.name, value=10.8 if item.name == "close" else item.value) for item in baseline.values
    )
    poisoned = seal_record(
        baseline.model_copy(
            update={
                "record_hash": None,
                "revision_id": "poisoned",
                "source": "independent-source",
                "source_record_id": "POISONED-CLOSE",
                "values": poisoned_values,
            }
        )
    )

    findings = audit_cross_source_market((baseline, poisoned))

    assert len(findings) == 1
    assert findings[0].code == "CROSS_SOURCE_MARKET_MISMATCH"
    assert findings[0].evidence["field"] == "close"


def test_tushare_token_is_used_only_in_transient_http_request(tmp_path) -> None:
    transport = FakeTushareTransport()
    provider = TushareProvider(
        token="test-secret-token",
        api_base_url="https://gateway.example.invalid/",
        transport=transport,
        clock=lambda: RETRIEVED_AT,
    )
    fields = next(item.fields for item in provider.spec.capabilities if item.data_domain is DataDomain.MARKET)
    request = _request(DataDomain.MARKET, fields).model_copy(update={"parameters": ("api_name=daily",)})

    response = provider.fetch(request)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    raw_store = RawSnapshotStore(artifacts)
    captured = raw_store.capture(provider.spec, response, storage_encoding="gzip")

    assert transport.request_document["token"] == "test-secret-token"
    assert b"test-secret-token" not in response.payload
    assert "test-secret-token" not in provider.spec.model_dump_json()
    assert b"test-secret-token" not in artifacts.read_bytes(captured.manifest_reference)
    assert captured.reference.payload_encoding == "gzip"
    assert raw_store.read_payload(captured) == response.payload
