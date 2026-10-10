"""Definition-only, date-level bank allocation indicators; no data or label access."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

SOURCE = "BANK_TIMING"
VERSION = "1.0.0"
RESEARCH_ROUTE = "BANK_SECTOR_ALLOCATION"


@dataclass(frozen=True)
class BankTimingIndicator:
    factor_id: str
    chinese_name: str
    category: str
    research_batch: int
    formula: str
    description: str
    required_fields: tuple[str, ...]
    output_components: tuple[str, ...]
    expected_direction: str
    parameters: tuple[tuple[str, Any], ...] = ()
    dependency_note: str = "复用银行时点可见数据；历史版本认证与字段覆盖须在计算前核验。"
    factor_version: str = VERSION


def bank_timing_catalog() -> tuple[BankTimingIndicator, ...]:
    history = (("lookback_sessions", 756), ("minimum_history_sessions", 504))
    joint = history + (("pb_percentile_ceiling", 0.20), ("yield_percentile_floor", 0.80))
    items = (
        BankTimingIndicator(
            "bank-sector-pb-history-percentile", "银行板块·PB历史分位", "估值", 1,
            "mean_i(time_percentile_756(1 / book_to_price_i)); companion=median_i",
            "先计算各银行相对自身过去三年PB的分位，再取板块均值与中位数；低值表示历史相对便宜。",
            ("book_to_price",), ("mean", "median"), "LOW", history,
        ),
        BankTimingIndicator(
            "bank-sector-dividend-history-percentile", "银行板块·股息率历史分位", "估值", 1,
            "mean_i(time_percentile_756(cash_yield365_i)); companion=median_i",
            "使用近365天已实施税前现金股息率，各股先算自身历史分位；不混用供应商TTM或拟派息。",
            ("cash_yield365",), ("mean", "median"), "HIGH", history,
        ),
        BankTimingIndicator(
            "bank-sector-cheap-high-yield-breadth", "银行板块·低估高息联合占比", "估值", 1,
            "count(pb_percentile <= .20 AND yield_percentile >= .80) / count(both_valid)",
            "衡量低PB、高股息率同时出现的普遍程度；20%与80%是待验证的研究默认值。",
            ("book_to_price", "cash_yield365"), ("breadth",), "HIGH", joint,
        ),
        BankTimingIndicator(
            "bank-sector-quality-cheap-high-yield-breadth", "银行板块·质量合格低估高息占比", "估值", 1,
            "count(quality_gate == 1 AND cheap_high_yield) / count(quality_gate == 1 AND both_valid)",
            "分母仅包含已有质量门槛合格且估值双项有效的银行；同时报告质量合格覆盖率，不把未知当不合格。",
            ("book_to_price", "cash_yield365", "quality_gate"), ("breadth", "quality_eligible_count"),
            "HIGH", joint,
        ),
        BankTimingIndicator(
            "bank-sector-nim-deterioration-breadth", "银行板块·净息差恶化覆盖面", "质量", 1,
            "count(nim_change < 0) / count(nim_change_valid); companion=median_i(nim_change)",
            "采用已披露、同报告期且同口径净息差同比变化；低覆盖面表示恶化较少，变化幅度单位为百分点。",
            ("nim_change",), ("breadth", "median_change_pp"), "LOW",
        ),
        BankTimingIndicator(
            "bank-sector-trend-breadth", "银行板块·中期趋势覆盖面", "动量", 1,
            "count(pit_total_return_index_i > SMA_120(pit_total_return_index_i)) / count(trend_valid)",
            "观察站上120日均线的银行比例；使用当时可构建的含分红收益序列，排除除息造成的机械跌破。",
            ("bank_total_return_index",), ("breadth",), "HIGH", (("trend_sessions", 120),),
            "由原始行情及已知公司行为构建含分红序列；未解释调整不参与，不能直接用原始收盘价。",
        ),
        BankTimingIndicator(
            "bank-sector-relative-strength", "银行板块·相对宽基强弱", "风格", 1,
            "bank_equal_total_return_63 - broad_market_total_return_63",
            "银行等权篮子相对宽基的63交易日收益差；两边统一含分红口径，独立于银行内部个股相对动量。",
            ("bank_equal_total_return_index", "broad_market_total_return_index"),
            ("relative_return",), "HIGH", (("momentum_sessions", 63),),
            "待核对宽基含分红序列覆盖；若不足需补采或另行冻结可复现的宽基篮子定义，禁止用价格指数替代。",
        ),
        BankTimingIndicator(
            "bank-sector-valuation-dispersion", "银行板块·估值分化程度", "风格", 2,
            "Q75_i(pb_percentile) - Q25_i(pb_percentile); companion=IQR_i(yield_percentile)",
            "观察个股自身历史估值位置的离散程度；按当时可知银行类型作附加拆分，方向仅用于诊断。",
            ("book_to_price", "cash_yield365"), ("pb_iqr", "yield_iqr"), "DIAGNOSTIC", history,
            "总体分化复用估值数据；银行类型拆分需可追溯历史分类，缺失时留空而不以当前分类回填。",
        ),
        BankTimingIndicator(
            "bank-sector-operating-deterioration", "银行板块·综合经营恶化指标", "质量", 2,
            "mean(breadth(profit_growth < 0), breadth(nim_change < 0), breadth(npl_improvement < 0), "
            "breadth(provision_change_yoy < 0), breadth(cet1_change_yoy < 0))",
            "五项恶化占比等权汇总，并保留各分项；各自按有效样本计算，任一分项覆盖不足则总指标留空。",
            ("profit_growth", "nim_change", "npl_improvement", "provision_change_yoy", "cet1_change_yoy"),
            ("score", "profit_breadth", "nim_breadth", "npl_breadth", "provision_breadth", "cet1_breadth"),
            "LOW", (("component_weights", "equal"),),
            "拨备、资本同比变化需由同报告期、同单位、同口径的已知财务事实派生，不能用交易日252日差替代。",
        ),
        BankTimingIndicator(
            "bank-sector-earnings-yield-history-percentile", "银行板块·年度盈利收益率历史分位", "估值", 2,
            "mean_i(time_percentile_756(annual_earnings_yield_i)); companion=median_i",
            "使用既有年度普通股盈利收益率，作为PB与股息率的对照；年度口径明确区别于PE_TTM。",
            ("annual_earnings_yield",), ("mean", "median"), "HIGH", history,
        ),
    )


    return tuple(replace(item, factor_version="2.0.0",
                         required_fields=tuple("pb_daily" if field == "book_to_price" else field
                                               for field in item.required_fields),
                         formula=item.formula.replace("1 / book_to_price_i", "pb_daily_i"),
                         dependency_note=(item.dependency_note
                                          + " PB复用已归档日估值，预热从2016年或上市后开始；普通股账面市值比另列。"))
                 if "book_to_price" in item.required_fields else item for item in items)


def is_bank_timing_indicator(factor_id: str) -> bool:
    return any(item.factor_id == factor_id for item in bank_timing_catalog())


def bank_timing_overview() -> list[dict[str, Any]]:
    """Expose definitions only: registration never materializes or certifies values."""
    return [
        {
            "factor_id": item.factor_id,
            "factor_version": item.factor_version,
            "external_name": None,
            "chinese_name": item.chinese_name,
            "english_name": item.factor_id,
            "category": item.category,
            "family": "bank-sector-allocation",
            "description": item.description,
            "formula": item.formula,
            "required_fields": list(item.required_fields),
            "source_collection": SOURCE,
            "source_label": "银行板块择时指标",
            "expected_direction": item.expected_direction,
            "observation_level": "SECTOR",
            "research_batch": item.research_batch,
            "output_components": list(item.output_components),
            "parameters": {
                "minimum_valid_banks": 10,
                "minimum_coverage": 0.70,
                **dict(item.parameters),
            },
            "definition_status": "RESEARCH_ONLY",
            "compute_supported": True,
            "m4_supported": False,
            "research_route": RESEARCH_ROUTE,
            "dependency_note": item.dependency_note,
            "research_scope": "每天一组板块观测，控制银行额度使用率；不参与个股横截面排名。",
            "coverage_policy": "分项独立报告有效数、当日历史股票池数及覆盖率；缺失不作零。质量条件占比另报合格池覆盖。",
            "percentile_policy": "仅用截至当日的有效历史，(小于当日值的数量+0.5*等于的数量)/有效数量；PB须为正。",
            "signal_cutoff": "POST_CLOSE",
            "execution_policy": "使用收盘信息的信号最早于下一合法交易事件执行。",
            "status": "NOT_CALCULATED",
            "status_label": "已接入，未计算",
            "calculated": False,
            "m4_completed": False,
            "latest_release_id": None,
            "release_count": 0,
            "coverage": None,
            "result": None,
            "accuracy_status": None,
            "accuracy_error": None,
        }
        for item in bank_timing_catalog()
    ]
