"""AKShare acquisition adapter for the free A-share data tier."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
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

from ._shared import as_float, as_int, ashare_code, baostock_code, instrument_key, row_revision, session_close
from ._tabular import payload_rows, records_from_table, tabular_payload

Clock = Callable[[], datetime]


class AKShareProvider:
    """Capture AKShare tables as immutable provider-native JSON snapshots.

    The adapter deliberately exposes only capabilities whose endpoint semantics
    have been reviewed. AKShare's broad catalogue is not automatically trusted.
    """

    def __init__(self, client: Any | None = None, *, clock: Clock | None = None) -> None:
        self._client = client if client is not None else import_module("akshare")
        self._clock = clock or (lambda: datetime.now(UTC))
        client_version = str(getattr(self._client, "__version__", "unknown"))
        self._spec = ProviderSpec(
            provider_id="akshare",
            provider_version=client_version,
            adapter_version="0.1.0",
            api_base_url="https://github.com/akfamily/akshare",
            documentation_url="https://akshare.akfamily.xyz/",
            assessed_at=datetime(2026, 9, 1, tzinfo=UTC),
            assessor="alpha-research-os",
            timezone="Asia/Shanghai",
            license=LicenseSpec(
                license_id="akshare-personal-research",
                terms_url="https://github.com/akfamily/akshare#statement",
                retrieval_allowed=True,
                local_raw_storage_allowed=True,
                derived_storage_allowed=True,
                redistribution_allowed=False,
                commercial_use_allowed=False,
                credential_required=False,
                attribution_required=True,
                notes=(
                    "Enabled only for the owner's personal non-commercial research. The MIT client license does "
                    "not grant rights to upstream data; each upstream endpoint remains subject to review."
                ),
            ),
            capabilities=(
                DomainCapability(
                    data_domain=DataDomain.MARKET,
                    fields=("trade_date", "provider_code", "open", "high", "low", "close", "volume", "amount"),
                    coverage_start=None,
                    coverage_end=None,
                    pit_grade=PITGrade.RECONSTRUCTED_PIT,
                    revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
                    time_fields=("event_time", "ingested_at"),
                    preserves_delisted_history=None,
                    preserves_historical_status=False,
                    units=("price:CNY/share", "raw volume:100-share lots", "normalized volume:shares", "amount:CNY"),
                    limitations=(
                        "unadjusted daily bars only",
                        "historical corrections are not versioned by the upstream endpoint",
                        "availability is conservatively reconstructed at session close",
                        "Eastmoney is primary and Sina is the recorded fallback upstream",
                    ),
                ),
                DomainCapability(
                    data_domain=DataDomain.TRADING_CALENDAR,
                    fields=("trade_date", "is_session"),
                    coverage_start=None,
                    coverage_end=None,
                    pit_grade=PITGrade.UNVERIFIED,
                    revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
                    time_fields=("event_time", "ingested_at"),
                    limitations=("current endpoint has no historical publication lineage",),
                ),
                DomainCapability(
                    data_domain=DataDomain.SECURITY_MASTER,
                    fields=("provider_code", "name"),
                    coverage_start=None,
                    coverage_end=None,
                    pit_grade=PITGrade.CURRENT_ONLY,
                    revision_behavior=RevisionBehavior.OVERWRITES_HISTORY,
                    time_fields=("ingested_at",),
                    preserves_delisted_history=False,
                    preserves_historical_status=False,
                    limitations=("current listed-company catalogue only",),
                ),
            ),
            response_backfill_policy="upstream may overwrite history; every acquisition is snapshotted locally",
            rate_limit_notes="No stable contract; caller must throttle and retry conservatively.",
        )

    @property
    def spec(self) -> ProviderSpec:
        return self._spec

    def fetch(self, request: FetchRequest) -> ProviderResponse:
        request = FetchRequest.model_validate(request)
        if request.data_domain is DataDomain.MARKET:
            endpoints = set()
            if not request.instrument_ids:
                raise ValueError("AKShare market fetch requires explicit instrument_ids")
            rows = []
            for instrument_id in request.instrument_ids:
                code = ashare_code(instrument_id)
                try:
                    endpoint = "stock_zh_a_hist"
                    table = self._client.stock_zh_a_hist(
                        symbol=code,
                        period="daily",
                        start_date=request.start.strftime("%Y%m%d"),
                        end_date=request.end.strftime("%Y%m%d"),
                        adjust="",
                    )
                    volume_unit = "lot_100_shares"
                    upstream = "eastmoney"
                except Exception:
                    endpoint = "stock_zh_a_daily"
                    symbol = baostock_code(instrument_id).replace(".", "")
                    table = self._client.stock_zh_a_daily(
                        symbol=symbol,
                        start_date=request.start.strftime("%Y%m%d"),
                        end_date=request.end.strftime("%Y%m%d"),
                        adjust="",
                    )
                    volume_unit = "shares"
                    upstream = "sina"
                endpoints.add(endpoint)
                instrument_rows = records_from_table(table)
                for row in instrument_rows:
                    row["provider_code"] = code
                    row["raw_volume_unit"] = volume_unit
                    row["upstream"] = upstream
                rows.extend(instrument_rows)
            endpoint = "+".join(sorted(endpoints))
        elif request.data_domain is DataDomain.TRADING_CALENDAR:
            endpoint = "tool_trade_date_hist_sina"
            rows = records_from_table(self._client.tool_trade_date_hist_sina())
            rows = [
                row
                for row in rows
                if request.start <= date.fromisoformat(str(row["trade_date"])[:10]) <= request.end
            ]
            for row in rows:
                row["is_session"] = True
        elif request.data_domain is DataDomain.SECURITY_MASTER:
            endpoint = "stock_info_a_code_name"
            rows = records_from_table(self._client.stock_info_a_code_name())
            if request.instrument_ids:
                requested = {ashare_code(item) for item in request.instrument_ids}
                rows = [row for row in rows if str(row.get("code") or row.get("代码")) in requested]
        else:
            raise ValueError(f"AKShare does not declare capability for {request.data_domain.value}")
        retrieved_at = self._clock()
        return ProviderResponse(
            request=request,
            provider_request_id=f"AK-{endpoint}-{request.request_id}",
            retrieved_at=retrieved_at,
            media_type="application/vnd.alpha-research-os.provider-tabular+json",
            payload=tabular_payload(
                endpoint=endpoint,
                rows=rows,
                metadata={"adapter_version": self.spec.adapter_version, "provider_version": self.spec.provider_version},
            ),
        )


def normalize_akshare_market(
    response: ProviderResponse,
    snapshot: RawSnapshotRef,
) -> tuple[NormalizedRecord, ...]:
    if response.request.data_domain is not DataDomain.MARKET or snapshot.provider_id != "akshare":
        raise ValueError("AKShare market normalizer received mismatched lineage")
    normalized = []
    for row in payload_rows(response.payload):
        trade_date = str(row.get("日期") or row.get("trade_date") or row.get("date"))[:10]
        provider_code = str(row.get("provider_code") or row.get("股票代码") or row.get("code"))
        event_time = session_close(trade_date)
        if snapshot.retrieved_at.astimezone(event_time.tzinfo) < event_time:
            raise ValueError("cannot normalize an AKShare daily bar acquired before session close")
        values = {
            "amount": as_float(row.get("成交额", row.get("amount"))),
            "close": as_float(row.get("收盘", row.get("close"))),
            "high": as_float(row.get("最高", row.get("high"))),
            "low": as_float(row.get("最低", row.get("low"))),
            "open": as_float(row.get("开盘", row.get("open"))),
            "provider_code": provider_code,
            "volume": as_int(row.get("成交量", row.get("volume")))
            * (100 if row.get("raw_volume_unit") == "lot_100_shares" else 1),
        }
        source_record_id = f"AK-MARKET-{provider_code}-{trade_date}"
        record = NormalizedRecord(
            logical_key=f"bar:{instrument_key(provider_code)}:{trade_date}",
            record_type=DataDomain.MARKET,
            instrument_id=instrument_key(provider_code),
            event_time=event_time,
            published_at=event_time,
            available_at=event_time,
            ingested_at=snapshot.retrieved_at,
            source="akshare",
            source_record_id=source_record_id,
            revision_id=row_revision(row),
            raw_snapshot_id=snapshot.snapshot_id,
            values=tuple(FieldValue(name=name, value=value) for name, value in sorted(values.items())),
        )
        normalized.append(seal_record(record))
    return tuple(normalized)
