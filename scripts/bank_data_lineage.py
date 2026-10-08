"""Field-level provenance for mixed bank data; no acquisition and no financial-value changes."""

from __future__ import annotations

import json
import math

import pandas as pd

FREE_METRICS = (
    "npl_ratio",
    "provision_coverage_ratio",
    "cet1_ratio",
    "capital_adequacy_ratio",
    "net_interest_margin",
    "tier1_capital_ratio",
    "net_interest_spread",
    "loan_provision_ratio",
    "loans_gross",
    "deposits_total",
    "loans_advances",
    "npl_amount",
    "overdue_loan_amount",
    "vendor_bvps",
)


def metric_unit(metric: str) -> str:
    if metric in FREE_METRICS[:8] or metric.endswith("_pct"):
        return "percent"
    if metric in ("vendor_bvps", "selected_bvps", "tushare_bps", "tushare_eps"):
        return "CNY/share"
    if metric == "tushare_report_shares":
        return "shares"
    return "CNY"


def metric_scope(metric: str) -> str:
    if metric in ("loans_gross", "npl_amount", "npl_ratio", "loan_provision_ratio", "provision_coverage_ratio"):
        return "regulatory_loan_scope;provider_basis_requires_review"
    if metric in ("cet1_ratio", "tier1_capital_ratio", "capital_adequacy_ratio"):
        return "regulatory_capital;method_and_group_scope_require_review"
    if metric in ("net_interest_margin", "net_interest_spread"):
        return "year_to_date_annualized;not_single_quarter;provider_basis_requires_review"
    if metric in ("loans_advances", "tushare_loans_advances_carrying"):
        return "consolidated_net_loans_carrying;not_gross_principal"
    if metric.startswith("tushare_"):
        if metric in (
            "tushare_parent_profit",
            "tushare_revenue",
            "tushare_interest_income",
            "tushare_interest_expense",
            "tushare_admin_expense",
            "tushare_operating_cashflow",
        ):
            return "consolidated_year_to_date;not_single_quarter"
        return "consolidated_reported_value;current_revision"
    return "provider_reported_value;basis_requires_review"


def present(value) -> bool:
    return value is not None and not pd.isna(value)


