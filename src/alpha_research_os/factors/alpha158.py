"""Source-faithful metadata catalog for Microsoft's Qlib Alpha158 features.

The entries in this module are catalog metadata, not calculated Factor Releases.  Qlib
expressions are retained verbatim enough for provenance and later adapter work; they are
not silently treated as native Alpha Research OS expressions.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import HttpUrl

from alpha_research_os.kernel.specs import FrozenSpec

FactorCategory = Literal["动量", "波动", "流动性", "质量", "估值", "风格", "量价", "形态"]

QLIB_ALPHA158_SOURCE = "https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py"
WINDOWS = (5, 10, 20, 30, 60)


class Alpha158CatalogItem(FrozenSpec):
    factor_id: str
    external_name: str
    factor_version: str = "qlib-main-catalog-2"
    chinese_name: str
    category: FactorCategory
    family: str
    formula: str
    description: str
    required_fields: tuple[str, ...]
    window_sessions: int | None = None
    source_id: str = "microsoft-qlib-alpha158"
    source_uri: HttpUrl = QLIB_ALPHA158_SOURCE
    catalog_status: Literal["CATALOGED_NOT_CALCULATED"] = "CATALOGED_NOT_CALCULATED"


def _fields(formula: str) -> tuple[str, ...]:
    return tuple(sorted(set(re.findall(r"\$([a-z]+)", formula))))


def _item(
    name: str,
    chinese_name: str,
    category: FactorCategory,
    family: str,
    formula: str,
    description: str,
    window: int | None = None,
) -> Alpha158CatalogItem:
    return Alpha158CatalogItem(
        factor_id=f"alpha158-{name.lower()}",
        external_name=name,
        chinese_name=chinese_name,
        category=category,
        family=family,
        formula=formula,
        description=description,
        required_fields=_fields(formula),
        window_sessions=window,
    )


def _kbar_items() -> tuple[Alpha158CatalogItem, ...]:
    definitions = (
        ("KMID", "K线实体涨跌", "($close-$open)/$open", "收盘价相对开盘价的涨跌幅。"),
        ("KLEN", "日内振幅", "($high-$low)/$open", "最高价与最低价的距离，相对开盘价归一化。"),
        ("KMID2", "实体占振幅比", "($close-$open)/($high-$low+1e-12)", "K线实体在全天振幅中的方向和占比。"),
        ("KUP", "上影线幅度", "($high-Greater($open, $close))/$open", "上影线相对开盘价的长度。"),
        ("KUP2", "上影线占振幅比", "($high-Greater($open, $close))/($high-$low+1e-12)", "上影线在全天振幅中的占比。"),
        ("KLOW", "下影线幅度", "(Less($open, $close)-$low)/$open", "下影线相对开盘价的长度。"),
        ("KLOW2", "下影线占振幅比", "(Less($open, $close)-$low)/($high-$low+1e-12)", "下影线在全天振幅中的占比。"),
        ("KSFT", "收盘位置偏移", "(2*$close-$high-$low)/$open", "收盘价相对当日高低价中点的位置。"),
        ("KSFT2", "归一化收盘位置", "(2*$close-$high-$low)/($high-$low+1e-12)", "收盘位置相对全天振幅归一化。"),
    )
    return tuple(
        _item(name, chinese, "形态", "kbar", formula, description)
        for name, chinese, formula, description in definitions
    )


def _price_items() -> tuple[Alpha158CatalogItem, ...]:
    labels = {"OPEN": "开盘价位置", "HIGH": "最高价位置", "LOW": "最低价位置", "VWAP": "成交均价位置"}
    return tuple(
        _item(
            f"{field}0",
            labels[field],
            "形态",
            "price",
            f"${field.lower()}/$close",
            f"当日{labels[field].removesuffix('位置')}相对收盘价的比例。",
        )
        for field in ("OPEN", "HIGH", "LOW", "VWAP")
    )


def _rolling_formula(operator: str, window: int) -> str:
    formulas = {
        "ROC": f"Ref($close, {window})/$close",
        "MA": f"Mean($close, {window})/$close",
        "STD": f"Std($close, {window})/$close",
        "BETA": f"Slope($close, {window})/$close",
        "RSQR": f"Rsquare($close, {window})",
        "RESI": f"Resi($close, {window})/$close",
        "MAX": f"Max($high, {window})/$close",
        "MIN": f"Min($low, {window})/$close",
        "QTLU": f"Quantile($close, {window}, 0.8)/$close",
        "QTLD": f"Quantile($close, {window}, 0.2)/$close",
        "RANK": f"Rank($close, {window})",
        "RSV": f"($close-Min($low, {window}))/(Max($high, {window})-Min($low, {window})+1e-12)",
        "IMAX": f"IdxMax($high, {window})/{window}",
        "IMIN": f"IdxMin($low, {window})/{window}",
        "IMXD": f"(IdxMax($high, {window})-IdxMin($low, {window}))/{window}",
        "CORR": f"Corr($close, Log($volume+1), {window})",
        "CORD": f"Corr($close/Ref($close,1), Log($volume/Ref($volume, 1)+1), {window})",
        "CNTP": f"Mean($close>Ref($close, 1), {window})",
        "CNTN": f"Mean($close<Ref($close, 1), {window})",
        "CNTD": f"Mean($close>Ref($close, 1), {window})-Mean($close<Ref($close, 1), {window})",
        "SUMP": f"Sum(Greater($close-Ref($close, 1), 0), {window})/(Sum(Abs($close-Ref($close, 1)), {window})+1e-12)",
        "SUMN": f"Sum(Greater(Ref($close, 1)-$close, 0), {window})/(Sum(Abs($close-Ref($close, 1)), {window})+1e-12)",
        "SUMD": (
            f"(Sum(Greater($close-Ref($close, 1), 0), {window})"
            f"-Sum(Greater(Ref($close, 1)-$close, 0), {window}))"
            f"/(Sum(Abs($close-Ref($close, 1)), {window})+1e-12)"
        ),
        "VMA": f"Mean($volume, {window})/($volume+1e-12)",
        "VSTD": f"Std($volume, {window})/($volume+1e-12)",
        "WVMA": (
            f"Std(Abs($close/Ref($close, 1)-1)*$volume, {window})"
            f"/(Mean(Abs($close/Ref($close, 1)-1)*$volume, {window})+1e-12)"
        ),
        "VSUMP": (
            f"Sum(Greater($volume-Ref($volume, 1), 0), {window})/(Sum(Abs($volume-Ref($volume, 1)), {window})+1e-12)"
        ),
        "VSUMN": (
            f"Sum(Greater(Ref($volume, 1)-$volume, 0), {window})/(Sum(Abs($volume-Ref($volume, 1)), {window})+1e-12)"
        ),
        "VSUMD": (
            f"(Sum(Greater($volume-Ref($volume, 1), 0), {window})"
            f"-Sum(Greater(Ref($volume, 1)-$volume, 0), {window}))"
            f"/(Sum(Abs($volume-Ref($volume, 1)), {window})+1e-12)"
        ),
    }
    return formulas[operator]


_ROLLING_META: dict[str, tuple[str, FactorCategory, str]] = {
    "ROC": ("价格变化率", "动量", "历史收盘价相对当前收盘价的比例，反映价格变化方向。"),
    "MA": ("移动均价比", "动量", "滚动平均收盘价相对当前收盘价的位置。"),
    "STD": ("价格波动率", "波动", "滚动收盘价标准差相对当前收盘价归一化。"),
    "BETA": ("价格趋势斜率", "动量", "滚动线性趋势斜率相对当前收盘价归一化。"),
    "RSQR": ("趋势拟合度", "动量", "滚动线性趋势的拟合优度。"),
    "RESI": ("趋势残差", "动量", "当前价格偏离滚动线性趋势的程度。"),
    "MAX": ("区间最高价比", "形态", "区间最高价相对当前收盘价的位置。"),
    "MIN": ("区间最低价比", "形态", "区间最低价相对当前收盘价的位置。"),
    "QTLU": ("价格上分位点", "形态", "区间收盘价80%分位数相对当前收盘价的位置。"),
    "QTLD": ("价格下分位点", "形态", "区间收盘价20%分位数相对当前收盘价的位置。"),
    "RANK": ("当前价格区间排名", "形态", "当前收盘价在历史窗口中的百分位位置。"),
    "RSV": ("区间相对位置", "形态", "收盘价位于区间最低价和最高价之间的位置。"),
    "IMAX": ("距区间高点时间", "形态", "区间最高价出现时间距当前的归一化距离。"),
    "IMIN": ("距区间低点时间", "形态", "区间最低价出现时间距当前的归一化距离。"),
    "IMXD": ("高低点时间差", "形态", "区间最高价与最低价出现时间的方向差。"),
    "CORR": ("价量水平相关", "量价", "收盘价与对数成交量在窗口内的相关性。"),
    "CORD": ("价量变化相关", "量价", "价格变化比例与成交量变化比例的相关性。"),
    "CNTP": ("上涨天数占比", "动量", "窗口内收盘上涨交易日的占比。"),
    "CNTN": ("下跌天数占比", "动量", "窗口内收盘下跌交易日的占比。"),
    "CNTD": ("涨跌天数差", "动量", "窗口内上涨与下跌交易日占比之差。"),
    "SUMP": ("上涨幅度占比", "动量", "窗口内上涨幅度占全部绝对价格变动的比例。"),
    "SUMN": ("下跌幅度占比", "动量", "窗口内下跌幅度占全部绝对价格变动的比例。"),
    "SUMD": ("净涨跌强度", "动量", "上涨和下跌幅度之差相对全部价格变动归一化。"),
    "VMA": ("成交量均值比", "流动性", "窗口平均成交量相对当前成交量的比例。"),
    "VSTD": ("成交量波动率", "流动性", "窗口成交量标准差相对当前成交量归一化。"),
    "WVMA": ("量价加权波动", "量价", "以成交量加权的价格变化波动程度。"),
    "VSUMP": ("成交量增加占比", "流动性", "成交量增加部分占全部成交量变化的比例。"),
    "VSUMN": ("成交量减少占比", "流动性", "成交量减少部分占全部成交量变化的比例。"),
    "VSUMD": ("成交量增减强度", "流动性", "成交量增加与减少的净强度。"),
}


def alpha158_catalog() -> tuple[Alpha158CatalogItem, ...]:
    items = [*_kbar_items(), *_price_items()]
    for operator, (chinese, category, description) in _ROLLING_META.items():
        for window in WINDOWS:
            items.append(
                _item(
                    f"{operator}{window}",
                    f"{window}日{chinese}",
                    category,
                    operator.lower(),
                    _rolling_formula(operator, window),
                    description,
                    window,
                )
            )
    if len(items) != 158 or len({item.factor_id for item in items}) != 158:
        raise AssertionError("Alpha158 catalog must contain exactly 158 unique features")
    return tuple(items)
