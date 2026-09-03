"""BaoStock adapter used as an independent free A-share verification source."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from importlib import import_module
from typing import Any

from alpha_research_os.data.contracts import (
    DomainCapability,
    FetchRequest,
    FieldValue,
    LicenseSpec,
    NormalizedRecord,
    PITGrade,
    ProviderResponse,
    ProviderSpec,
    RawSnapshotRef,
    RevisionBehavior,
)
from alpha_research_os.data.pit import seal_record
from alpha_research_os.kernel.specs import DataDomain

from ._shared import as_float, as_int, baostock_code, instrument_key, row_revision, session_close
from ._tabular import payload_rows, tabular_payload

Clock = Callable[[], datetime]


def _result_rows(result: Any) -> list[dict[str, str]]:
    if str(result.error_code) != "0":
        raise RuntimeError(f"BaoStock query failed: {result.error_code} {result.error_msg}")
    rows = []
    fields = [str(field) for field in result.fields]
    while result.next():
        rows.append(dict(zip(fields, result.get_row_data(), strict=True)))
    return rows


class BaoStockProvider:
    """Acquire unadjusted bars and daily status from BaoStock's free server."""

    def __init__(self, client: Any | None = None, *, clock: Clock | None = None) -> None:
        self._client = client if client is not None else import_module("baostock")
        self._clock = clock or (lambda: datetime.now(UTC))
        client_version = str(getattr(self._client, "__version__", "0.9.x"))
        self._spec = ProviderSpec(
            provider_id="baostock",
            provider_version=client_version,
            adapter_version="0.1.0",
            api_base_url="http://www.baostock.com",
            documentation_url="https://pypi.org/project/baostock/",
            assessed_at=datetime(2026, 9, 1, tzinfo=UTC),
            assessor="alpha-research-os",
            timezone="Asia/Shanghai",
            license=LicenseSpec(
                license_id="baostock-personal-research",
                terms_url="http://www.baostock.com/baostock/index.php/Python_API文档",
                retrieval_allowed=True,
                local_raw_storage_allowed=True,
                derived_storage_allowed=True,
                redistribution_allowed=False,
                commercial_use_allowed=None,
                credential_required=False,
                attribution_required=True,
                notes=(
                    "Free anonymous service used for personal research. Client licensing does not establish "
                    "redistribution rights for the data, so raw and derived datasets remain local."
                ),
            ),
            capabilities=(
                DomainCapability(
                    data_domain=DataDomain.MARKET,
                    fields=(
                        "trade_date",
                        "provider_code",
                        "open",
                        "high",
                        "low",
                        "close",
                        "pre_close",
                        "volume",
                        "amount",
                    ),
                    coverage_start=None,
                    coverage_end=None,
                    pit_grade=PITGrade.RECONSTRUCTED_PIT,
                    revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
                    time_fields=("event_time", "ingested_at"),
                    preserves_delisted_history=None,
                    preserves_historical_status=True,
                    units=("price:CNY/share", "volume:shares", "amount:CNY"),
                    limitations=(
                        "unadjusted daily bars only",
                        "historical corrections are not versioned by the upstream endpoint",
                        "availability is conservatively reconstructed at session close",
                    ),
                ),
                DomainCapability(
                    data_domain=DataDomain.SECURITY_STATUS,
                    fields=("trade_date", "provider_code", "trade_status", "is_st"),
                    coverage_start=None,
                    coverage_end=None,
                    pit_grade=PITGrade.RECONSTRUCTED_PIT,
                    revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
                    time_fields=("event_time", "ingested_at"),
                    preserves_delisted_history=None,
                    preserves_historical_status=True,
                    limitations=("does not by itself prove historical price-limit tradability",),
                ),
                DomainCapability(
                    data_domain=DataDomain.TRADING_CALENDAR,
                    fields=("calendar_date", "is_session"),
                    coverage_start=None,
                    coverage_end=None,
                    pit_grade=PITGrade.UNVERIFIED,
                    revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
                    time_fields=("event_time", "ingested_at"),
                    limitations=("current endpoint has no historical publication lineage",),
                ),
                DomainCapability(
                    data_domain=DataDomain.SECURITY_MASTER,
                    fields=("provider_code", "name", "exchange", "list_date", "delist_date", "status"),
                    coverage_start=None,
                    coverage_end=None,
                    pit_grade=PITGrade.CURRENT_ONLY,
                    revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
                    time_fields=("ingested_at",),
                    preserves_delisted_history=None,
                    preserves_historical_status=False,
                    limitations=("listing and delisting dates are current knowledge, not historical knowledge",),
                ),
            ),
            response_backfill_policy="upstream may overwrite history; every acquisition is snapshotted locally",
            rate_limit_notes="Anonymous login; sequential requests and conservative retries required.",
        )

    @property
    def spec(self) -> ProviderSpec:
        return self._spec

    def fetch(self, request: FetchRequest) -> ProviderResponse:
        request = FetchRequest.model_validate(request)
        login = self._client.login()
        if str(login.error_code) != "0":
            raise RuntimeError(f"BaoStock login failed: {login.error_code} {login.error_msg}")
        try:
            endpoint, rows = self._fetch_authenticated(request)
        finally:
            self._client.logout()
        return ProviderResponse(
            request=request,
            provider_request_id=f"BS-{endpoint}-{request.request_id}",
            retrieved_at=self._clock(),
            media_type="application/vnd.alpha-research-os.provider-tabular+json",
            payload=tabular_payload(
                endpoint=endpoint,
                rows=rows,
                metadata={"adapter_version": self.spec.adapter_version, "provider_version": self.spec.provider_version},
            ),
        )

    def _fetch_authenticated(self, request: FetchRequest) -> tuple[str, list[dict[str, str]]]:
        if request.data_domain in {DataDomain.MARKET, DataDomain.SECURITY_STATUS}:
            if not request.instrument_ids:
                raise ValueError("BaoStock bar/status fetch requires explicit instrument_ids")
            if request.data_domain is DataDomain.MARKET:
                endpoint = "query_history_k_data_plus"
                fields = "date,code,open,high,low,close,preclose,volume,amount"
            else:
                endpoint = "query_history_k_data_plus"
                fields = "date,code,tradestatus,isST"
            rows = []
            for instrument_id in request.instrument_ids:
                result = self._client.query_history_k_data_plus(
                    baostock_code(instrument_id),
                    fields,
                    start_date=request.start.isoformat(),
                    end_date=request.end.isoformat(),
                    frequency="d",
                    adjustflag="3",
                )
                rows.extend(_result_rows(result))
            return endpoint, rows
        if request.data_domain is DataDomain.TRADING_CALENDAR:
            result = self._client.query_trade_dates(
                start_date=request.start.isoformat(),
                end_date=request.end.isoformat(),
            )
            return "query_trade_dates", _result_rows(result)
        if request.data_domain is DataDomain.SECURITY_MASTER:
            rows = []
            if request.instrument_ids:
                for instrument_id in request.instrument_ids:
                    rows.extend(_result_rows(self._client.query_stock_basic(code=baostock_code(instrument_id))))
            else:
                rows.extend(_result_rows(self._client.query_stock_basic()))
            return "query_stock_basic", rows
        raise ValueError(f"BaoStock does not declare capability for {request.data_domain.value}")


