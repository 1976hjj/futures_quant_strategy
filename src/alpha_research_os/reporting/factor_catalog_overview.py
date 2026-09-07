"""Join factor catalog metadata to published values and M4 evidence."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Literal

from alpha_research_os.factors.alpha158 import FactorCategory, alpha158_catalog
from alpha_research_os.factors.library import m4_2_factor_entries

CATEGORY_ORDER: tuple[FactorCategory, ...] = ("动量", "波动", "流动性", "质量", "估值", "风格", "量价", "形态")
STATUS_ORDER = {"M4_COMPLETE": 0, "CALCULATED": 1, "NOT_CALCULATED": 2}

CURRENT_LOCALIZATION: dict[str, tuple[str, FactorCategory, str]] = {
    "price-momentum-20": ("20日价格动量", "动量", "观察过去20个交易日的复权价格趋势是否延续。"),
    "short-reversal-5": ("5日短期反转", "动量", "给近期跌幅较大的股票更高分，检验短期冲击是否回补。"),
    "overnight-gap-1": ("隔夜跳空", "形态", "分离上一日收盘到当日开盘的隔夜价格变化。"),
    "intraday-strength": ("日内强弱", "形态", "观察开盘到收盘的价格强弱。"),
    "volume-shock-20": ("20日成交量异动", "量价", "比较当日成交量与过去20日平均水平。"),
    "return-volatility-20": ("20日收益波动", "波动", "衡量过去20日收益率的波动程度。"),
    "amihud-illiquidity-20": ("20日非流动性", "流动性", "衡量单位成交金额对应的价格波动，数值越高越难交易。"),
    "book-to-price": ("账面市值比", "估值", "市净率的倒数，用于观察账面价值相对市场价格的便宜程度。"),
    "earnings-yield": ("盈利收益率", "估值", "市盈率的倒数，用于观察盈利相对市场价格的水平。"),
    "log-size": ("对数市值", "风格", "用总市值的对数描述大盘与小盘风格。"),
    "roe-pit": ("时点可见净资产收益率", "质量", "只使用当时已经披露的ROE，观察企业盈利质量。"),
    "debt-to-assets-pit": ("时点可见资产负债率", "质量", "只使用当时已经披露的资产负债率，观察财务杠杆。"),
    "wq-alpha101-reproduction": (
        "WorldQuant Alpha101复现",
        "形态",
        "复现公开Alpha#101公式，用于验证外部因子接入流程。",
    ),
}


def _release_index(project_root: Path) -> dict[tuple[str, str], list[dict[str, Any]]]:
    index: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for path in sorted((project_root / "data" / "factor_store" / "releases").glob("*/manifest.json")):
        payload = json.loads(path.read_bytes())
        request = payload["request"]
        for factor in request["factors"]:
            index.setdefault((factor["factor_id"], factor["factor_version"]), []).append(
                {
                    "release_id": payload["release_id"],
                    "start": request["start"],
                    "end": request["end"],
                    "row_count": factor.get("row_count"),
                    "present_count": factor.get("present_count"),
                    "coverage": factor.get("coverage"),
                }
            )
    for releases in index.values():
        releases.sort(key=lambda item: (item["end"], item["release_id"]), reverse=True)
    return index


def _latest_explorer_factors(project_root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    candidates = sorted(
        (project_root / "reports" / "factor_explorer").glob("*/evidence-summary.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    latest_by_factor: dict[tuple[str, str], dict[str, Any]] = {}
    for path in candidates:
        payload = json.loads(path.read_bytes())
        raw = [item for item in payload.get("factors", []) if item.get("variant") == "RAW"]
        for item in raw:
            key = (item["factor_id"], item["factor_version"])
            # Reports are newest-first, so retain each factor's own latest result.
            # A later run of a different factor must not hide earlier M4 evidence.
            latest_by_factor.setdefault(key, item)
    return latest_by_factor


def _mean(values: list[float]) -> float | None:
    finite = [value for value in values if math.isfinite(value)]
    return sum(finite) / len(finite) if finite else None


def _result_summary(item: dict[str, Any]) -> dict[str, Any]:
    folds = item.get("folds") or []
    rank_ic = _mean(
        [
            float(fold["test_mean_rank_ic_directed"])
            for fold in folds
            if fold.get("test_mean_rank_ic_directed") is not None
        ]
    )
    supported = sum(
        fold.get("hac_direction_outcome") == "DIRECTION_SUPPORTED"
        or fold.get("bootstrap_direction_outcome") == "DIRECTION_SUPPORTED"
        for fold in folds
    )
    contradicted = sum(
        fold.get("hac_direction_outcome") == "DIRECTION_CONTRADICTED"
        or fold.get("bootstrap_direction_outcome") == "DIRECTION_CONTRADICTED"
        for fold in folds
    )
    scenarios = (item.get("execution") or {}).get("capital_scenarios") or []
    preferred = next(
        (row for row in scenarios if row.get("capital_cny") == 10_000_000),
        scenarios[0] if scenarios else None,
    )
    routes = item.get("routes") or []
    if "DIRECTION_CONTRADICTED" in routes:
        conclusion, tone = "方向与原预期相反，需反转或复核", "warning"
    elif supported:
        conclusion, tone = "已有支持证据，仍需新样本确认", "positive"
    else:
        conclusion, tone = "暂未形成稳定方向结论", "neutral"
    return {
        "mean_test_rank_ic": rank_ic,
        "supported_folds": supported,
        "contradicted_folds": contradicted,
        "fold_count": len(folds),
        "fill_rate_10m": preferred.get("fill_rate") if preferred else None,
        "net_return_10m": preferred.get("average_daily_net_return") if preferred else None,
        "conclusion": conclusion,
        "tone": tone,
        "routes": routes,
    }


def _status(published: list[dict[str, Any]], result: dict[str, Any] | None) -> str:
    return "M4_COMPLETE" if result else "CALCULATED" if published else "NOT_CALCULATED"


def _dynamic_fields(
    published: list[dict[str, Any]], result: dict[str, Any] | None, *, alpha158: bool
) -> dict[str, Any]:
    status = _status(published, result)
    label = {
        "M4_COMPLETE": "M4 已完成",
        "CALCULATED": "已计算，待完整 M4",
        "NOT_CALCULATED": "已接入，未计算" if alpha158 else "未计算",
    }[status]
    return {
        "status": status,
        "status_label": label,
        "calculated": bool(published),
        "m4_completed": result is not None,
        "latest_release_id": published[0]["release_id"] if published else None,
        "release_count": len(published),
        "coverage": published[0] if published else None,
        "result": _result_summary(result) if result else None,
    }


def build_factor_catalog_overview(project_root: Path) -> list[dict[str, Any]]:
    releases = _release_index(project_root)
    evidence = _latest_explorer_factors(project_root)
    items: list[dict[str, Any]] = []
    for cataloged in m4_2_factor_entries():
        spec = cataloged.spec
        chinese_name, category, description = CURRENT_LOCALIZATION[spec.factor_id]
        key = (spec.factor_id, spec.factor_version)
        published, result = releases.get(key, []), evidence.get(key)
        items.append(
            {
                "factor_id": spec.factor_id,
                "external_name": None,
                "factor_version": spec.factor_version,
                "chinese_name": chinese_name,
                "english_name": spec.name,
                "category": category,
                "family": cataloged.family,
                "description": description,
                "formula": spec.expression.formula if spec.expression else None,
                "required_fields": list(spec.required_fields),
                "source_collection": "CURRENT",
                "source_label": "现有机制因子",
                **_dynamic_fields(published, result, alpha158=False),
            }
        )
    for factor in alpha158_catalog():
        key = (factor.factor_id, factor.factor_version)
        published, result = releases.get(key, []), evidence.get(key)
        items.append(
            {
                "factor_id": factor.factor_id,
                "external_name": factor.external_name,
                "factor_version": factor.factor_version,
                "chinese_name": factor.chinese_name,
                "english_name": f"Qlib Alpha158 {factor.external_name}",
                "category": factor.category,
                "family": factor.family,
                "description": factor.description,
                "formula": factor.formula,
                "required_fields": list(factor.required_fields),
                "window_sessions": factor.window_sessions,
                "source_collection": "ALPHA158",
                "source_label": "Microsoft Qlib Alpha158",
                **_dynamic_fields(published, result, alpha158=True),
            }
        )
    return items


def query_factor_catalog(
    items: list[dict[str, Any]],
    *,
    page: int = 1,
    page_size: int = 36,
    query: str = "",
    category: str = "全部",
    source: Literal["ALL", "CURRENT", "ALPHA158"] = "ALL",
    status: Literal["ALL", "M4_COMPLETE", "CALCULATED", "NOT_CALCULATED"] = "ALL",
    sort_by: Literal["category", "name", "status", "factor_id"] = "category",
    sort_order: Literal["asc", "desc"] = "asc",
) -> dict[str, Any]:
    if page < 1 or page_size < 1 or page_size > 100:
        raise ValueError("page must be positive and pageSize must be between 1 and 100")
    if category != "全部" and category not in CATEGORY_ORDER:
        raise ValueError("unknown factor category")
    term = query.strip().casefold()
    facet_items = [
        item
        for item in items
        if (source == "ALL" or item["source_collection"] == source)
        and (status == "ALL" or item["status"] == status)
        and (
            not term
            or term
            in " ".join(
                str(item.get(key) or "")
                for key in ("factor_id", "external_name", "chinese_name", "english_name", "description")
            ).casefold()
        )
    ]
    filtered = [item for item in facet_items if category == "全部" or item["category"] == category]
    category_rank = {name: position for position, name in enumerate(CATEGORY_ORDER)}
    key_functions = {
        "category": lambda item: (category_rank[item["category"]], item["chinese_name"], item["factor_id"]),
        "name": lambda item: (item["chinese_name"], item["factor_id"]),
        "status": lambda item: (STATUS_ORDER[item["status"]], item["factor_id"]),
        "factor_id": lambda item: (item["factor_id"],),
    }
    filtered.sort(key=key_functions[sort_by], reverse=sort_order == "desc")
    total = len(filtered)
    start = (page - 1) * page_size
    counts = {
        "total": len(items),
        "calculated": sum(item["calculated"] for item in items),
        "m4_completed": sum(item["m4_completed"] for item in items),
        "not_calculated": sum(not item["calculated"] for item in items),
        "current": sum(item["source_collection"] == "CURRENT" for item in items),
        "alpha158": sum(item["source_collection"] == "ALPHA158" for item in items),
    }
    categories = {"全部": len(facet_items)} | {
        name: sum(item["category"] == name for item in facet_items) for name in CATEGORY_ORDER
    }
    return {
        "items": filtered[start : start + page_size],
        "page": page,
        "pageSize": page_size,
        "totalItems": total,
        "totalPages": math.ceil(total / page_size) if total else 0,
        "counts": counts,
        "categories": categories,
    }