def add_lineage(panel: pd.DataFrame, token_evidence: dict | None = None, free_evidence: dict | None = None):
    """Attach exact field origin; supplemental details take precedence over row origin."""
    panel = panel.copy()
    token_evidence = token_evidence or {}
    free_evidence = free_evidence or {}
    metrics = [m for m in FREE_METRICS if m in panel]
    metrics += [
        m
        for m in panel
        if m.startswith("tushare_") and not m.endswith("_available_at") and pd.api.types.is_numeric_dtype(panel[m])
    ]
    records = []
    source_columns = {metric: [] for metric in metrics + ["selected_bvps"] if metric in panel}
    unit_columns = {metric: metric_unit(metric) for metric in source_columns}
    lineage_json = []
    for _, row in panel.iterrows():
        details = row.get("supplement_details")
        details = json.loads(details) if isinstance(details, str) and details else []
        supplemental = {d["metric"]: d for d in (details or [])}
        lineage = {}
        for metric in metrics:
            value = row[metric]
            source = None
            if present(value):
                base = {
                    "source_id": row.get("source_id"),
                    "source_sha256": row.get("raw_sha256"),
                    "available_at": row.get("vendor_available_at", row.get("available_at")),
                    "retrieved_at": row.get("retrieved_at_utc"),
                    "pit_grade": row.get("pit_grade"),
                    "source_file": None,
                    "source_url": None,
                    "pdf_page": None,
                    "report_scope": metric_scope(metric),
                    "first_disclosure_verified": False,
                }
                base.update(free_evidence.get(row.get("raw_sha256"), {}))
                if metric in supplemental:
                    base.update(supplemental[metric])
                    if not math.isclose(float(base["value"]), float(value), rel_tol=1e-10, abs_tol=1e-8):
                        raise ValueError(f"Supplement lineage value mismatch: {row.code} {row.report_date} {metric}")
                    base["report_scope"] = base.get("method") or metric_scope(metric)
                elif metric.startswith("tushare_"):
                    base.update(
                        {
                            "source_id": "tushare_compatible_token",
                            "source_url": None,
                            "source_sha256": None,
                            "retrieved_at": None,
                            "available_at": row.get("token_available_at"),
                            "pit_grade": "current_revision_first_disclosure_unverified",
                        }
                    )
                    base.update(token_evidence.get((row.code, row.report_date, metric), {}))
                    # Announcement dates describe the revision; historical availability remains
                    # conservative until the originally disclosed value/version is certified.
                    base["available_at"] = base.get("retrieved_at")
                source = base["source_id"]
                if not present(source):
                    raise ValueError(f"Missing field source: {row.code} {row.report_date} {metric}")
                base = {
                    k: (v if present(v) else None)
                    for k, v in base.items()
                    if k not in {"value", "metric", "code", "report_date", "normalized_value", "normalized_unit"}
                }
                base.update(unit=metric_unit(metric), value=float(value))
                lineage[metric] = base
            source_columns[metric].append(source)
        if "selected_bvps" in panel:
            metric = "selected_bvps"
            value = row[metric]
            origin = "tushare_bps" if present(row.get("tushare_bps")) else "vendor_bvps"
            selected = dict(lineage[origin]) if present(value) else None
            if selected:
                if not math.isclose(selected["value"], float(value), rel_tol=1e-10, abs_tol=1e-8):
                    raise ValueError("Selected BPS differs from origin")
                selected["selected_from_metric"] = origin
                lineage[metric] = selected
            source_columns[metric].append(selected["source_id"] if selected else None)
        lineage_json.append(json.dumps(lineage, ensure_ascii=False, allow_nan=False))
        for metric, evidence in lineage.items():
            normalized_unit = "shares" if evidence["unit"] == "10k_shares" else evidence["unit"]
            records.append(
                {
                    "code": row.code,
                    "report_date": row.report_date,
                    "metric": metric,
                    **evidence,
                    "normalized_value": evidence["value"] * (10000 if evidence["unit"] == "10k_shares" else 1),
                    "normalized_unit": normalized_unit,
                    "historical_pit_verified": False,
                }
            )
    for metric, values in source_columns.items():
        panel[metric + "_source"] = values
        panel[metric + "_unit"] = unit_columns[metric]
    panel["field_lineage"] = lineage_json
    panel["row_source_role"] = "source_id is base free record only; use field source for mixed values"
    # Preserve vendor/direct values; compare rather than coalescing incompatible BPS bases.
    if "tushare_bps" in panel and "vendor_bvps" in panel:
        panel["bps_cross_source_difference"] = panel.tushare_bps - panel.vendor_bvps
        panel["bps_scope_status"] = [
            "different_values_basis_review_required"
            if present(r.tushare_bps) and present(r.vendor_bvps) and abs(r.tushare_bps - r.vendor_bvps) > 0.01
            else "within_0.01_or_single_source_not_basis_certified"
            for r in panel.itertuples()
        ]
    required = ["tushare_parent_equity", "tushare_other_equity_tools", "tushare_report_shares"]
    if all(c in panel for c in required):
        valid = panel[required].notna().all(axis=1) & (panel.tushare_report_shares > 0)
        panel["valuation_common_bvps"] = (
            (panel.tushare_parent_equity - panel.tushare_other_equity_tools) / panel.tushare_report_shares
        ).where(valid)
        panel["valuation_common_bvps_unit"] = "CNY/share"
        panel["valuation_common_bvps_source"] = "derived_from_tushare_balancesheet"
        panel["valuation_common_bvps_formula"] = "(parent_equity-other_equity_tools)/report_shares_in_shares"
        panel["valuation_common_bvps_scope"] = (
            "report_date_common_equity;adjust_later_share_events_before_price_comparison"
        )
    if "valuation_common_bvps" in panel:
        for index, row in panel.iterrows():
            if not present(row.valuation_common_bvps):
                continue
            lineage = json.loads(panel.at[index, "field_lineage"])
            inputs = [lineage[m] for m in required]
            detail = {
                "source_id": "derived_from_tushare_balancesheet",
                "source_api": "balancesheet",
                "source_field": "total_hldr_eqy_exc_min_int - oth_eqt_tools; divided by total_share",
                "source_file": inputs[0]["source_file"],
                "source_sha256": inputs[0]["source_sha256"],
                "source_url": inputs[0].get("source_url"),
                "pdf_page": None,
                "unit": "CNY/share",
                "value": float(row.valuation_common_bvps),
                "formula": row.valuation_common_bvps_formula,
                "report_scope": row.valuation_common_bvps_scope,
                "available_at": max((x["available_at"] for x in inputs if x.get("available_at")), default=None),
                "historical_pit_verified": False,
                "derived_inputs": required,
            }
            lineage["valuation_common_bvps"] = detail
            panel.at[index, "field_lineage"] = json.dumps(lineage, ensure_ascii=False)
            records.append(
                {
                    "code": row.code,
                    "report_date": row.report_date,
                    "metric": "valuation_common_bvps",
                    **detail,
                    "normalized_value": detail["value"],
                    "normalized_unit": "CNY/share",
                }
            )
    return panel, pd.DataFrame(records)


