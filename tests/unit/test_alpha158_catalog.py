from __future__ import annotations

from collections import Counter
import json
import os

import pytest

from alpha_research_os.factors.alpha158 import alpha158_catalog
from alpha_research_os.reporting.factor_catalog_overview import (
    _latest_explorer_factors,
    build_factor_catalog_overview,
    query_factor_catalog,
)
from scripts.publish_alpha158_factor import _catalog, _factor_sql


def test_alpha158_catalog_has_exact_official_shape() -> None:
    catalog = alpha158_catalog()
    by_name = {item.external_name: item for item in catalog}

    assert len(catalog) == 158
    assert len(by_name) == 158
    assert Counter(item.family for item in catalog)["kbar"] == 9
    assert Counter(item.family for item in catalog)["price"] == 4
    assert all(Counter(item.family for item in catalog)[family] == 5 for family in ("roc", "std", "vsumd"))
    assert by_name["KMID"].formula == "($close-$open)/$open"
    assert by_name["ROC20"].formula == "Ref($close, 20)/$close"
    assert by_name["VSTD60"].formula == "Std($volume, 60)/($volume+1e-12)"


def test_empty_project_shows_13_native_and_158_alpha_items_as_not_calculated(tmp_path) -> None:
    items = build_factor_catalog_overview(tmp_path)
    response = query_factor_catalog(items, page=1, page_size=24)

    assert len(items) == 171
    assert response["counts"] == {
        "total": 171,
        "calculated": 0,
        "m4_completed": 0,
        "not_calculated": 171,
        "current": 13,
        "alpha158": 158,
    }
    assert len(response["items"]) == 24
    assert response["totalPages"] == 8


def test_explorer_index_keeps_latest_result_for_each_factor(tmp_path) -> None:
    explorer_root = tmp_path / "reports" / "factor_explorer"

    def publish(report_id: str, factor_id: str, rank_ic: float, timestamp: int) -> None:
        report = explorer_root / report_id / "evidence-summary.json"
        report.parent.mkdir(parents=True)
        report.write_text(
            json.dumps(
                {
                    "factors": [
                        {
                            "factor_id": factor_id,
                            "factor_version": "qlib-main-catalog-1",
                            "variant": "RAW",
                            "basic_evidence": {"mean_rank_ic": rank_ic},
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        os.utime(report, (timestamp, timestamp))

    publish("older-sump", "alpha158-sump10", 0.01, 100)
    publish("newer-sump", "alpha158-sump10", 0.02, 300)
    publish("latest-other-factor", "alpha158-cntp10", 0.03, 400)

    indexed = _latest_explorer_factors(tmp_path)

    assert set(indexed) == {
        ("alpha158-sump10", "qlib-main-catalog-1"),
        ("alpha158-cntp10", "qlib-main-catalog-1"),
    }
    assert indexed[("alpha158-sump10", "qlib-main-catalog-1")]["basic_evidence"]["mean_rank_ic"] == 0.02


def test_catalog_filter_search_and_pagination_are_deterministic(tmp_path) -> None:
    items = build_factor_catalog_overview(tmp_path)
    first = query_factor_catalog(
        items,
        page=1,
        page_size=5,
        query="波动",
        category="波动",
        source="ALPHA158",
    )
    repeated = query_factor_catalog(
        items,
        page=1,
        page_size=5,
        query="波动",
        category="波动",
        source="ALPHA158",
    )

    assert first == repeated
    assert first["totalItems"] == 5
    assert all(item["category"] == "波动" for item in first["items"])
    assert first["categories"]["质量"] == 0
    assert first["categories"]["波动"] == 5


def test_catalog_rejects_unbounded_page_size(tmp_path) -> None:
    with pytest.raises(ValueError, match="pageSize"):
        query_factor_catalog(build_factor_catalog_overview(tmp_path), page_size=101)


def test_every_alpha158_factor_compiles_to_duckdb_sql() -> None:
    expressions = {item.external_name: _factor_sql(item) for item in alpha158_catalog()}

    assert len(expressions) == 158
    assert "lag(close,20)" in expressions["ROC20"]
    assert "quantile_cont" in expressions["QTLU60"]
    assert "corr" in expressions["CORD10"]


def test_alpha158_release_catalog_is_one_factor_only() -> None:
    item = next(item for item in alpha158_catalog() if item.external_name == "VWAP0")
    catalog = _catalog(item)
    registered = catalog.list()

    assert len(registered) == 1
    assert registered[0].entry.spec.factor_id == "alpha158-vwap0"
    assert set(registered[0].entry.spec.required_fields) == {
        "adj_factor", "amount_cny", "close", "volume_shares"
    }
    assert registered[0].entry.lifecycle.value == "RESEARCH_ONLY"
