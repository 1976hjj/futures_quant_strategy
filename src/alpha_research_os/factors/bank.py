"""Bank-specific research factors and feature-only, disclosure-time calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from statistics import median, pstdev

from alpha_research_os.factors.catalog import (
    FactorCatalog,
    FactorCatalogEntry,
    FactorLifecycle,
    FactorSource,
    FactorSourceKind,
)
from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import (
    DataDomain,
    FactorDirection,
    FactorSpec,
    ImplementationType,
    SignalCutoff,
)

ENGINE_VERSION = "bank-disclosure-feature-1.0.0"
MAX_REPORT_AGE_DAYS = 550


@dataclass(frozen=True)
class BankFactor:
    factor_id: str
    chinese_name: str
    category: str
    field: str
    formula: str
    description: str
    expected_direction: str = "HIGH"
    factor_version: str = "1.0.0"


def bank_factor_catalog() -> tuple[BankFactor, ...]:
    return (
        BankFactor(
            "bank-pb-daily", "银行·日PB", "估值", "pb_daily", "archived_daily_basic.pb",
            "复用已有日估值PB及原始归档血缘；2016年起携带上市后历史；与普通股账面市值比口径分别保存。",
            "LOW",
        ),
        BankFactor(
            "bank-cash-dividend-yield-365",
            "银行·已实施现金股息率",
            "估值",
            "cash_yield365",
            "cash_dividend_per_current_share_365 / raw_close",
            "近365天已除权实施的税前现金股息／原始收盘价；按已知送转折算，不使用未来拟派息。",
        ),
        BankFactor(
            "bank-earnings-yield-annual",
            "银行·年度盈利收益率",
            "估值",
            "annual_earnings_yield",
            "known_annual_ordinary_eps_normalized / raw_close",
            "最近已公开年度普通股基本EPS／原始价格；不是TTM，送转后折算，非送转除权后暂停旧每股锚。",
        ),
        BankFactor(
            "bank-book-to-price-pit",
            "银行·普通股账面市值比",
            "估值",
            "book_to_price",
            "known_common_bvps_normalized / raw_close",
            "已公开普通股每股净资产／价格；不是供应商最新修订PB的历史倒填，非送转除权后暂停旧锚。",
        ),
        BankFactor(
            "bank-roe-pit",
            "银行·年度加权ROE",
            "质量",
            "roe_weighted",
            "known_annual_weighted_roe_pct",
            "仅使用已公开年度加权ROE，百分数单位；不混用季度累计ROE。",
        ),
        BankFactor(
            "bank-npl-ratio-pit",
            "银行·不良贷款率",
            "质量",
            "npl_ratio",
            "known_npl_ratio_pct",
            "当时可见不良贷款率，低值优先；缺失不等于无不良。",
            "LOW",
        ),
        BankFactor(
            "bank-provision-coverage-pit",
            "银行·拨备覆盖率",
            "质量",
            "provision_coverage_ratio",
            "known_provision_coverage_pct",
            "当时可见拨备覆盖率；高值是缓冲候选，不代表必然高收益。",
        ),
        BankFactor(
            "bank-cet1-pit",
            "银行·核心一级资本充足率",
            "质量",
            "cet1_ratio",
            "known_cet1_pct",
            "资本水平候选；监管方法和集团范围变化仍需独立检查，不当作统一资本超额缓冲。",
        ),
        BankFactor(
            "bank-npl-improvement-yoy",
            "银行·不良率同比改善",
            "质量",
            "npl_improvement",
            "same_period_prior_npl_pct - current_npl_pct",
            "同报告期、同可比定义的不良率同比下降，单位为百分点。",
        ),
        BankFactor(
            "bank-nim-change-yoy",
            "银行·净息差同比变化",
            "质量",
            "nim_change",
            "current_nim_pct - same_period_prior_nim_pct",
            "同报告期净息差变化，单位为百分点；不用相邻季度累计值相减。",
        ),
        BankFactor(
            "bank-profit-growth-yoy",
            "银行·归母利润同比",
            "质量",
            "profit_growth",
            "current_parent_profit / comparable_prior_parent_profit - 1",
            "优先同份报告的同比口径；缺失时才用当时已知同口径同期利润。",
        ),
        BankFactor(
            "bank-roe-median-3y",
            "银行·三年ROE中位数",
            "质量",
            "roe_median3",
            "median(three_consecutive_known_annual_roe_pct)",
            "连续三个财年、均已公开的年度ROE中位数；不是交易日重复财报的滚动均值。",
        ),
        BankFactor(
            "bank-roe-volatility-3y",
            "银行·三年ROE波动",
            "质量",
            "roe_volatility3",
            "population_std(three_consecutive_known_annual_roe_pct)",
            "连续三个财年ROE标准差，低值优先；应结合盈利水平，低且稳定不等于优质。",
            "LOW",
        ),
        BankFactor(
            "bank-quality-score-pit",
            "银行·规则质量分",
            "质量",
            "quality_score",
            "100 * (roe_points + npl_points + provision_points) / 52",
            "原研究质量分：ROE、不良率和拨备档位合计52分，再转100分。资本/恶化检查在门槛中。",
        ),
        BankFactor(
            "bank-quality-gate-pit",
            "银行·质量门槛",
            "质量",
            "quality_gate",
            "core_known AND Q>=75 AND ROE>=8 AND NPL<=2 AND PCR>=150 AND no_known_veto",
            "筛选诊断：通过=1，明确不通过=0，核心缺失=空。非收益排序信号；可选项未知另列标记。",
        ),
        BankFactor(
            "bank-quality-dividend-yield-365",
            "银行·质量门槛后股息率",
            "估值",
            "gated_cash_yield365",
            "cash_yield365 if quality_gate == 1 else missing",
            "原研究结构的组合对照：门槛合格后按未封顶现金股息率排序；不合格留空，不以0参与排名。",
        ),
    )


def bank_catalog(factor_id: str | None = None) -> FactorCatalog:
    catalog = FactorCatalog()
    source = FactorSource(
        source_id="bank-mechanism-research-v1",
        kind=FactorSourceKind.INTERNAL_HYPOTHESIS,
        title="银行行业机制研究与已披露财务公式",
        license_note="Project-owned hypotheses.",
        formula_verified_against_primary_source=True,
    )
    for item in bank_factor_catalog():
        if factor_id is not None and item.factor_id != factor_id:
            continue
        spec = FactorSpec(
            factor_id=item.factor_id,
            factor_version=item.factor_version,
            name=item.chinese_name,
            author="alpha-research-os",
            source=source.source_id,
            economic_hypothesis=item.description,
            expected_mechanism=item.formula,
            implementation_type=ImplementationType.PYTHON,
            python_entrypoint="scripts.publish_bank_factor:publish",
            required_fields=(item.field,),
            data_domains=(DataDomain.FUNDAMENTAL, DataDomain.MARKET, DataDomain.CORPORATE_ACTION),
            lookback_sessions=1,
            warmup_sessions=0,
            signal_cutoff=SignalCutoff.POST_CLOSE,
            missing_value_policy="propagate; non-banks absent; gate failures missing in gated yield",
            infinite_value_policy="to_missing",
            outlier_policy="raw_then_cross_section_pipeline",
            allowed_universe_ids=("ALL-A-PIT",),
            direction=FactorDirection.POSITIVE if item.expected_direction == "HIGH" else FactorDirection.NEGATIVE,
            implementation_hash=content_hash(
                {"engine": ENGINE_VERSION, "formula": item.formula, "report_age": MAX_REPORT_AGE_DAYS}
            ),
            generation_process="Bank hypotheses frozen before new evaluation; no label access; bank-masked PIT scope.",
            test_references=("bank-factor-disclosure-and-event-golden",),
        )
        catalog.register(
            FactorCatalogEntry(
                spec=spec,
                family="bank-industry",
                source_reference=source,
                adaptation_notes=item.description,
                lifecycle=FactorLifecycle.RESEARCH_ONLY,
            )
        )
    if factor_id is not None and not catalog.list():
        raise ValueError(f"unknown bank factor: {factor_id}")
    return catalog


def finite(value: object) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def quality_score(roe: float | None, npl: float | None, pcr: float | None) -> float | None:
    if not all(finite(x) for x in (roe, npl, pcr)):
        return None
    a = 28 if roe >= 11 else 24 if roe >= 9.5 else 20 if roe >= 8 else 10 if roe >= 6 else 2
    b = 14 if npl <= 1.2 else 12 if npl <= 1.6 else 8 if npl <= 2 else 2
    c = 10 if pcr >= 250 else 8 if pcr >= 180 else 5 if pcr >= 150 else 1
    return (a + b + c) / 52 * 100


def quality_gate(roe, npl, pcr, *, npl_change=None, cet1=None, profit_growth=None):
    q = quality_score(roe, npl, pcr)
    if q is None:
        return None
    return float(
        q >= 75
        and roe >= 8
        and npl <= 2
        and pcr >= 150
        and not (finite(npl_change) and npl_change > 0.2 + 1e-10)
        and not (finite(cet1) and cet1 < 8.5)
        and not (finite(profit_growth) and profit_growth < -0.05)
    )


def roe_history_statistics(values: list[tuple[date, float]], latest: date):
    years = {d.year: v for d, v in values if d.month == 12 and d.day == 31 and finite(v)}
    wanted = [latest.year - 2, latest.year - 1, latest.year]
    if any(year not in years for year in wanted):
        return None, None
    sample = [years[year] for year in wanted]
    return float(median(sample)), float(pstdev(sample))


def cash_per_current_share(events: list[dict], session: date, archive_complete: bool = True):
    """Known implemented dividends; free distributions normalize cash, never add it twice."""
    if not archive_complete:
        return None
    known = [
        e
        for e in events
        if e.get("ex_date") is not None
        and e["ex_date"] <= session
        and e.get("imp_ann_date") is not None
        and e["imp_ann_date"] < session
    ]
    total = 0.0
    for event in known:
        if not 0 <= (session - event["ex_date"]).days < 365:
            continue
        if not finite(event.get("cash_div_tax")) or event["cash_div_tax"] < 0:
            return None
        if not finite(event.get("stk_div")) or event["stk_div"] < 0:
            return None
        # cash_div_tax is per old share, including when this dividend also gives stock.
        scale = 1.0 + event["stk_div"]
        for later in known:
            if later["ex_date"] <= event["ex_date"]:
                continue
            if not finite(later.get("stk_div")) or later["stk_div"] < 0:
                return None
            scale *= 1 + later["stk_div"]
        total += event["cash_div_tax"] / scale
    return total