def implemented_dividend_events(frame: pd.DataFrame, cutoff):
    """One implemented economic event, never sum proposal/meeting/implementation revisions."""
    required = ["ts_code", "end_date", "ex_date", "div_proc"]
    if not all(c in frame for c in required):
        return frame.iloc[:0].copy(), []
    events = frame[frame.div_proc.eq("实施") & frame.ex_date.notna() & (frame.ex_date <= pd.Timestamp(cutoff))].copy()
    if events.empty:
        return events, []
    events["_notice"] = pd.to_datetime(events.get("imp_ann_date", events.ann_date)).fillna(events.ann_date)
    selected, conflicts = [], []
    numeric = [c for c in ("cash_div_tax", "stk_div", "stk_bo_rate", "stk_co_rate") if c in events]
    for key, rows in events.groupby(["ts_code", "end_date", "ex_date"]):
        rows = rows[rows.retrieved_at == rows.retrieved_at.max()]
        if rows._notice.notna().any():
            rows = rows[rows._notice == rows._notice.max()]
        if any(rows[col].dropna().nunique() > 1 for col in numeric):
            conflicts.append(
                {
                    "code": key[0],
                    "report_date": str(key[1].date()),
                    "ex_date": str(key[2].date()),
                    "status": "conflicting_implemented_event_do_not_sum",
                }
            )
            continue
        best = rows.iloc[rows[numeric].notna().sum(axis=1).to_numpy().argmax()].copy()
        origins = {}
        for col in numeric:
            known = rows[rows[col].notna()]
            if not known.empty:
                origin = known.iloc[-1]
                if pd.isna(best[col]):
                    best[col] = origin[col]
                origins[col] = {
                    "source_file": origin.get("source_file"),
                    "source_sha256": origin.get("source_sha256"),
                    "source_row": int(origin.source_row) if "source_row" in origin else None,
                }
        best["event_field_lineage"] = json.dumps(origins)
        selected.append(best)
    output = pd.DataFrame(selected).drop(columns=["_notice"], errors="ignore") if selected else events.iloc[:0]
    output["event_policy"] = "one implemented economic event; cash/stock values are per pre-event share"
    output["cash_div_tax_unit"] = "CNY/pre_event_share"
    return output, conflicts
