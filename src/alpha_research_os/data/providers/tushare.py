"""Tushare-compatible HTTP adapter with explicit secret and endpoint boundaries."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Protocol
from urllib.request import Request, urlopen

from alpha_research_os.data.contracts import (
    DomainCapability,
    FetchRequest,
    LicenseSpec,
    PITGrade,
    ProviderResponse,
    ProviderSpec,
    RevisionBehavior,
)
from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.kernel.specs import DataDomain

Clock = Callable[[], datetime]


class HTTPTransport(Protocol):
    def post(self, url: str, payload: bytes, *, timeout: float) -> bytes: ...


class UrllibHTTPTransport:
    def post(self, url: str, payload: bytes, *, timeout: float) -> bytes:
        request = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "alpha-research-os/0.1"},
            method="POST",
        )
        with urlopen(request, timeout=timeout) as response:  # noqa: S310 - configured HTTPS endpoint
            return response.read()


_ENDPOINT_DOMAINS = {
    "trade_cal": DataDomain.TRADING_CALENDAR,
    "daily": DataDomain.MARKET,
    "adj_factor": DataDomain.MARKET,
    "daily_basic": DataDomain.MARKET,
    "stock_basic": DataDomain.SECURITY_MASTER,
    "suspend_d": DataDomain.SECURITY_STATUS,
    "stk_limit": DataDomain.SECURITY_STATUS,
    "namechange": DataDomain.SECURITY_STATUS,
    "stock_st": DataDomain.SECURITY_STATUS,
    "income": DataDomain.FUNDAMENTAL,
    "income_vip": DataDomain.FUNDAMENTAL,
    "balancesheet": DataDomain.FUNDAMENTAL,
    "balancesheet_vip": DataDomain.FUNDAMENTAL,
    "cashflow": DataDomain.FUNDAMENTAL,
    "cashflow_vip": DataDomain.FUNDAMENTAL,
    "fina_indicator": DataDomain.FUNDAMENTAL,
    "fina_indicator_vip": DataDomain.FUNDAMENTAL,
    "disclosure_date": DataDomain.FUNDAMENTAL,
    "forecast": DataDomain.FUNDAMENTAL,
    "forecast_vip": DataDomain.FUNDAMENTAL,
    "express": DataDomain.FUNDAMENTAL,
    "express_vip": DataDomain.FUNDAMENTAL,
    "fina_audit": DataDomain.FUNDAMENTAL,
    "fina_mainbz": DataDomain.FUNDAMENTAL,
    "fina_mainbz_vip": DataDomain.FUNDAMENTAL,
    "stk_holdernumber": DataDomain.FUNDAMENTAL,
    "top10_holders": DataDomain.FUNDAMENTAL,
    "top10_floatholders": DataDomain.FUNDAMENTAL,
    "dividend": DataDomain.CORPORATE_ACTION,
    "share_float": DataDomain.CORPORATE_ACTION,
    "repurchase": DataDomain.CORPORATE_ACTION,
    "stk_holdertrade": DataDomain.CORPORATE_ACTION,
    "pledge_stat": DataDomain.CORPORATE_ACTION,
    "pledge_detail": DataDomain.CORPORATE_ACTION,
    "index_basic": DataDomain.UNIVERSE,
    "index_classify": DataDomain.UNIVERSE,
    "index_member_all": DataDomain.UNIVERSE,
    "index_weight": DataDomain.UNIVERSE,
    "margin": DataDomain.MARKET,
    "margin_detail": DataDomain.MARKET,
    "margin_secs": DataDomain.MARKET,
    "hk_hold": DataDomain.MARKET,
}

_DEFAULT_ENDPOINTS = {
    DataDomain.TRADING_CALENDAR: "trade_cal",
    DataDomain.MARKET: "daily",
    DataDomain.SECURITY_MASTER: "stock_basic",
    DataDomain.SECURITY_STATUS: "suspend_d",
    DataDomain.FUNDAMENTAL: "fina_indicator",
    DataDomain.CORPORATE_ACTION: "dividend",
    DataDomain.UNIVERSE: "index_weight",
}

_DOMAIN_FIELDS = {
    DataDomain.TRADING_CALENDAR: ("exchange", "cal_date", "is_open", "pretrade_date"),
    DataDomain.MARKET: (
        "ts_code",
        "trade_date",
        "open",
        "high",
        "low",
        "close",
        "pre_close",
        "change",
        "pct_chg",
        "vol",
        "amount",
        "adj_factor",
        "turnover_rate",
        "turnover_rate_f",
        "volume_ratio",
        "pe",
        "pe_ttm",
        "pb",
        "ps",
        "ps_ttm",
        "dv_ratio",
        "dv_ttm",
        "total_share",
        "float_share",
        "free_share",
        "total_mv",
        "circ_mv",
    ),
    DataDomain.SECURITY_MASTER: (
        "ts_code",
        "symbol",
        "name",
        "area",
        "industry",
        "fullname",
        "enname",
        "cnspell",
        "market",
        "exchange",
        "curr_type",
        "list_status",
        "list_date",
        "delist_date",
        "is_hs",
        "act_name",
        "act_ent_type",
    ),
    DataDomain.SECURITY_STATUS: (
        "ts_code",
        "trade_date",
        "suspend_type",
        "suspend_timing",
        "pre_close",
        "up_limit",
        "down_limit",
        "name",
        "start_date",
        "end_date",
        "ann_date",
        "change_reason",
        "type",
        "type_name",
    ),
    DataDomain.FUNDAMENTAL: (
        "ts_code",
        "ann_date",
        "f_ann_date",
        "end_date",
        "report_type",
        "comp_type",
        "end_type",
        "update_flag",
        "total_revenue",
        "revenue",
        "operate_profit",
        "total_profit",
        "n_income",
        "n_income_attr_p",
        "total_assets",
        "total_liab",
        "total_hldr_eqy_exc_min_int",
        "n_cashflow_act",
        "n_cashflow_inv_act",
        "n_cash_flows_fnc_act",
        "eps",
        "dt_eps",
        "roe",
        "roa",
        "profit_dedt",
    ),
    DataDomain.CORPORATE_ACTION: (
        "ts_code",
        "end_date",
        "ann_date",
        "div_proc",
        "stk_div",
        "stk_bo_rate",
        "stk_co_rate",
        "cash_div",
        "cash_div_tax",
        "record_date",
        "ex_date",
        "pay_date",
        "div_listdate",
        "imp_ann_date",
        "base_date",
        "base_share",
    ),
    DataDomain.UNIVERSE: ("index_code", "con_code", "trade_date", "weight"),
}


def _parameter_map(parameters: tuple[str, ...]) -> dict[str, str]:
    parsed = {}
    for parameter in parameters:
        key, separator, value = parameter.partition("=")
        if separator != "=" or not key or not value or key in parsed:
            raise ValueError(f"invalid or duplicate provider parameter: {parameter}")
        parsed[key] = value
    return parsed


def tushare_response_rows(payload: bytes) -> list[dict[str, object]]:
    document = json.loads(payload)
    data = document.get("data", {})
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or not isinstance(items, list):
        raise ValueError("invalid Tushare-compatible tabular payload")
    return [dict(zip(fields, item, strict=True)) for item in items]


def _provider_code(instrument_id: str) -> str:
    value = instrument_id.upper()
    if value.endswith((".SH", ".SZ", ".BJ")):
        return value
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) != 6:
        raise ValueError(f"cannot map instrument_id to Tushare code: {instrument_id}")
    exchange = "SH" if digits.startswith(("5", "6", "9")) else "BJ" if digits.startswith(("4", "8")) else "SZ"
    return f"{digits}.{exchange}"


class TushareProvider:
    """Acquire exact JSON responses from a Tushare-compatible endpoint.

    Tokens live only in this process. They are never placed in FetchRequest,
    ProviderSpec, ProviderResponse payloads, or artifact metadata.
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        api_base_url: str = "https://api.tushare.pro/",
        transport: HTTPTransport | None = None,
        clock: Clock | None = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_base_url.startswith("https://"):
            raise ValueError("Tushare-compatible endpoint must use HTTPS")
        self._token = token
        self._transport = transport or UrllibHTTPTransport()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._timeout = timeout
        self._spec = ProviderSpec(
            provider_id="tushare-compatible",
            provider_version="http-api-v1",
            adapter_version="0.2.0",
            api_base_url=api_base_url,
            documentation_url="https://tushare.pro/document/2",
            assessed_at=datetime(2026, 9, 1, tzinfo=UTC),
            assessor="alpha-research-os",
            timezone="Asia/Shanghai",
            license=LicenseSpec(
                license_id="tushare-compatible-personal-research",
                terms_url="https://tushare.pro/document/1?doc_id=405",
                retrieval_allowed=True,
                local_raw_storage_allowed=True,
                derived_storage_allowed=True,
                redistribution_allowed=False,
                commercial_use_allowed=False,
                credential_required=True,
                attribution_required=True,
                notes=(
                    "Personal research only. The configured gateway may be a third-party Tushare-compatible "
                    "service; its purchase terms must be retained separately. No redistribution is authorized."
                ),
            ),
            capabilities=tuple(self._capability(domain) for domain in _DOMAIN_FIELDS),
            response_backfill_policy=(
                "exact responses are append-only local snapshots; upstream revisions may overwrite"
            ),
            rate_limit_notes=(
                "Entitlement is gateway-specific; use bounded retries and checkpointed sequential backfill."
            ),
        )

    @staticmethod
    def _capability(domain: DataDomain) -> DomainCapability:
        grade = {
            DataDomain.MARKET: PITGrade.RECONSTRUCTED_PIT,
            DataDomain.SECURITY_STATUS: PITGrade.RECONSTRUCTED_PIT,
            DataDomain.FUNDAMENTAL: PITGrade.RECONSTRUCTED_PIT,
            DataDomain.CORPORATE_ACTION: PITGrade.RECONSTRUCTED_PIT,
            DataDomain.SECURITY_MASTER: PITGrade.CURRENT_ONLY,
            DataDomain.TRADING_CALENDAR: PITGrade.UNVERIFIED,
            DataDomain.UNIVERSE: PITGrade.UNVERIFIED,
        }[domain]
        revision = (
            RevisionBehavior.APPEND_WITH_HISTORY
            if domain in {DataDomain.FUNDAMENTAL, DataDomain.CORPORATE_ACTION}
            else RevisionBehavior.OVERWRITES_HISTORY
        )
        return DomainCapability(
            data_domain=domain,
            fields=_DOMAIN_FIELDS[domain],
            coverage_start=None,
            coverage_end=None,
            pit_grade=grade,
            revision_behavior=revision,
            time_fields=("event_time", "published_at", "ingested_at")
            if domain in {DataDomain.FUNDAMENTAL, DataDomain.CORPORATE_ACTION}
            else ("event_time", "ingested_at"),
            preserves_delisted_history=True if domain is DataDomain.SECURITY_MASTER else None,
            preserves_historical_status=True if domain is DataDomain.SECURITY_STATUS else None,
            limitations=(
                "gateway capability was sampled, not exhaustively audited",
                "available_at must be reconstructed conservatively by endpoint-specific normalizers",
            ),
        )

    @property
    def spec(self) -> ProviderSpec:
        return self._spec

    def fetch(self, request: FetchRequest) -> ProviderResponse:
        request = FetchRequest.model_validate(request)
        if not self._token:
            raise RuntimeError("Tushare-compatible provider is disabled: token was not supplied")
        parameters = _parameter_map(request.parameters)
        endpoint = parameters.pop("api_name", _DEFAULT_ENDPOINTS[request.data_domain])
        all_fields = parameters.pop("_all_fields", "false").lower() == "true"
        if _ENDPOINT_DOMAINS.get(endpoint) is not request.data_domain:
            raise ValueError(f"endpoint {endpoint} is not declared for domain {request.data_domain.value}")
        params = self._build_params(endpoint, request, parameters)
        body = canonical_json_bytes(
            {
                "api_name": endpoint,
                "fields": "" if all_fields else ",".join(request.fields),
                "params": params,
                "token": self._token,
            }
        )
        payload = self._transport.post(self.spec.api_base_url, body, timeout=self._timeout)
        document = json.loads(payload)
        if document.get("code") != 0:
            raise RuntimeError(f"Tushare-compatible API failed: code={document.get('code')} msg={document.get('msg')}")
        if not isinstance(document.get("data", {}).get("fields"), list) or not isinstance(
            document.get("data", {}).get("items"), list
        ):
            raise RuntimeError("Tushare-compatible API returned an invalid tabular response")
        return ProviderResponse(
            request=request,
            provider_request_id=f"TS-{endpoint}-{request.request_id}",
            retrieved_at=self._clock(),
            media_type="application/json",
            payload=payload,
        )

    @staticmethod
    def _build_params(
        endpoint: str,
        request: FetchRequest,
        explicit: Mapping[str, str],
    ) -> dict[str, str]:
        params = dict(explicit)
        query_mode = params.pop("_query_mode", None)
        if request.instrument_ids:
            if len(request.instrument_ids) != 1:
                raise ValueError("one raw Tushare snapshot must contain at most one explicit instrument")
            params.setdefault("ts_code", _provider_code(request.instrument_ids[0]))
            params.setdefault("start_date", request.start.strftime("%Y%m%d"))
            params.setdefault("end_date", request.end.strftime("%Y%m%d"))
            return params
        if endpoint == "stock_basic":
            return params
        if endpoint in {"namechange", "dividend"} and query_mode == "all":
            return params
        if endpoint == "namechange" and query_mode == "range":
            params.setdefault("start_date", request.start.strftime("%Y%m%d"))
            params.setdefault("end_date", request.end.strftime("%Y%m%d"))
            return params
        if endpoint == "trade_cal":
            params.setdefault("start_date", request.start.strftime("%Y%m%d"))
            params.setdefault("end_date", request.end.strftime("%Y%m%d"))
            return params
        has_explicit_range = "start_date" in params and "end_date" in params
        if request.start != request.end and "period" not in params and not has_explicit_range:
            raise ValueError("all-market raw requests must target one date or provide an explicit period")
        if not any(key in params for key in ("period", "trade_date", "ann_date", "start_date", "end_date")):
            date_parameter = "trade_date" if endpoint not in {"dividend", "namechange"} else "ann_date"
            params.setdefault(date_parameter, request.start.strftime("%Y%m%d"))
        return params
