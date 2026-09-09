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

    return (
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
