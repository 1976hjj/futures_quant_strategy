"""Curated JoinQuant/JQData factors used by the first strategy research pass.

The formulas below are copied from JoinQuant's factor-board metadata.  Publicly
reproducible formulas are evaluated on governed local data; analyst forecasts and
the composite residual-volatility factor still use JQData's published values.
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
    catalog_status: Literal["CATALOGED_NOT_CALCULATED"] = "CATALOGED_NOT_CALCULATED"


def jqdata_catalog() -> tuple[JQDataCatalogItem, ...]:
    """Return selected JQData definitions and their local replacement candidates."""

    base = (
        JQDataCatalogItem(
            factor_id="jqdata-predicted-earnings-to-price-ratio",
            external_name="predicted_earnings_to_price_ratio",
            chinese_name="预期盈利收益率（聚宽原名：预期市盈率）",
            category="估值",
            family="jqdata-valuation",
            formula="分析师对未来一年预期盈利加权平均值 / 当前股票市值",
            description="衡量分析师一致预期盈利相对当前市值的水平。它是收益率口径，不是通常所说的市盈率倍数。",
            required_fields=("JQData.predicted_earnings_to_price_ratio",),
            expected_direction="HIGH",
        ),
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
            factor_version="jqdata-factorlib-2",
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
            factor_version="jqdata-factorlib-2",
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
            chinese_name="残余波动率因子",
            category="波动",
            family="jqdata-risk",
            formula="0.50 * daily_std + 0.42 * historical_resid_sigma + 0.08 * cum_range",
            description="综合日收益波动、市场模型残差波动和累计收益区间。第一版策略用它排除残余波动最高的一组股票。",
            required_fields=("JQData.resvol",),
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
            factor_version="jqdata-factorlib-local-1",
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
    )
    # Local reproductions are versioned immutably. Version 2 adds strict full-window
    # guards for rolling market factors and supersedes the initial candidate release.
    expanded = tuple(
        item.model_copy(update={"factor_version": "jqdata-factorlib-local-2"})
        for item in expanded_v1
    )
    return base + expanded
