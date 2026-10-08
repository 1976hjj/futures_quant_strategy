"""Credential-free bank financial acquisition; source facts are not historical PIT certification."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ENDPOINT = "https://datacenter.eastmoney.com/securities/api/data/get"
METRICS = {
    "npl_ratio": "NONPERLOAN",
    "provision_coverage_ratio": "BLDKBBL",
    "cet1_ratio": "HXYJBCZL",
    "capital_adequacy_ratio": "NEWCAPITALADER",
    "net_interest_margin": "NET_INTEREST_MARGIN",
    "tier1_capital_ratio": "FIRST_ADEQUACY_RATIO",
    "net_interest_spread": "NET_INTEREST_SPREAD",
    "loan_provision_ratio": "LOAN_PROVISION_RATIO",
    "loans_gross": "GROSSLOANS",
    "deposits_total": "TOTALDEPOSITS",
    "loans_advances": "LOAN_ADVANCES",
    "npl_amount": "NON_PERFORMING_LOAN",
    "overdue_loan_amount": "OVERDUE_LOANS",
    "vendor_bvps": "BPS",
}
CORE_METRICS = tuple(METRICS)[:5]


def request_params(code: str, page: int = 1) -> dict[str, str]:
    if not re.fullmatch(r"\d{6}\.(SH|SZ)", code):
        raise ValueError("invalid A-share bank code")
    return {"type": "RPT_F10_FINANCE_MAINFINADATA", "sty": "APP_F10_MAINFINADATA",
            "quoteColumns": "", "filter": f'(SECUCODE="{code}")', "p": str(page),
            "ps": "200", "sr": "-1", "st": "REPORT_DATE", "source": "HSF10", "client": "PC"}


def fetch_page(code: str, page: int = 1) -> tuple[bytes, dict[str, Any]]:
    params = request_params(code, page)
    request = Request(ENDPOINT + "?" + urlencode(params), headers={
        "User-Agent": "Mozilla/5.0", "Accept": "application/json",
        "Referer": "https://emweb.securities.eastmoney.com/",
    })
    for attempt in range(3):
        try:
            with urlopen(request, timeout=20) as response:
                raw = response.read(20_000_001)
            if len(raw) > 20_000_000:
                raise ValueError("bank response exceeded size limit")
            payload = json.loads(raw)
            if payload.get("success") is not True or not isinstance(payload.get("result"), dict):
                raise ValueError("bank source returned unsuccessful response")
            if not isinstance(payload["result"].get("data"), list) or not payload["result"]["data"]:
                raise ValueError("bank source returned no financial records")
            return raw, params
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt * 2)
    raise AssertionError("unreachable")


def _day(value: Any) -> str:
    result = str(value or "")[:10]
    date.fromisoformat(result)
    return result


def normalize_row(row: dict[str, Any], bank: dict[str, str], *, retrieved_at: str,
                  requested_end: date, run_id: str, raw_sha256: str) -> dict[str, Any] | None:
    if row.get("SECUCODE") != bank["code"] or row.get("ORG_TYPE") != "银行":
        raise ValueError(f"bank identity mismatch for {bank['code']}")
    report = _day(row["REPORT_DATE"])
    notice = _day(row["NOTICE_DATE"]) if row.get("NOTICE_DATE") else None
    updated = _day(row["UPDATE_DATE"]) if row.get("UPDATE_DATE") else None
    if notice and report > notice:
        raise ValueError(f"report date after notice for {bank['code']}")
    if report > requested_end.isoformat() or (notice and notice > requested_end.isoformat()):
        return None
    observed_day = datetime.fromisoformat(retrieved_at).astimezone(timezone(timedelta(hours=8))).date()
    if updated and updated > observed_day.isoformat():
        raise ValueError("upstream update date is in the future")
    values = {}
    for metric, source_field in METRICS.items():
        value = row.get(source_field)
        if value in (None, "", "--", "-", "null"):
            values[metric] = None
        else:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"non-finite bank metric {source_field}")
            values[metric] = number
    flags = []
    if notice is None:
        flags.append("missing_provider_notice_date")
    if updated is None:
        flags.append("missing_provider_update_date")
    if any(values[k] is not None and not 0 <= values[k] <= 100
           for k in ("npl_ratio", "cet1_ratio", "capital_adequacy_ratio", "tier1_capital_ratio")):
        flags.append("ratio_range")
    if values["provision_coverage_ratio"] is not None and not 0 <= values["provision_coverage_ratio"] <= 10000:
        flags.append("provision_range")
    capital = [values[k] for k in ("cet1_ratio", "tier1_capital_ratio", "capital_adequacy_ratio")]
    if all(v is not None for v in capital) and not capital[0] <= capital[1] <= capital[2]:
        flags.append("capital_order")
    return {"run_id": run_id, **bank, "source_id": "eastmoney", "report_date": report,
            "report_type": row.get("REPORT_TYPE"), "provider_notice_date": notice,
            "provider_update_date": updated, "verified_notice_date": None,
            "retrieved_at_utc": retrieved_at, "available_at": retrieved_at,
            "record_sha256": hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(),
            "raw_sha256": raw_sha256, "requested_end": requested_end.isoformat(),
            "pit_grade": "CURRENT_SNAPSHOT_HISTORY_UNVERIFIED",
            "capital_method": "UNVERIFIED_PROVIDER_SELECTED",
            "units": "risk_capital_margin:percent;amounts:CNY;vendor_bvps:CNY/share",
            "quality_flags": json.dumps(flags),
            "missing_core": json.dumps([k for k in CORE_METRICS if values[k] is None]), **values}
