"""Independently audit M4.5 redundancy, clustering, and conditional RankIC evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np

from alpha_research_os.evaluation import (
    LabelReleaseManifest,
    RedundancyEvidenceManifest,
    WalkForwardEvidenceManifest,
)
from alpha_research_os.kernel.canonical import content_hash

DEFAULT_REDUNDANCY_ID = "sha256:442cf98e81eb98eea2b41714ed1d9d5a6fb449b905e6805766f7f8f3c93a0626"
VARIANT_PRIORITY = {"RAW": 0, "SIZE_NEUTRALIZED": 1, "WINSORIZED_ZSCORE": 2}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _factor_path(factor_store: Path, reference: object) -> Path:
    kind = "releases" if reference.variant == "RAW" else "processed_releases"
    filename = "raw_factor_values.parquet" if reference.variant == "RAW" else "processed_factor_values.parquet"
    return factor_store / kind / reference.release_id.removeprefix("sha256:") / filename


def _ranks(values: list[float]) -> np.ndarray:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    result = np.zeros(len(values), dtype=float)
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


def _close(left: float | None, right: float | None, tolerance: float = 1e-10) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)


def _exposure_snapshot(connection: duckdb.DuckDBPyConnection, *, before: datetime | None = None) -> tuple[str, int]:
    predicate = "" if before is None else "WHERE recorded_at < ?"
    parameters = [] if before is None else [before]
    rows = connection.execute(
        f"""SELECT event_id,walk_forward_id,fold_id,test_start,test_end,prior_exposure_status,
        event_type,recorded_at FROM metadata.holdout_exposure_ledger {predicate} ORDER BY event_id""",
        parameters,
    ).fetchall()
    columns = [item[0] for item in connection.description]
    payload = [
        {
            key: value.isoformat() if hasattr(value, "isoformat") else value
            for key, value in zip(columns, row, strict=True)
        }
        for row in rows
    ]
    return content_hash(payload), len(payload)


def _independent_partial(correlation: np.ndarray, ridge: float) -> tuple[float, float, float, float, float]:
    controls = correlation[2:, 2:]
    regularized = controls + np.eye(len(controls)) * ridge
    candidate_controls = correlation[0, 2:]
    label_controls = correlation[1, 2:]
    candidate_beta = np.linalg.solve(regularized, candidate_controls)
    label_beta = np.linalg.solve(regularized, label_controls)
    candidate_variance = max(ridge, 1.0 - float(candidate_controls @ candidate_beta))
    label_variance = max(ridge, 1.0 - float(label_controls @ label_beta))
    covariance = float(correlation[0, 1] - candidate_controls @ label_beta)
    conditional = covariance / math.sqrt(candidate_variance)
    orthogonal = covariance / math.sqrt(candidate_variance * label_variance)
    baseline_r2 = max(0.0, min(1.0, float(label_controls @ label_beta)))
    incremental_r2 = max(0.0, covariance * covariance / candidate_variance)
    full_r2 = max(0.0, min(1.0, baseline_r2 + incremental_r2))
    return conditional, orthogonal, baseline_r2, full_r2, full_r2 - baseline_r2


def _independent_canonical(
    entities: list[str], pair_rows: list[dict[str, object]], value_threshold: float, ic_threshold: float
) -> tuple[dict[str, str], list[str]]:
    parent = {entity_id: entity_id for entity_id in entities}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for row in pair_rows:
        left = str(row["left_entity_id"])
        right = str(row["right_entity_id"])
        if left.split("|", 1)[1] != right.split("|", 1)[1]:
            continue
        value_correlation = row["mean_daily_spearman_value_correlation"]
        ic_correlation = row["daily_rank_ic_correlation"]
        if (
            value_correlation is not None
            and ic_correlation is not None
            and float(value_correlation) >= value_threshold
            and float(ic_correlation) >= ic_threshold
        ):
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[max(left_root, right_root)] = min(left_root, right_root)
    components: dict[str, list[str]] = {}
    for entity_id in entities:
        components.setdefault(find(entity_id), []).append(entity_id)
    canonical_by_entity = {}
    canonical = []
    for members in components.values():
        chosen = min(members, key=lambda item: (VARIANT_PRIORITY[item.split("|", 1)[0]], item))
        canonical.append(chosen)
        for member in members:
            canonical_by_entity[member] = chosen
    return canonical_by_entity, sorted(canonical)


def _independent_clusters(
    canonical: list[str], pair_rows: list[dict[str, object]], threshold: float
) -> tuple[list[tuple[str, ...]], list[tuple[tuple[str, ...], tuple[str, ...], float, tuple[str, ...]]]]:
    pairs = {tuple(sorted((str(row["left_entity_id"]), str(row["right_entity_id"])))): row for row in pair_rows}
    distances = {}
    for left_index, left in enumerate(canonical[:-1]):
        for right in canonical[left_index + 1 :]:
            row = pairs[(left, right)]
            correlations = [
                abs(float(value))
                for value in (
                    row["mean_daily_spearman_value_correlation"],
                    row["daily_rank_ic_correlation"],
                )
                if value is not None
            ]
            distances[(left, right)] = 1 - max(correlations, default=0.0)

    def cluster_distance(left: tuple[str, ...], right: tuple[str, ...]) -> float:
        return sum(distances[tuple(sorted((a, b)))] for a in left for b in right) / (len(left) * len(right))

    active = [(entity_id,) for entity_id in canonical]
    full_merges = []
    cut_clusters: list[tuple[str, ...]] | None = None
    while len(active) > 1:
        candidates = []
        for left_index, left in enumerate(active[:-1]):
            for right_index in range(left_index + 1, len(active)):
                right = active[right_index]
                candidates.append((cluster_distance(left, right), left, right, left_index, right_index))
        distance, left, right, left_index, right_index = min(candidates, key=lambda item: (item[0], item[1], item[2]))
        if cut_clusters is None and distance > threshold:
            cut_clusters = sorted(active)
        merged = tuple(sorted((*left, *right)))
        full_merges.append((left, right, distance, merged))
        active = [cluster for index, cluster in enumerate(active) if index not in {left_index, right_index}]
        active.append(merged)
        active.sort()
    if cut_clusters is None:
        cut_clusters = sorted(active)
    return cut_clusters, full_merges


def audit(
    database: Path,
    factor_store: Path,
    evidence_store: Path,
    redundancy_id: str,
) -> dict[str, object]:
    failures: list[str] = []
    directory = evidence_store / "redundancy" / redundancy_id.removeprefix("sha256:")
    manifest = RedundancyEvidenceManifest.model_validate_json((directory / "manifest.json").read_bytes())
    if manifest.redundancy_id != redundancy_id:
        failures.append("redundancy identity differs from manifest")
    paths = {item.name: evidence_store / item.relative_path for item in manifest.files}
    for item in manifest.files:
        if _sha256_file(paths[item.name]) != item.artifact_hash:
            failures.append(f"M4.5 artifact hash mismatch: {item.name}")

    source_directory = evidence_store / "walk_forward" / manifest.request.source_walk_forward_id.removeprefix("sha256:")
    source = WalkForwardEvidenceManifest.model_validate_json((source_directory / "manifest.json").read_bytes())
    if content_hash(source) != manifest.request.source_walk_forward_manifest_hash:
        failures.append("M4.4 source manifest lineage mismatch")
    source_paths = {item.name: evidence_store / item.relative_path for item in source.files}
    daily_ic_path = source_paths["daily_rank_ic"]
    label_directory = evidence_store / "labels" / manifest.request.label_release_id.removeprefix("sha256:")
    label_manifest = LabelReleaseManifest.model_validate_json((label_directory / "manifest.json").read_bytes())
    label_path = evidence_store / label_manifest.parquet_relative_path
    if content_hash(label_manifest) != manifest.request.label_manifest_hash:
        failures.append("label manifest lineage mismatch")
    factor_paths = {
        reference.variant: _factor_path(factor_store, reference) for reference in manifest.request.factor_inputs
    }
    for reference in manifest.request.factor_inputs:
        if _sha256_file(factor_paths[reference.variant]) != reference.parquet_hash:
            failures.append(f"factor input hash mismatch: {reference.variant}")

    with duckdb.connect(str(database), read_only=True) as connection:
        registered = connection.execute(
            "SELECT manifest_hash FROM metadata.factor_redundancy_evidence_manifest WHERE redundancy_id=?",
            [redundancy_id],
        ).fetchone()
        if registered != (content_hash(manifest),):
            failures.append("DuckDB M4.5 registry differs from manifest")
        ledger_hash, ledger_count = _exposure_snapshot(connection, before=manifest.created_at)
        if (ledger_hash, ledger_count) != (
            manifest.request.exposure_ledger_snapshot_hash,
            manifest.request.exposure_ledger_row_count,
        ):
            failures.append("holdout exposure ledger snapshot before M4.5 does not match the request")
        if connection.execute(
            "SELECT count(*) FROM metadata.holdout_exposure_ledger WHERE walk_forward_id=?", [redundancy_id]
        ).fetchone()[0]:
            failures.append("M4.5 incorrectly created a new holdout exposure event")

        pair_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT (left_entity_id,right_entity_id)),
            count(*) FILTER (WHERE abs(mean_daily_spearman_value_correlation)>1
              OR abs(daily_rank_ic_correlation)>1)
            FROM read_parquet('{_sql_path(paths["pair_correlations"])}')"""
        ).fetchone()
        expected_pair_count = manifest.entity_count * (manifest.entity_count - 1) // 2
        if pair_dimensions != (expected_pair_count, expected_pair_count, 0):
            failures.append("pair-correlation dimensions, uniqueness, or bounds failed")
        pair_tuples = connection.execute(
            f"SELECT * FROM read_parquet('{_sql_path(paths['pair_correlations'])}') ORDER BY 1,2"
        ).fetchall()
        pair_columns = [item[0] for item in connection.description]
        pair_rows = [dict(zip(pair_columns, row, strict=True)) for row in pair_tuples]
        daily_value_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT (session,left_entity_id,right_entity_id)),
            count(*) FILTER (WHERE abs(spearman_value_correlation)>1)
            FROM read_parquet('{_sql_path(paths["daily_value_correlations"])}')"""
        ).fetchone()
        if daily_value_dimensions[0] != daily_value_dimensions[1] or daily_value_dimensions[2]:
            failures.append("daily factor-value correlation uniqueness or bounds failed")
        dedup_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT entity_id),sum(is_canonical::INTEGER)
            FROM read_parquet('{_sql_path(paths["variant_deduplication"])}')"""
        ).fetchone()
        if dedup_dimensions[:2] != (manifest.entity_count, manifest.entity_count):
            failures.append("variant-deduplication dimensions failed")
        if dedup_dimensions[2] != manifest.canonical_entity_count:
            failures.append("canonical entity count differs from manifest")
        stored_dedup = connection.execute(
            f"""SELECT entity_id,canonical_entity_id,is_canonical
            FROM read_parquet('{_sql_path(paths["variant_deduplication"])}') ORDER BY entity_id"""
        ).fetchall()
        entities = [row[0] for row in stored_dedup]
        expected_canonical_by_entity, expected_canonical = _independent_canonical(
            entities,
            pair_rows,
            manifest.request.evaluation.duplicate_value_correlation_threshold,
            manifest.request.evaluation.duplicate_ic_correlation_threshold,
        )
        if any(
            canonical != expected_canonical_by_entity[entity_id]
            or is_canonical != (entity_id == expected_canonical_by_entity[entity_id])
            for entity_id, canonical, is_canonical in stored_dedup
        ):
            failures.append("independent duplicate-component reconstruction differs")
        invalid_duplicate = connection.execute(
            f"""SELECT count(*) FROM read_parquet('{_sql_path(paths["variant_deduplication"])}') d
            JOIN read_parquet('{_sql_path(paths["pair_correlations"])}') p
              ON least(d.entity_id,d.canonical_entity_id)=p.left_entity_id
             AND greatest(d.entity_id,d.canonical_entity_id)=p.right_entity_id
            WHERE NOT d.is_canonical AND (
              p.mean_daily_spearman_value_correlation<
                {manifest.request.evaluation.duplicate_value_correlation_threshold}
              OR p.daily_rank_ic_correlation<{manifest.request.evaluation.duplicate_ic_correlation_threshold})"""
        ).fetchone()[0]
        if invalid_duplicate:
            failures.append("a collapsed variant does not satisfy both frozen duplicate thresholds")
        cluster_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT entity_id),count(DISTINCT cluster_id),
            count(DISTINCT cluster_id) FILTER (WHERE is_representative)
            FROM read_parquet('{_sql_path(paths["factor_clusters"])}')"""
        ).fetchone()
        expected_cluster_dimensions = (
            manifest.canonical_entity_count,
            manifest.canonical_entity_count,
            manifest.cluster_count,
            manifest.cluster_count,
        )
        if cluster_dimensions != expected_cluster_dimensions:
            failures.append("cluster membership or representative cardinality failed")
        expected_clusters, expected_merges = _independent_clusters(
            expected_canonical, pair_rows, manifest.request.evaluation.cluster_distance_threshold
        )
        stored_cluster_rows = connection.execute(
            f"""SELECT cluster_id,entity_id,representative_entity_id
            FROM read_parquet('{_sql_path(paths["factor_clusters"])}') ORDER BY cluster_id,entity_id"""
        ).fetchall()
        stored_clusters: dict[str, list[str]] = {}
        for cluster_id, entity_id, _ in stored_cluster_rows:
            stored_clusters.setdefault(cluster_id, []).append(entity_id)
        if [tuple(members) for members in stored_clusters.values()] != expected_clusters:
            failures.append("independent average-linkage cluster cut differs")
        linkage = connection.execute(
            f"""SELECT left_members_json,right_members_json,distance,merged_members_json
            FROM read_parquet('{_sql_path(paths["hierarchical_linkage"])}') ORDER BY step"""
        ).fetchall()
        if len(linkage) != manifest.canonical_entity_count - 1:
            failures.append("hierarchical linkage does not contain n-1 merges")
        if any(right[2] + 1e-12 < left[2] for left, right in zip(linkage[:-1], linkage[1:], strict=True)):
            failures.append("average-linkage distances are not monotone")
        if len(linkage) == len(expected_merges) and any(
            tuple(json.loads(stored[0])) != expected[0]
            or tuple(json.loads(stored[1])) != expected[1]
            or not _close(stored[2], expected[2])
            or tuple(json.loads(stored[3])) != expected[3]
            for stored, expected in zip(linkage, expected_merges, strict=True)
        ):
            failures.append("independent full average-linkage reconstruction differs")

        coverage_rows = connection.execute(
            f"""SELECT variant || '|' || factor_id || '|' || factor_version entity_id,avg(coverage)
            FROM read_parquet('{_sql_path(daily_ic_path)}') GROUP BY 1"""
        ).fetchall()
        coverage = dict(coverage_rows)
        pair_lookup = {
            tuple(sorted((str(row["left_entity_id"]), str(row["right_entity_id"])))): row for row in pair_rows
        }
        expected_representatives = {}
        for index, members in enumerate(expected_clusters, 1):
            mean_distances = {}
            for member in members:
                current = []
                for other in members:
                    if member == other:
                        continue
                    row = pair_lookup[tuple(sorted((member, other)))]
                    correlations = [
                        abs(float(value))
                        for value in (
                            row["mean_daily_spearman_value_correlation"],
                            row["daily_rank_ic_correlation"],
                        )
                        if value is not None
                    ]
                    current.append(1 - max(correlations, default=0.0))
                mean_distances[member] = sum(current) / len(current) if current else 0.0
            expected_representatives[f"CLUSTER-{index:03d}"] = min(
                members,
                key=lambda item: (
                    mean_distances[item],
                    -coverage[item],
                    VARIANT_PRIORITY[item.split("|", 1)[0]],
                    item,
                ),
            )
        if any(
            representative != expected_representatives[cluster_id]
            for cluster_id, _, representative in stored_cluster_rows
        ):
            failures.append("independent mechanical representative selection differs")
        conditional_dimensions = connection.execute(
            f"""SELECT count(*),count(DISTINCT (candidate_entity_id,session)),
            count(*) FILTER (WHERE abs(raw_rank_ic)>1 OR abs(conditional_rank_ic)>1
              OR abs(orthogonal_rank_ic)>1 OR incremental_r_squared<0 OR incremental_r_squared>1)
            FROM read_parquet('{_sql_path(paths["daily_conditional_rank_ic"])}')"""
        ).fetchone()
        if conditional_dimensions[0] != conditional_dimensions[1] or conditional_dimensions[2]:
            failures.append("conditional RankIC uniqueness or bounds failed")

        sample_session = connection.execute(
            f"""SELECT max(session) FROM read_parquet('{_sql_path(paths["daily_value_correlations"])}')"""
        ).fetchone()[0]
        collapsed = next((row for row in stored_dedup if not row[2]), None)
        if collapsed is None:
            sample_left, sample_right = pair_rows[0]["left_entity_id"], pair_rows[0]["right_entity_id"]
        else:
            sample_left, sample_right = sorted((collapsed[0], collapsed[1]))
        left_variant, left_factor, left_version = str(sample_left).split("|", 2)
        right_variant, right_factor, right_version = str(sample_right).split("|", 2)
        sample_values = connection.execute(
            f"""SELECT l.value,r.value FROM read_parquet('{_sql_path(factor_paths[left_variant])}') l
            JOIN read_parquet('{_sql_path(factor_paths[right_variant])}') r USING(session,instrument_id)
            WHERE l.session=? AND l.factor_id=? AND l.factor_version=?
              AND r.factor_id=? AND r.factor_version=?
              AND l.value IS NOT NULL AND r.value IS NOT NULL ORDER BY l.instrument_id""",
            [sample_session, left_factor, left_version, right_factor, right_version],
        ).fetchall()
        independent_value_correlation = float(
            np.corrcoef(
                _ranks([row[0] for row in sample_values]),
                _ranks([row[1] for row in sample_values]),
            )[0, 1]
        )
        stored_value_correlation = connection.execute(
            f"""SELECT spearman_value_correlation
            FROM read_parquet('{_sql_path(paths["daily_value_correlations"])}')
            WHERE session=? AND left_entity_id=? AND right_entity_id=?""",
            [sample_session, sample_left, sample_right],
        ).fetchone()[0]
        if not _close(independent_value_correlation, stored_value_correlation):
            failures.append("independent factor-value Spearman correlation differs")

        ic_pairs = connection.execute(
            f"""SELECT a.rank_ic,b.rank_ic FROM read_parquet('{_sql_path(daily_ic_path)}') a
            JOIN read_parquet('{_sql_path(daily_ic_path)}') b USING(session)
            WHERE a.variant=? AND a.factor_id=? AND a.factor_version=?
              AND b.variant=? AND b.factor_id=? AND b.factor_version=?
              AND a.rank_ic IS NOT NULL AND b.rank_ic IS NOT NULL
            ORDER BY session""",
            [left_variant, left_factor, left_version, right_variant, right_factor, right_version],
        ).fetchall()
        independent_ic_correlation = float(
            np.corrcoef([row[0] for row in ic_pairs], [row[1] for row in ic_pairs])[0, 1]
        )
        stored_ic_correlation = connection.execute(
            f"""SELECT daily_rank_ic_correlation FROM read_parquet('{_sql_path(paths["pair_correlations"])}')
            WHERE left_entity_id=? AND right_entity_id=?""",
            [sample_left, sample_right],
        ).fetchone()[0]
        if not _close(independent_ic_correlation, stored_ic_correlation):
            failures.append("independent daily-IC correlation differs")

        target = connection.execute(
            f"""SELECT * FROM read_parquet('{_sql_path(paths["daily_conditional_rank_ic"])}')
            ORDER BY session DESC,candidate_entity_id LIMIT 1"""
        ).fetchone()
        target_columns = [item[0] for item in connection.description]
        target = dict(zip(target_columns, target, strict=True))
        conditional_sample_session = target["session"]
        controls = json.loads(target["control_entity_ids_json"])
        selected_entities = [target["candidate_entity_id"], *controls]
        factor_rows = connection.execute(
            f"""SELECT variant,factor_id,factor_version,instrument_id,value
            FROM read_parquet([{",".join(repr(_sql_path(path)) for path in factor_paths.values())}],union_by_name=true)
            WHERE session=? AND value IS NOT NULL""",
            [conditional_sample_session],
        ).fetchall()
        values: dict[str, dict[str, float]] = {entity_id: {} for entity_id in selected_entities}
        for variant, factor_id, version, instrument, value in factor_rows:
            entity_id = f"{variant}|{factor_id}|{version}"
            if entity_id in values:
                values[entity_id][instrument] = value
        label_rows = connection.execute(
            f"""SELECT instrument_id,value FROM read_parquet('{_sql_path(label_path)}')
            WHERE signal_session=? AND is_valid AND value IS NOT NULL""",
            [conditional_sample_session],
        ).fetchall()
        labels = dict(label_rows)
        common = set(labels)
        for entity_values in values.values():
            common &= set(entity_values)
        instruments = sorted(common)
        ranked_values = {}
        for entity_id in selected_entities:
            entity_instruments = sorted(values[entity_id])
            entity_ranks = _ranks([values[entity_id][instrument] for instrument in entity_instruments])
            ranked_values[entity_id] = dict(zip(entity_instruments, entity_ranks, strict=True))
        label_instruments = sorted(labels)
        all_label_ranks = _ranks([labels[instrument] for instrument in label_instruments])
        ranked_labels = dict(zip(label_instruments, all_label_ranks, strict=True))
        rank_columns = [
            np.array([ranked_values[entity_id][instrument] for instrument in instruments])
            for entity_id in selected_entities
        ]
        label_ranks = np.array([ranked_labels[instrument] for instrument in instruments])
        correlation = np.corrcoef(np.column_stack((rank_columns[0], label_ranks, *rank_columns[1:])), rowvar=False)
        independent_partial = _independent_partial(correlation, manifest.request.evaluation.ridge_regularization)
        stored_partial = tuple(
            target[name]
            for name in (
                "conditional_rank_ic",
                "orthogonal_rank_ic",
                "baseline_r_squared",
                "full_r_squared",
                "incremental_r_squared",
            )
        )
        if any(not _close(left, right) for left, right in zip(independent_partial, stored_partial, strict=True)):
            failures.append("independent conditional/orthogonal RankIC calculation differs")

        focus_rows = connection.execute(
            f"""SELECT candidate_entity_id,strict_incremental_outcome
            FROM read_parquet('{_sql_path(paths["incremental_value_summary"])}')
            WHERE focus_hypothesis<>'NONE' ORDER BY candidate_entity_id"""
        ).fetchall()

    collapsed_count = manifest.entity_count - manifest.canonical_entity_count
    findings = (
        f"{collapsed_count} configured factor-variant paths collapse under both frozen duplicate thresholds.",
        (
            f"The {manifest.canonical_entity_count} canonical paths form {manifest.cluster_count} unsupervised "
            "clusters; representatives are diagnostics and are not promoted."
        ),
        f"{len(focus_rows)} configured focus paths were audited without treating them as independent discoveries.",
        "Configured post-selection direction overrides remain diagnostics rather than confirmatory hypotheses.",
        (
            f"The {manifest.request.window.start.isoformat()} to {manifest.request.window.end.isoformat()} interval "
            "is exposed research data, not a new holdout or OOS confirmation."
        ),
    )
    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "redundancy_id": redundancy_id,
        "entity_count": manifest.entity_count,
        "canonical_entity_count": manifest.canonical_entity_count,
        "cluster_count": manifest.cluster_count,
        "conditional_hypothesis_count": manifest.conditional_hypothesis_count,
        "factor_value_crosscheck_session": sample_session.isoformat(),
        "factor_value_crosscheck_pair": [sample_left, sample_right],
        "conditional_crosscheck_session": conditional_sample_session.isoformat(),
        "conditional_crosscheck_entity": target["candidate_entity_id"],
        "focus_outcomes": focus_rows,
        "findings": findings,
        "failures": failures,
        "status": "PASS_WITH_FINDINGS" if not failures else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--factor-store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--redundancy-id", default=DEFAULT_REDUNDANCY_ID)
    parser.add_argument("--output", type=Path, default=Path("reports/m4_5_redundancy_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.factor_store, args.evidence_store, args.redundancy_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
