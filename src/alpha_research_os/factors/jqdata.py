"""Curated JoinQuant/JQData-style factors used by the strategy research pass.

Where a vendor formula is public it is retained as provenance.  The explicitly
marked local factors are transparent, point-in-time reproductions: their names
describe the economic signal, not a claim of byte-for-byte vendor equivalence.
"""

from __future__ import annotations

from typing import Literal

from pydantic import HttpUrl

from alpha_research_os.factors.alpha158 import FactorCategory
from alpha_research_os.kernel.specs import FrozenSpec

JQDATA_FACTOR_SOURCE = "https://www.joinquant.com/view/factorlib/list"


class JQDataCatalogItem(FrozenSpec):
    factor_id: str
    external_name: str
    factor_version: str = "jqdata-factorlib-1"
    chinese_name: str
    category: FactorCategory
    family: str
    formula: str
    description: str
    required_fields: tuple[str, ...]
    expected_direction: Literal["HIGH", "LOW"]
    source_id: str = "joinquant-jqdata-factorlib"
    source_uri: HttpUrl = JQDATA_FACTOR_SOURCE
    formula_verified_against_primary_source: bool = True
    catalog_status: Literal["CATALOGED_NOT_CALCULATED"] = "CATALOGED_NOT_CALCULATED"


def jqdata_catalog() -> tuple[JQDataCatalogItem, ...]:
    """Return selected JQData definitions and their local replacement candidates."""

    base = (
        JQDataCatalogItem(
            factor_id="jqdata-cash-earnings-to-price-ratio",
            external_name="cash_earnings_to_price_ratio",
            factor_version="jqdata-factorlib-2",
            chinese_name="现金流量市值比",
            category="估值",
            family="jqdata-valuation",
            formula="过去一年的净经营现金流 / 当前股票市值",
            description="衡量经营现金流相对公司市值的充足程度；不要与 1 / P/CF 的“现金流市值比”混用。",
            required_fields=("operating_cashflow", "total_mv"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-earnings-to-price-ratio",
            external_name="earnings_to_price_ratio",
            factor_version="jqdata-factorlib-2",
            chinese_name="利润市值比（TTM盈利收益率）",
            category="估值",
            family="jqdata-valuation",
            formula="过去一年的归母净利润 / 当前股票市值（等于 PE_TTM 的倒数）",
            description="预期盈利收益率的本地替代项。使用公告时点可见的TTM归母净利润，不依赖分析师预测。",
            required_fields=("net_income_parent", "total_mv"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-share-turnover-monthly",
            external_name="share_turnover_monthly",
            factor_version="jqdata-factorlib-3",
            chinese_name="月换手率",
            category="流动性",
            family="jqdata-liquidity",
            formula="ln(sum(turn_over_ratio, 21个交易日))",
            description="过去 21 个交易日换手率之和的自然对数。第一版策略用它排除换手最极端的一组股票。",
            required_fields=("turnover_rate",),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-daily-standard-deviation",
            external_name="daily_standard_deviation",
            factor_version="jqdata-factorlib-3",
            chinese_name="日收益率标准差（252日指数加权）",
            category="波动",
            family="jqdata-risk",
            formula=(
                "sqrt(sum(w_t * (r_t-r_mean)^2, 252日))，"
                "w_t为半衰期42个交易日的归一化指数权重"
            ),
            description="残余波动率的本地替代项。保留长期高波动过滤能力，不依赖市场模型或外部风险库。",
            required_fields=("close", "pre_close"),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-resvol",
            external_name="resvol",
            factor_version="jqdata-factorlib-local-2",
            chinese_name="残余波动率因子",
            category="波动",
            family="jqdata-risk",
            formula="0.50 * daily_std + 0.42 * historical_resid_sigma + 0.08 * cum_range",
            description="综合日收益波动、市场模型残差波动和累计收益区间。第一版策略用它排除残余波动最高的一组股票。",
            required_fields=("adjusted_close", "close", "pre_close"),
            expected_direction="LOW",
        ),
    )
    expanded_v1 = (
        JQDataCatalogItem(
            factor_id="jqdata-book-to-price-ratio",
            external_name="book_to_price_ratio",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="账面市值比（聚宽口径）",
            category="估值",
            family="jqdata-valuation",
            formula="1 / PB",
            description="每单位市场价格对应的账面净资产，使用当日可见PB倒数。",
            required_fields=("pb",),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-cash-flow-to-price-ratio",
            external_name="cash_flow_to_price_ratio",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="净现金流市值比",
            category="估值",
            family="jqdata-valuation",
            formula="过去一年的现金及现金等价物净增加额 / 当前股票市值",
            description="使用公告时点可见的TTM净现金流衡量现金净增加额相对市值的水平。",
            required_fields=("net_cashflow", "total_mv"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-roe-ttm",
            external_name="roe_ttm",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="权益回报率TTM",
            category="质量",
            family="jqdata-quality",
            formula="归母净利润TTM / 期末归母股东权益",
            description="只使用财报公告日之后可见的利润和权益数据。",
            required_fields=("net_income_parent", "equity_parent"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-roa-ttm",
            external_name="roa_ttm",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="资产回报率TTM",
            category="质量",
            family="jqdata-quality",
            formula="归母净利润TTM / 期末总资产",
            description="使用公告时点可见的TTM利润与期末总资产衡量资产盈利能力。",
            required_fields=("net_income_parent", "total_assets"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-acca",
            external_name="ACCA",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="应计利润率ACCA",
            category="质量",
            family="jqdata-quality",
            formula="(归母净利润TTM - 经营现金流TTM) / 期末总资产",
            description="衡量利润中未被经营现金流支持的部分，通常低值代表盈利质量更好。",
            required_fields=("net_income_parent", "operating_cashflow", "total_assets"),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-adjusted-profit-to-total-profit",
            external_name="adjusted_profit_to_total_profit",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="扣非净利润与利润总额之比",
            category="质量",
            family="jqdata-quality",
            formula="扣除非经常损益后的归母净利润 / 利润总额",
            description="衡量利润对非经常性项目的依赖程度。",
            required_fields=("profit_dedt", "total_profit"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-net-operating-cash-flow-coverage",
            external_name="net_operating_cash_flow_coverage",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="净利润现金含量",
            category="质量",
            family="jqdata-quality",
            formula="经营现金流TTM / 归母净利润TTM",
            description="衡量会计利润被经营现金流覆盖的程度。",
            required_fields=("operating_cashflow", "net_income_parent"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-debt-to-equity-ratio",
            external_name="debt_to_equity_ratio",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="负债权益比",
            category="质量",
            family="jqdata-quality",
            formula="期末总负债 / 期末归母股东权益",
            description="衡量财务杠杆；跨金融与非金融行业比较时需谨慎。",
            required_fields=("total_liabilities", "equity_parent"),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-growth",
            external_name="growth",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="成长因子（本地复现）",
            category="质量",
            family="jqdata-style",
            formula="平均(营业收入同比增长率, 归母净利润同比增长率, 总资产同比增长率)",
            description="本地PIT成长复合值；与聚宽供应商风格模型可能存在差异。",
            required_fields=("or_yoy", "netprofit_yoy", "assets_yoy"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-momentum",
            external_name="momentum",
            # Version 3 corrects the full 252-session lag warmup.
            factor_version="jqdata-factorlib-local-3",
            chinese_name="中期动量（本地复现）",
            category="动量",
            family="jqdata-style",
            formula="过去252至21个交易日复权价格收益",
            description="排除最近一个月反转影响的中期动量本地复现值。",
            required_fields=("adjusted_close",),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-rank1m",
            external_name="Rank1M",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="1个月收益横截面排名",
            category="动量",
            family="jqdata-momentum",
            formula="1 - 过去20日收益的横截面升序排名 / 当日股票数",
            description="低收益股票取得更高数值，刻画一个月反转效应。",
            required_fields=("adjusted_close",),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-variance20",
            external_name="Variance20",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="20日收益方差",
            category="波动",
            family="jqdata-risk",
            formula="过去20个交易日日收益率的样本方差 * 250",
            description="20日窗口年化收益方差。",
            required_fields=("close", "pre_close"),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-sharpe-ratio-60",
            external_name="sharpe_ratio_60",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="60日夏普比率",
            category="波动",
            family="jqdata-risk",
            formula="过去60日日均收益 / 日收益标准差 * sqrt(250)",
            description="无风险利率取零的60日年化夏普比率。",
            required_fields=("close", "pre_close"),
            expected_direction="HIGH",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-beta",
            external_name="beta",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="市场贝塔（本地复现）",
            category="波动",
            family="jqdata-style",
            formula="过去252日个股收益与全A等权收益的协方差 / 市场收益方差",
            description="使用历史时点全A可投股票等权收益作为市场收益的本地贝塔。",
            required_fields=("close", "pre_close", "ALL-A-PIT"),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-atr6",
            external_name="ATR6",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="6日平均真实波幅",
            category="波动",
            family="jqdata-technical",
            formula="MA(max(H-L, abs(H-PC), abs(L-PC)), 6)",
            description="6日平均真实波幅，并除以收盘价转为可跨股票比较的比例。",
            required_fields=("high", "low", "pre_close", "close"),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-davol10",
            external_name="DAVOL10",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="10日相对换手率",
            category="流动性",
            family="jqdata-liquidity",
            formula="10日平均换手率 / 120日平均换手率 - 1",
            description="衡量短期换手相对长期常态的变化。",
            required_fields=("turnover_rate",),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-liquidity",
            external_name="liquidity",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="流动性因子（本地复现）",
            category="流动性",
            family="jqdata-style",
            formula="ln(21日平均换手率)",
            description="以一个月平均换手率对数刻画流动性的本地复现值。",
            required_fields=("turnover_rate",),
            expected_direction="LOW",
        ),
        JQDataCatalogItem(
            factor_id="jqdata-natural-log-of-market-cap",
            external_name="natural_log_of_market_cap",
            factor_version="jqdata-factorlib-local-1",
            chinese_name="对数总市值（聚宽口径）",
            category="估值",
            family="jqdata-style",
            formula="ln(总市值)",
            description="总市值自然对数；低值代表小市值。",
            required_fields=("total_mv",),
            expected_direction="LOW",
        ),
        # The following are intentionally transparent local formulas.  They are
        # representative value/quality/growth/size/liquidity/risk signals from
        # the factor-library research screen, rather than claims about an exact
        # opaque vendor implementation.
        JQDataCatalogItem(
            factor_id="jqdata-sales-to-price-ratio",
            external_name="sales_to_price_ratio",
            chinese_name="营收市值比（TTM）",
            category="估值",
            family="jqdata-valuation",
            formula="1 / PS_TTM",
            description="以当日可见的滚动市销率倒数衡量收入相对市值的便宜程度。",
            required_fields=("ps_ttm",),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-dividend-yield-ttm",
            external_name="dividend_yield_ttm",
            chinese_name="股息率（TTM）",
            category="估值",
            family="jqdata-valuation",
            formula="dv_ttm / 100",
            description="当日可见的滚动十二个月现金股息收益率；零或缺失值不作填充。",
            required_fields=("dv_ttm",),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-operating-cashflow-to-ev-ttm",
            external_name="operating_cashflow_to_ev_ttm",
            chinese_name="经营现金流企业价值比（TTM）",
            category="估值",
            family="jqdata-valuation",
            formula="经营现金流TTM / (市值 + 有息负债 - 货币资金)",
            description="以经营现金流相对企业价值衡量估值，企业价值非正时保留为空值。",
            required_fields=("operating_cashflow", "total_mv", "debt", "money_cap"),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-gross-margin-ttm",
            external_name="gross_margin_ttm",
            chinese_name="毛利率（TTM）",
            category="质量",
            family="jqdata-quality",
            formula="(营业总收入TTM - 营业成本TTM) / 营业总收入TTM",
            description="用公告日可见财报构造的滚动毛利率。",
            required_fields=("total_revenue", "oper_cost"),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-operating-margin-ttm",
            external_name="operating_margin_ttm",
            chinese_name="营业利润率（TTM）",
            category="质量",
            family="jqdata-quality",
            formula="营业利润TTM / 营业总收入TTM",
            description="经营利润相对收入的盈利质量；财报未完整时保留为空。",
            required_fields=("operate_profit", "total_revenue"),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-asset-turnover-ttm",
            external_name="asset_turnover_ttm",
            chinese_name="总资产周转率（TTM）",
            category="质量",
            family="jqdata-quality",
            formula="营业总收入TTM / 期末总资产",
            description="收入相对最近可见期末总资产的运营效率，使用期末口径以保持 PIT 可复现。",
            required_fields=("total_revenue", "total_assets"),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-operating-cashflow-to-debt",
            external_name="operating_cashflow_to_debt",
            chinese_name="经营现金流负债比（TTM）",
            category="质量",
            family="jqdata-quality",
            formula="经营现金流TTM / 期末总负债",
            description="以经营现金流覆盖全部负债的能力衡量偿债质量。",
            required_fields=("operating_cashflow", "total_liabilities"),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-current-ratio",
            external_name="current_ratio",
            chinese_name="流动比率",
            category="质量",
            family="jqdata-quality",
            formula="流动资产 / 流动负债",
            description="最近公告期的短期偿债能力；流动负债非正或缺失时保留为空。",
            required_fields=("total_cur_assets", "total_cur_liab"),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-revenue-growth-yoy",
            external_name="revenue_growth_yoy",
            chinese_name="营业收入同比增长率",
            category="质量",
            family="jqdata-growth",
            formula="or_yoy / 100",
            description="财报指标中的营业收入同比增速，按公告日期点时连接。",
            required_fields=("or_yoy",),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-nonlinear-size",
            external_name="nonlinear_size",
            chinese_name="非线性市值",
            category="风格",
            family="jqdata-style",
            formula="residual(ln(总市值)^3 ~ ln(总市值))",
            description="每日横截面对数市值三次项对对数市值回归后的残差，刻画中等市值暴露。",
            required_fields=("total_mv",),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-turnover-cv-20",
            external_name="turnover_cv_20",
            chinese_name="20日换手率相对波动率",
            category="流动性",
            family="jqdata-liquidity",
            formula="std(turnover_rate, 20) / mean(turnover_rate, 20)",
            description="完整 20 日窗口内换手率的变异系数，衡量交易活跃度的稳定性。",
            required_fields=("turnover_rate",),
            expected_direction="LOW",
            formula_verified_against_primary_source=False,
        ),
        JQDataCatalogItem(
            factor_id="jqdata-return-skewness-120",
            external_name="return_skewness_120",
            chinese_name="120日收益率偏度",
            category="波动",
            family="jqdata-risk",
            formula="skewness(日收益率, 120)",
            description="完整 120 日窗口的样本偏度，用于刻画收益分布尾部形态。",
            required_fields=("close", "pre_close"),
            expected_direction="HIGH",
            formula_verified_against_primary_source=False,
        ),
    )
    # Local reproductions are versioned immutably.  The original local set keeps
    # its released versions; this representative expansion starts at version 3.
    representative_expansion = {
        "sales_to_price_ratio", "dividend_yield_ttm", "operating_cashflow_to_ev_ttm",
        "gross_margin_ttm", "operating_margin_ttm", "asset_turnover_ttm",
        "operating_cashflow_to_debt", "current_ratio", "revenue_growth_yoy",
        "nonlinear_size", "turnover_cv_20", "return_skewness_120",
    }
    expanded = tuple(
        item.model_copy(update={
            "factor_version": (
                "jqdata-factorlib-local-3"
                if item.external_name == "momentum" or item.external_name in representative_expansion
                else "jqdata-factorlib-local-2"
            )
        })
        for item in expanded_v1
    )
    return base + expanded
