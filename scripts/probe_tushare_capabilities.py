"""Probe useful Tushare endpoint permissions without storing returned business rows."""

from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROBES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("每日涨跌停价", "stk_limit", {"trade_date": "20240808", "limit": 1}),
    ("指数基本信息", "index_basic", {"market": "SSE", "limit": 1}),
    (
        "指数月度权重",
        "index_weight",
        {"index_code": "000300.SH", "start_date": "20240801", "end_date": "20240831", "limit": 1},
    ),
    ("申万行业分类", "index_classify", {"level": "L1", "src": "SW2021", "limit": 1}),
    ("申万历史成分", "index_member_all", {"ts_code": "600036.SH", "limit": 1}),
    ("财报披露计划", "disclosure_date", {"ts_code": "600036.SH", "limit": 1}),
    ("业绩预告", "forecast", {"ts_code": "600036.SH", "limit": 1}),
    ("业绩预告VIP", "forecast_vip", {"period": "20231231", "limit": 1}),
    ("业绩快报", "express", {"ts_code": "600036.SH", "limit": 1}),
    ("业绩快报VIP", "express_vip", {"period": "20231231", "limit": 1}),
    ("财务审计意见", "fina_audit", {"ts_code": "600036.SH", "limit": 1}),
    ("主营业务构成", "fina_mainbz", {"ts_code": "600036.SH", "type": "P", "limit": 1}),
    ("主营业务构成VIP", "fina_mainbz_vip", {"period": "20231231", "type": "P", "limit": 1}),
    ("限售股解禁", "share_float", {"start_date": "20240801", "end_date": "20240831", "limit": 1}),
    ("股票回购", "repurchase", {"start_date": "20240801", "end_date": "20240831", "limit": 1}),
    ("股东增减持", "stk_holdertrade", {"start_date": "20240801", "end_date": "20240831", "limit": 1}),
    ("股东人数", "stk_holdernumber", {"ts_code": "600036.SH", "limit": 1}),
    ("前十大股东", "top10_holders", {"ts_code": "600036.SH", "period": "20231231", "limit": 1}),
    ("前十大流通股东", "top10_floatholders", {"ts_code": "600036.SH", "period": "20231231", "limit": 1}),
    ("股权质押统计", "pledge_stat", {"ts_code": "600036.SH", "limit": 1}),
    ("股权质押明细", "pledge_detail", {"ts_code": "600036.SH", "limit": 1}),
    ("融资融券汇总", "margin", {"trade_date": "20240808", "limit": 1}),
    ("融资融券明细", "margin_detail", {"trade_date": "20240808", "limit": 1}),
    ("融资融券标的", "margin_secs", {"trade_date": "20240808", "limit": 1}),
    ("个股资金流向", "moneyflow", {"trade_date": "20240808", "limit": 1}),
    ("同花顺个股资金流", "moneyflow_ths", {"trade_date": "20240808", "limit": 1}),
    ("东方财富个股资金流", "moneyflow_dc", {"trade_date": "20240808", "limit": 1}),
    ("大宗交易", "block_trade", {"trade_date": "20240808", "limit": 1}),
    ("龙虎榜每日", "top_list", {"trade_date": "20240808", "limit": 1}),
    ("龙虎榜机构", "top_inst", {"trade_date": "20240808", "limit": 1}),
    ("概念成分", "concept_detail", {"ts_code": "600036.SH", "limit": 1}),
    ("沪深港通持股", "hk_hold", {"trade_date": "20240808", "exchange": "SH", "limit": 1}),
)


def _post(endpoint: str, token: str, api_name: str, params: dict[str, Any]) -> dict[str, Any]:
    payload = json.dumps(
        {"api_name": api_name, "fields": "", "params": params, "token": token},
        separators=(",", ":"),
    ).encode()
    request = Request(endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def _probe(endpoint: str, token: str, label: str, api_name: str, params: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    last_error = ""
    for attempt in range(4):
        try:
            document = _post(endpoint, token, api_name, params)
            code = document.get("code")
            message = str(document.get("msg") or "").replace(token, "[REDACTED]")
            data = document.get("data") if isinstance(document.get("data"), dict) else {}
            fields = data.get("fields") if isinstance(data.get("fields"), list) else []
            items = data.get("items") if isinstance(data.get("items"), list) else []
            if code == 0:
                status = "AVAILABLE" if items else "ACCESSIBLE_EMPTY"
            elif any(word in message.lower() for word in ("权限", "permission", "积分", "privilege")):
                status = "DENIED"
            else:
                status = "API_ERROR"
            return {
                "api_name": api_name,
                "code": code,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "field_count": len(fields),
                "fields": fields,
                "label": label,
                "message": message,
                "returned_rows": len(items),
                "status": status,
            }
        except HTTPError as error:
            last_error = f"HTTP {error.code}"
            if error.code not in (429, 500, 502, 503, 504) or attempt == 3:
                break
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = str(error).replace(token, "[REDACTED]")
            if attempt == 3:
                break
        time.sleep(2 ** attempt * 2)
    return {
        "api_name": api_name,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "error": last_error,
        "label": label,
        "status": "TRANSPORT_ERROR",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="https://t.xiaodefa.top/")
    parser.add_argument("--output", type=Path, default=Path("reports/tushare_extended_capability_probe.json"))
    parser.add_argument("--delay", type=float, default=1.25)
    args = parser.parse_args()
    token = os.environ.get("TUSHARE_TOKEN")
    if not token:
        parser.error("TUSHARE_TOKEN is not set")
    results = []
    for label, api_name, params in PROBES:
        result = _probe(args.endpoint, token, label, api_name, params)
        results.append(result)
        print(f"{api_name}: {result['status']}", flush=True)
        time.sleep(args.delay)
    counts: dict[str, int] = {}
    for result in results:
        counts[result["status"]] = counts.get(result["status"], 0) + 1
    report = {
        "endpoint": args.endpoint,
        "note": "Permission probe only; no returned business rows were persisted.",
        "probed_at": datetime.now().astimezone().isoformat(),
        "results": results,
        "schema": "tushare-capability-probe-v2",
        "summary": counts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(args.output)
    print(f"Report: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