def normalize_baostock_market(
    response: ProviderResponse,
    snapshot: RawSnapshotRef,
) -> tuple[NormalizedRecord, ...]:
    if response.request.data_domain is not DataDomain.MARKET or snapshot.provider_id != "baostock":
        raise ValueError("BaoStock market normalizer received mismatched lineage")
    normalized = []
    for row in payload_rows(response.payload):
        trade_date = str(row["date"])[:10]
        provider_code = str(row["code"])
        event_time = session_close(trade_date)
        if snapshot.retrieved_at.astimezone(event_time.tzinfo) < event_time:
            raise ValueError("cannot normalize a BaoStock daily bar acquired before session close")
        values = {
            "amount": as_float(row["amount"]),
            "close": as_float(row["close"]),
            "high": as_float(row["high"]),
            "low": as_float(row["low"]),
            "open": as_float(row["open"]),
            "pre_close": as_float(row["preclose"]),
            "provider_code": provider_code,
            "volume": as_int(row["volume"]),
        }
        record = NormalizedRecord(
            logical_key=f"bar:{instrument_key(provider_code)}:{trade_date}",
            record_type=DataDomain.MARKET,
            instrument_id=instrument_key(provider_code),
            event_time=event_time,
            published_at=event_time,
            available_at=event_time,
            ingested_at=snapshot.retrieved_at,
            source="baostock",
            source_record_id=f"BS-MARKET-{provider_code}-{trade_date}",
            revision_id=row_revision(row),
            raw_snapshot_id=snapshot.snapshot_id,
            values=tuple(FieldValue(name=name, value=value) for name, value in sorted(values.items())),
        )
        normalized.append(seal_record(record))
    return tuple(normalized)


def normalize_baostock_status(
    response: ProviderResponse,
    snapshot: RawSnapshotRef,
) -> tuple[NormalizedRecord, ...]:
    if response.request.data_domain is not DataDomain.SECURITY_STATUS or snapshot.provider_id != "baostock":
        raise ValueError("BaoStock status normalizer received mismatched lineage")
    normalized = []
    for row in payload_rows(response.payload):
        trade_date = str(row["date"])[:10]
        provider_code = str(row["code"])
        event_time = session_close(trade_date)
        is_suspended = str(row["tradestatus"]) != "1"
        values = {
            "can_buy": not is_suspended,
            "can_sell": not is_suspended,
            "is_st": str(row["isST"]) == "1",
            "is_suspended": is_suspended,
            "provider_code": provider_code,
            "valid_from": trade_date,
            "valid_to": trade_date,
        }
        record = NormalizedRecord(
            logical_key=f"status:{instrument_key(provider_code)}:{trade_date}",
            record_type=DataDomain.SECURITY_STATUS,
            instrument_id=instrument_key(provider_code),
            event_time=event_time,
            published_at=event_time,
            available_at=event_time,
            ingested_at=snapshot.retrieved_at,
            source="baostock",
            source_record_id=f"BS-STATUS-{provider_code}-{trade_date}",
            revision_id=row_revision(row),
            raw_snapshot_id=snapshot.snapshot_id,
            values=tuple(FieldValue(name=name, value=value) for name, value in sorted(values.items())),
        )
        normalized.append(seal_record(record))
    return tuple(normalized)
