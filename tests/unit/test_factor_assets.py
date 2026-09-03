from __future__ import annotations

from datetime import date

import duckdb
import pytest

from alpha_research_os.factors.assets import DatasetLineage, FactorAssetRef, FactorAssetRequest
from alpha_research_os.factors.expression import compile_feature_expression
from alpha_research_os.factors.sql import expression_manifest_to_duckdb

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64


def _reference(factor_id: str) -> FactorAssetRef:
    return FactorAssetRef(
        factor_id=factor_id,
        factor_version="1.0.0",
        spec_hash=DIGEST_A,
        implementation_hash=DIGEST_B,
        catalog_entry_hash=DIGEST_C,
    )


def _request(end: date = date(2024, 1, 31)) -> FactorAssetRequest:
    return FactorAssetRequest(
        engine_version="test-engine-v1",
        factors=(_reference("alpha-a"), _reference("alpha-b")),
        dataset_lineage=(DatasetLineage(manifest_table="metadata.dataset", checkpoint_hashes=(DIGEST_A,)),),
        universe_id="ALL-A-PIT",
        universe_version="test-v1",
        start=date(2024, 1, 1),
        end=end,
        signal_clock_version="close-v1",
    )


def test_factor_asset_key_is_stable_and_sensitive_to_scope() -> None:
    assert _request().computation_key == _request().computation_key
    assert _request().computation_key != _request(date(2024, 2, 1)).computation_key


def test_factor_asset_references_must_be_sorted() -> None:
    values = _request().model_dump()
    values["factors"] = (_reference("alpha-b"), _reference("alpha-a"))
    with pytest.raises(ValueError, match="sorted and unique"):
        FactorAssetRequest(**values)


@pytest.mark.parametrize(
    ("formula", "expected"),
    [
        ("close / Ref(close, 1) - 1", [None, 0.1, 0.1]),
        ("Mean(close, 2)", [None, 10.5, 11.55]),
        ("Std(return_1d, 3)", [None, None, pytest.approx(0.0081649658)]),
    ],
)
def test_duckdb_translation_matches_expression_semantics(formula: str, expected: list[object]) -> None:
    compiled = compile_feature_expression(formula)
    sql = expression_manifest_to_duckdb(
        compiled.manifest()["root"],
        window="PARTITION BY instrument_id ORDER BY session",
    )
    connection = duckdb.connect()
    rows = connection.execute(
        f"""SELECT {sql} FROM (VALUES
        ('A', 1, 10.0, 0.01), ('A', 2, 11.0, 0.02), ('A', 3, 12.1, 0.03)
        ) input(instrument_id, session, close, return_1d) ORDER BY session"""
    ).fetchall()
    for actual, target in zip([row[0] for row in rows], expected, strict=True):
        if target is None:
            assert actual is None
        else:
            assert actual == pytest.approx(target)
