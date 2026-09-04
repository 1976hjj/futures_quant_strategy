"""Independently audit M4.4 walk-forward, direction, FDR, and exposure semantics."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import duckdb
from audit_m4_3_robustness import _independent_bh, _independent_bootstrap, _independent_hac

from alpha_research_os.evaluation import LabelReleaseManifest, WalkForwardEvidenceManifest
from alpha_research_os.kernel.canonical import content_hash

DEFAULT_WALK_FORWARD_ID = "sha256:a32e6aa8bdfa962280b7cac5fdedfe0be4dd98b620a0295eec65b2956999a95e"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _close(left: float | None, right: float | None, tolerance: float = 1e-12) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def _ranks(values: list[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    result = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = ((index + 1) + end) / 2
        for original, _ in ordered[index:end]:
            result[original] = rank
        index = end
    return result


def _correlation(left: list[float], right: list[float]) -> float:
    left_mean = sum(left) / len(left)
    right_mean = sum(right) / len(right)
    left_centered = [value - left_mean for value in left]
    right_centered = [value - right_mean for value in right]
    numerator = sum(a * b for a, b in zip(left_centered, right_centered, strict=True))
    denominator = math.sqrt(sum(value**2 for value in left_centered) * sum(value**2 for value in right_centered))
    return numerator / denominator


def audit(
    database: Path,
    factor_store: Path,
    evidence_store: Path,
    walk_forward_id: str,
) -> dict[str, object]:
    failures: list[str] = []
    directory = evidence_store / "walk_forward" / walk_forward_id.removeprefix("sha256:")
    manifest = WalkForwardEvidenceManifest.model_validate_json((directory / "manifest.json").read_bytes())
    if manifest.walk_forward_id != walk_forward_id:
        failures.append("walk-forward identity differs from manifest")
    paths = {item.name: evidence_store / item.relative_path for item in manifest.files}
    for item in manifest.files:
        if _sha256_file(paths[item.name]) != item.artifact_hash:
            failures.append(f"walk-forward artifact hash mismatch: {item.name}")
    for reference in manifest.request.factor_inputs:
        kind = "releases" if reference.variant == "RAW" else "processed_releases"
        factor_manifest_path = factor_store / kind / reference.release_id.removeprefix("sha256:") / "manifest.json"
        factor_manifest_payload = json.loads(factor_manifest_path.read_bytes())
        if content_hash(factor_manifest_payload) != reference.manifest_hash:
            failures.append(f"factor manifest hash mismatch: {reference.variant}")
        if _sha256_file(factor_store / factor_manifest_payload["parquet_relative_path"]) != reference.parquet_hash:
            failures.append(f"factor Parquet hash mismatch: {reference.variant}")
    label_directory = evidence_store / "labels" / manifest.request.label_release_id.removeprefix("sha256:")
    label_manifest = LabelReleaseManifest.model_validate_json((label_directory / "manifest.json").read_bytes())
    label_path = evidence_store / label_manifest.parquet_relative_path
    if content_hash(label_manifest) != manifest.request.label_manifest_hash:
        failures.append("label manifest hash differs from walk-forward request")

    daily_path = paths["daily_rank_ic"]
    fold_path = paths["fold_statistics"]
    regime_path = paths["regime_statistics"]
    with duckdb.connect(str(database), read_only=True) as connection:
        registered = connection.execute(
            "SELECT manifest_hash FROM metadata.walk_forward_evidence_manifest WHERE walk_forward_id=?",
            [walk_forward_id],
        ).fetchone()
        if registered != (content_hash(manifest),):
            failures.append("DuckDB walk-forward registry differs from manifest")
        daily_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT (variant,session,factor_id,factor_version)),
              count(*) FILTER (WHERE rank_ic IS NOT NULL AND (NOT isfinite(rank_ic) OR abs(rank_ic)>1))
            FROM read_parquet('{_sql_path(daily_path)}')"""
        ).fetchone()
        if daily_dimensions != (manifest.daily_row_count, manifest.daily_row_count, 0):
            failures.append("daily RankIC dimensions, uniqueness, or bounds failed")
        fold_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT (fold_id,variant,factor_id,factor_version)),
              count(*) FILTER (WHERE hac_p_value_two_sided NOT BETWEEN 0 AND 1
                OR bootstrap_p_value_two_sided NOT BETWEEN 0 AND 1
                OR hac_bh_q_value NOT BETWEEN 0 AND 1 OR bootstrap_bh_q_value NOT BETWEEN 0 AND 1)
            FROM read_parquet('{_sql_path(fold_path)}')"""
        ).fetchone()
        if fold_dimensions != (manifest.fold_hypothesis_count, manifest.fold_hypothesis_count, 0):
            failures.append("fold hypothesis dimensions, uniqueness, or probability bounds failed")
        regime_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT
              (fold_id,variant,factor_id,factor_version,regime_dimension,regime))
            FROM read_parquet('{_sql_path(regime_path)}')"""
        ).fetchone()
        if regime_dimensions != (manifest.regime_row_count, manifest.regime_row_count):
            failures.append("regime dimensions or uniqueness failed")

        exposure = connection.execute(
            """SELECT fold_id,prior_exposure_status,event_type,recorded_at
            FROM metadata.holdout_exposure_ledger WHERE walk_forward_id=? ORDER BY fold_id""",
            [walk_forward_id],
        ).fetchall()
        if len(exposure) != len(manifest.request.evaluation.folds) or any(
            row[2] != "RESERVED_BEFORE_STATISTICAL_READ" for row in exposure
        ):
            failures.append("holdout exposure was not reserved before statistical read")
        if any(row[3] >= manifest.created_at for row in exposure):
            failures.append("holdout exposure timestamp is not earlier than release creation")

        factor_paths = {
            reference.variant: factor_store
            / ("releases" if reference.variant == "RAW" else "processed_releases")
            / reference.release_id.removeprefix("sha256:")
            / ("raw_factor_values.parquet" if reference.variant == "RAW" else "processed_factor_values.parquet")
            for reference in manifest.request.factor_inputs
        }
        sample_variant, sample_factor_id, sample_factor_version, sample_session = connection.execute(
            f"""SELECT variant,factor_id,factor_version,max(session) AS sample_session
            FROM read_parquet('{_sql_path(daily_path)}') WHERE rank_ic IS NOT NULL
            GROUP BY 1,2,3 ORDER BY sample_session DESC,variant,factor_id,factor_version LIMIT 1"""
        ).fetchone()
        sample_pairs = connection.execute(
            f"""SELECT f.value,l.value FROM read_parquet('{_sql_path(factor_paths[sample_variant])}') f
            JOIN read_parquet('{_sql_path(label_path)}') l
              ON l.signal_session=f.session AND l.instrument_id=f.instrument_id
            WHERE f.session=? AND f.factor_id=? AND f.factor_version=?
              AND f.value IS NOT NULL AND l.is_valid AND l.value IS NOT NULL ORDER BY f.instrument_id""",
            [sample_session, sample_factor_id, sample_factor_version],
        ).fetchall()
        independent_rank_ic = _correlation(
            _ranks([row[0] for row in sample_pairs]), _ranks([row[1] for row in sample_pairs])
        )
        stored_rank_ic = connection.execute(
            f"""SELECT rank_ic FROM read_parquet('{_sql_path(daily_path)}')
            WHERE variant=? AND factor_id=? AND factor_version=? AND session=?""",
            [sample_variant, sample_factor_id, sample_factor_version, sample_session],
        ).fetchone()[0]
        if not _close(independent_rank_ic, stored_rank_ic):
            failures.append("independent cross-sectional RankIC differs from daily asset")

        fold_rows = connection.execute(
            f"""SELECT * FROM read_parquet('{_sql_path(fold_path)}')
            ORDER BY fold_id,variant,factor_id,factor_version"""
        ).fetchall()
        fold_columns = [item[0] for item in connection.description]
        fold_dicts = [dict(zip(fold_columns, row, strict=True)) for row in fold_rows]
        expected_hac_q = _independent_bh([item["hac_p_value_two_sided"] for item in fold_dicts])
        expected_bootstrap_q = _independent_bh([item["bootstrap_p_value_two_sided"] for item in fold_dicts])
        if any(
            not _close(item["hac_bh_q_value"], hac_q) or not _close(item["bootstrap_bh_q_value"], bootstrap_q)
            for item, hac_q, bootstrap_q in zip(fold_dicts, expected_hac_q, expected_bootstrap_q, strict=True)
        ):
            failures.append("independent BH-FDR differs from stored fold q-values")

        directions = {
            (factor_id, version): json.loads(spec_json)["direction"]
            for factor_id, version, spec_json in connection.execute(
                "SELECT factor_id,factor_version,spec_json FROM metadata.factor_registry"
            ).fetchall()
        }
        daily_grouped: dict[tuple[str, str, str], list[tuple[object, float]]] = defaultdict(list)
        for variant, factor_id, version, session, rank_ic in connection.execute(
            f"""SELECT variant,factor_id,factor_version,session,rank_ic
            FROM read_parquet('{_sql_path(daily_path)}') WHERE rank_ic IS NOT NULL ORDER BY 1,2,3,4"""
        ).fetchall():
            daily_grouped[(variant, factor_id, version)].append((session, rank_ic))
        fold_specs = {fold.fold_id: fold for fold in manifest.request.evaluation.folds}
        for item in fold_dicts:
            fold = fold_specs[item["fold_id"]]
            observations = daily_grouped[(item["variant"], item["factor_id"], item["factor_version"])]
            train = [value for session, value in observations if fold.train.start <= session <= fold.train.end]
            test = [value for session, value in observations if fold.test.start <= session <= fold.test.end]
            direction = directions[(item["factor_id"], item["factor_version"])]
            expected_multiplier = {"POSITIVE": 1, "NEGATIVE": -1}.get(
                direction, 1 if sum(train) / len(train) >= 0 else -1
            )
            if expected_multiplier != item["direction_multiplier"]:
                failures.append(f"direction leakage or mismatch: {item['fold_id']} {item['factor_id']}")
                break
            if not _close(sum(test) / len(test), item["test_mean_rank_ic_raw"]):
                failures.append(f"test mean differs from frozen date slice: {item['fold_id']} {item['factor_id']}")
                break

        target = fold_dicts[-1]
        fold = fold_specs[target["fold_id"]]
        test = [
            target["direction_multiplier"] * value
            for session, value in daily_grouped[(target["variant"], target["factor_id"], target["factor_version"])]
            if fold.test.start <= session <= fold.test.end
        ]
        hac = _independent_hac(test, manifest.request.evaluation.inference.hac_max_lag)
        stored_hac = (
            target["test_mean_rank_ic_directed"],
            target["hac_standard_error"],
            target["hac_z_statistic"],
            target["hac_p_value_two_sided"],
        )
        if any(not _close(left, right) for left, right in zip(hac, stored_hac, strict=True)):
            failures.append("independent target-fold HAC calculation differs")
        bootstrap = _independent_bootstrap(
            test,
            manifest.request.evaluation.inference.bootstrap_block_length,
            manifest.request.evaluation.inference.bootstrap_resamples,
            target["bootstrap_seed"],
            manifest.request.evaluation.inference.bootstrap_confidence_level,
        )
        stored_bootstrap = (
            target["bootstrap_p_value_two_sided"],
            target["bootstrap_confidence_lower"],
            target["bootstrap_confidence_upper"],
        )
        if any(not _close(left, right) for left, right in zip(bootstrap, stored_bootstrap, strict=True)):
            failures.append("independent target-fold moving-block bootstrap differs")

        regime_count_errors = connection.execute(
            f"""WITH counts AS (
              SELECT fold_id,variant,factor_id,factor_version,regime_dimension,sum(session_count) n
              FROM read_parquet('{_sql_path(regime_path)}') GROUP BY 1,2,3,4,5)
            SELECT count(*) FROM counts c JOIN read_parquet('{_sql_path(fold_path)}') f
              USING (fold_id,variant,factor_id,factor_version) WHERE c.n<>f.test_session_count"""
        ).fetchone()[0]
        if regime_count_errors:
            failures.append("regime partitions do not reconcile to test session counts")

    support_by_fold: dict[str, dict[str, int]] = {}
    for fold_id in sorted(fold_specs):
        current = [item for item in fold_dicts if item["fold_id"] == fold_id]
        support_by_fold[fold_id] = {
            "hac_supported": sum(item["hac_fdr_reject"] and item["test_mean_rank_ic_directed"] > 0 for item in current),
            "hac_contradicted": sum(
                item["hac_fdr_reject"] and item["test_mean_rank_ic_directed"] < 0 for item in current
            ),
            "bootstrap_supported": sum(
                item["bootstrap_fdr_reject"] and item["test_mean_rank_ic_directed"] > 0 for item in current
            ),
            "bootstrap_contradicted": sum(
                item["bootstrap_fdr_reject"] and item["test_mean_rank_ic_directed"] < 0 for item in current
            ),
        }
    contradicted = sum(item["hac_fdr_reject"] and item["test_mean_rank_ic_directed"] < 0 for item in fold_dicts)
    findings = (
        f"{contradicted} configured fold-factor-variant hypotheses significantly contradict their frozen direction.",
        "FDR rejection counts may contain correlated variants and are not independent Alpha discoveries.",
        "Any fold marked as a first research read is exposed after this run and cannot be reused as unseen.",
        "No result is promotion-eligible before configured execution, cost, delisting, and redundancy gates.",
    )
    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "walk_forward_id": walk_forward_id,
        "daily_rows": manifest.daily_row_count,
        "fold_hypotheses": manifest.fold_hypothesis_count,
        "regime_rows": manifest.regime_row_count,
        "rank_ic_crosscheck_session": sample_session.isoformat(),
        "rank_ic_crosscheck_entity": f"{sample_variant}|{sample_factor_id}|{sample_factor_version}",
        "support_and_contradiction_by_fold": support_by_fold,
        "findings": findings,
        "failures": failures,
        "status": "PASS_WITH_FINDINGS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--factor-store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--walk-forward-id", default=DEFAULT_WALK_FORWARD_ID)
    parser.add_argument("--output", type=Path, default=Path("reports/m4_4_walk_forward_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.factor_store, args.evidence_store, args.walk_forward_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
