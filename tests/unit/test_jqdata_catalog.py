from __future__ import annotations

import pytest

from alpha_research_os.factors.jqdata import jqdata_catalog
from alpha_research_os.reporting.factor_catalog_overview import build_factor_catalog_overview, query_factor_catalog
from scripts.publish_jqdata_factor import FUNDAMENTAL_ENGINE_VERSION, _catalog, _credentials, _engine_version, _jq_code


def test_jqdata_catalog_contains_selected_originals_and_local_replacements() -> None:
    items = jqdata_catalog()
    names = [item.external_name for item in items]
    assert names[:6] == [
        "predicted_earnings_to_price_ratio",
        "cash_earnings_to_price_ratio",
        "earnings_to_price_ratio",
        "share_turnover_monthly",
        "daily_standard_deviation",
        "resvol",
    ]
    assert set(names[6:]) == {
        "book_to_price_ratio", "cash_flow_to_price_ratio", "roe_ttm", "roa_ttm", "ACCA",
        "adjusted_profit_to_total_profit", "net_operating_cash_flow_coverage",
        "debt_to_equity_ratio", "growth", "momentum", "Rank1M", "Variance20",
        "sharpe_ratio_60", "beta", "ATR6", "DAVOL10", "liquidity",
        "natural_log_of_market_cap",
    }
    by_name = {item.external_name: item for item in items}
    assert by_name["cash_earnings_to_price_ratio"].factor_version == "jqdata-factorlib-2"
    assert by_name["earnings_to_price_ratio"].factor_version == "jqdata-factorlib-2"
    assert by_name["share_turnover_monthly"].factor_version == "jqdata-factorlib-2"
    assert by_name["daily_standard_deviation"].factor_version == "jqdata-factorlib-2"
    assert by_name["predicted_earnings_to_price_ratio"].formula == (
        "分析师对未来一年预期盈利加权平均值 / 当前股票市值"
    )
    assert by_name["cash_earnings_to_price_ratio"].formula == "过去一年的净经营现金流 / 当前股票市值"
    assert by_name["earnings_to_price_ratio"].formula == (
        "过去一年的归母净利润 / 当前股票市值（等于 PE_TTM 的倒数）"
    )
    assert by_name["share_turnover_monthly"].formula == "ln(sum(turn_over_ratio, 21个交易日))"
    assert "半衰期42个交易日" in by_name["daily_standard_deviation"].formula
    assert by_name["resvol"].formula == (
        "0.50 * daily_std + 0.42 * historical_resid_sigma + 0.08 * cum_range"
    )
    assert [item.expected_direction for item in items[:6]] == [
        "HIGH", "HIGH", "HIGH", "LOW", "LOW", "LOW"
    ]


def test_jqdata_catalog_is_a_separate_source_filter(tmp_path) -> None:
    response = query_factor_catalog(
        build_factor_catalog_overview(tmp_path), page=1, page_size=30, source="JQDATA"
    )
    assert response["totalItems"] == 24
    assert response["counts"]["jqdata"] == 24
    assert all(item["source_collection"] == "JQDATA" for item in response["items"])


def test_jqdata_factor_spec_preserves_direction_and_vendor_source() -> None:
    high, *_, low = jqdata_catalog()
    assert _catalog(high).list()[0].entry.spec.direction.value == "POSITIVE"
    assert _catalog(low).list()[0].entry.spec.direction.value == "NEGATIVE"
    assert _catalog(high).list()[0].entry.source_reference.kind.value == "DATA_VENDOR"


def test_fundamental_local_formulas_use_the_deterministic_engine() -> None:
    by_name = {item.external_name: item for item in jqdata_catalog()}
    for name in ("cash_earnings_to_price_ratio", "earnings_to_price_ratio"):
        assert _engine_version(by_name[name]) == FUNDAMENTAL_ENGINE_VERSION


def test_jqdata_security_code_translation() -> None:
    assert _jq_code("600000.SH") == "600000.XSHG"
    assert _jq_code("000001.SZ") == "000001.XSHE"
    assert _jq_code("430047.BJ") == "430047.XBJE"
    with pytest.raises(ValueError, match="unsupported"):
        _jq_code("ABC.HK")


def test_jqdata_credentials_are_required_for_provider_backed_factors(monkeypatch) -> None:
    monkeypatch.delenv("JQDATA_USERNAME", raising=False)
    monkeypatch.delenv("JQDATA_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="JQData 尚未配置"):
        _credentials()
