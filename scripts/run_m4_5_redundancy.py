"""Publish M4.5 factor redundancy, clustering, and incremental-value evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import uuid
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from alpha_research_os.evaluation import (
    EvidenceFile,
    LabelReleaseManifest,
    RedundancyEvaluationSpec,
    RedundancyEvidenceManifest,
    RedundancyEvidenceRequest,
    WalkForwardEvidenceManifest,
    average_linkage_clusters,
    benjamini_hochberg,
    hierarchical_average_linkage,
    moving_block_bootstrap_mean,
    newey_west_mean_test,
    partial_rank_metrics,
)
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash
from alpha_research_os.kernel.specs import DateRange

ENGINE_VERSION = "duckdb-numpy-factor-redundancy-1.0.0"
FAMILY_ID = "M4-5-ORTHOGONAL-RANKIC-EXPOSED-2020-2025-ALL-CANONICAL-v1"
SOURCE_WALK_FORWARD_ID = "sha256:a32e6aa8bdfa962280b7cac5fdedfe0be4dd98b620a0295eec65b2956999a95e"
VARIANT_PRIORITY = {"RAW": 0, "SIZE_NEUTRALIZED": 1, "WINSORIZED_ZSCORE": 2}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp.parquet")
    pq.write_table(pa.Table.from_pylist(rows), temporary, compression="zstd", compression_level=6)
    os.replace(temporary, path)


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _entity(variant: str, factor_id: str, version: str) -> str:
    return f"{variant}|{factor_id}|{version}"


def _split_entity(entity_id: str) -> tuple[str, str, str]:
    variant, factor_id, version = entity_id.split("|", 2)
    return variant, factor_id, version


def _factor_path(factor_store: Path, reference: Any) -> Path:
    kind = "releases" if reference.variant == "RAW" else "processed_releases"
    filename = "raw_factor_values.parquet" if reference.variant == "RAW" else "processed_factor_values.parquet"
    return factor_store / kind / reference.release_id.removeprefix("sha256:") / filename


def _source_inputs(
    database: Path, factor_store: Path, evidence_store: Path, source_walk_forward_id: str
) -> tuple[WalkForwardEvidenceManifest, Path, LabelReleaseManifest, Path, dict[str, Path]]:
    directory = evidence_store / "walk_forward" / source_walk_forward_id.removeprefix("sha256:")
    source = WalkForwardEvidenceManifest.model_validate_json((directory / "manifest.json").read_bytes())
    if source.walk_forward_id != source_walk_forward_id:
        raise ValueError("M4.4 source identity mismatch")
    source_paths = {item.name: evidence_store / item.relative_path for item in source.files}
    daily_path = source_paths["daily_rank_ic"]
    label_directory = evidence_store / "labels" / source.request.label_release_id.removeprefix("sha256:")
    label_manifest = LabelReleaseManifest.model_validate_json((label_directory / "manifest.json").read_bytes())
    label_path = evidence_store / label_manifest.parquet_relative_path
    factor_paths = {
        reference.variant: _factor_path(factor_store, reference) for reference in source.request.factor_inputs
    }
    for item in source.files:
        if _sha256_file(evidence_store / item.relative_path) != item.artifact_hash:
            raise ValueError(f"M4.4 source file hash mismatch: {item.name}")
    for reference in source.request.factor_inputs:
        if _sha256_file(factor_paths[reference.variant]) != reference.parquet_hash:
            raise ValueError(f"factor input hash mismatch: {reference.variant}")
    if _sha256_file(label_path) != label_manifest.parquet_hash:
        raise ValueError("label input hash mismatch")
    with duckdb.connect(str(database), read_only=True) as connection:
        registered = connection.execute(
            "SELECT manifest_hash FROM metadata.walk_forward_evidence_manifest WHERE walk_forward_id=?",
            [source_walk_forward_id],
        ).fetchone()
    if registered != (content_hash(source),):
        raise ValueError("M4.4 source is not registered with its manifest hash")
    return source, daily_path, label_manifest, label_path, factor_paths


def _exposure_snapshot(database: Path) -> tuple[str, int]:
    with duckdb.connect(str(database), read_only=True) as connection:
        rows = connection.execute(
            """SELECT event_id,walk_forward_id,fold_id,test_start,test_end,prior_exposure_status,
              event_type,recorded_at FROM metadata.holdout_exposure_ledger ORDER BY event_id"""
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


def _request(
    database: Path,
    source: WalkForwardEvidenceManifest,
    label_manifest: LabelReleaseManifest,
    family_id: str,
) -> RedundancyEvidenceRequest:
    ledger_hash, ledger_count = _exposure_snapshot(database)
    return RedundancyEvidenceRequest(
        engine_version=ENGINE_VERSION,
        multiple_testing_family_id=family_id,
        source_walk_forward_id=source.walk_forward_id,
        source_walk_forward_manifest_hash=content_hash(source),
        factor_inputs=source.request.factor_inputs,
        label_release_id=label_manifest.release_id,
        label_manifest_hash=content_hash(label_manifest),
        window=source.request.window,
        exposure_ledger_snapshot_hash=ledger_hash,
        exposure_ledger_row_count=ledger_count,
        evaluation=RedundancyEvaluationSpec(),
    )


def _entities(daily_path: Path) -> list[str]:
    with duckdb.connect() as connection:
        rows = connection.execute(
            f"""SELECT DISTINCT variant,factor_id,factor_version
            FROM read_parquet('{_sql_path(daily_path)}') ORDER BY 1,2,3"""
        ).fetchall()
    return [_entity(*row) for row in rows]


def _month_end_sessions(daily_path: Path) -> list[date]:
    with duckdb.connect() as connection:
        rows = connection.execute(
            f"""SELECT max(session) FROM read_parquet('{_sql_path(daily_path)}')
            GROUP BY year(session),month(session) ORDER BY 1"""
        ).fetchall()
    return [row[0] for row in rows]


def _wide_factor_sql(factor_paths: dict[str, Path], entities: list[str], sessions: list[date]) -> str:
    paths = ",".join(_sql_string(_sql_path(path)) for path in factor_paths.values())
    dates = ",".join(f"DATE '{session.isoformat()}'" for session in sessions)
    columns = []
    for index, entity_id in enumerate(entities):
        variant, factor_id, version = _split_entity(entity_id)
        condition = (
            f"variant={_sql_string(variant)} AND factor_id={_sql_string(factor_id)} "
            f"AND factor_version={_sql_string(version)}"
        )
        columns.append(f"max(CASE WHEN {condition} THEN factor_rank END) e{index:03d}")
    return f"""WITH selected AS (
      SELECT session,instrument_id,variant,factor_id,factor_version,value
      FROM read_parquet([{paths}],union_by_name=true)
      WHERE session IN ({dates}) AND value IS NOT NULL
    ), rank_base AS (
      SELECT *,rank() OVER (PARTITION BY session,variant,factor_id,factor_version ORDER BY value) rank_min,
        count(*) OVER (PARTITION BY session,variant,factor_id,factor_version,value) tie_count
      FROM selected
    ), ranked AS (
      SELECT *,rank_min+(tie_count-1)/2.0 factor_rank FROM rank_base
    )
    SELECT session,instrument_id,{",".join(columns)} FROM ranked
    GROUP BY session,instrument_id ORDER BY session,instrument_id"""


def _materialize_sample_matrix(
    target: Path, factor_paths: dict[str, Path], entities: list[str], sessions: list[date]
) -> None:
    if target.exists():
        return
    temporary = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.parquet")
    sql = _wide_factor_sql(factor_paths, entities, sessions)
    with duckdb.connect() as connection:
        connection.execute("SET memory_limit='10GB'")
        connection.execute(
            f"COPY ({sql}) TO '{_sql_path(temporary)}' (FORMAT PARQUET,COMPRESSION ZSTD,COMPRESSION_LEVEL 6)"
        )
    os.replace(temporary, target)


def _correlation(left: np.ndarray, right: np.ndarray) -> tuple[int, float | None]:
    mask = np.isfinite(left) & np.isfinite(right)
    count = int(mask.sum())
    if count < 3:
        return count, None
    x = left[mask]
    y = right[mask]
    if np.std(x) == 0 or np.std(y) == 0:
        return count, None
    return count, float(np.corrcoef(x, y)[0, 1])


def _sample_correlations(
    sample_path: Path, entities: list[str]
) -> tuple[list[dict[str, Any]], dict[tuple[str, str], list[float]]]:
    table = pq.read_table(sample_path)
    sessions = table.column("session").to_numpy(zero_copy_only=False)
    unique_sessions, starts = np.unique(sessions, return_index=True)
    ends = np.r_[starts[1:], len(sessions)]
    arrays = [table.column(f"e{index:03d}").to_numpy(zero_copy_only=False) for index in range(len(entities))]
    rows: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str], list[float]] = defaultdict(list)
    for session, start, end in zip(unique_sessions, starts, ends, strict=True):
        for left_index, left_entity in enumerate(entities[:-1]):
            for right_index in range(left_index + 1, len(entities)):
                right_entity = entities[right_index]
                count, value = _correlation(arrays[left_index][start:end], arrays[right_index][start:end])
                rows.append(
                    {
                        "session": session.astype("datetime64[D]").astype(object),
                        "left_entity_id": left_entity,
                        "right_entity_id": right_entity,
                        "paired_count": count,
                        "spearman_value_correlation": value,
                    }
                )
                if value is not None:
                    grouped[(left_entity, right_entity)].append(value)
    return rows, grouped


def _ic_series(daily_path: Path) -> dict[str, dict[date, float]]:
    result: dict[str, dict[date, float]] = defaultdict(dict)
    with duckdb.connect() as connection:
        rows = connection.execute(
            f"""SELECT variant,factor_id,factor_version,session,rank_ic
            FROM read_parquet('{_sql_path(daily_path)}') WHERE rank_ic IS NOT NULL ORDER BY 1,2,3,4"""
        ).fetchall()
    for variant, factor_id, version, session, value in rows:
        result[_entity(variant, factor_id, version)][session] = float(value)
    return result


def _pair_summary(
    entities: list[str], grouped_values: dict[tuple[str, str], list[float]], daily_path: Path
) -> list[dict[str, Any]]:
    series = _ic_series(daily_path)
    rows = []
    for left_index, left in enumerate(entities[:-1]):
        for right in entities[left_index + 1 :]:
            values = grouped_values.get((left, right), [])
            common = sorted(set(series[left]) & set(series[right]))
            _, ic_correlation = _correlation(
                np.array([series[left][session] for session in common]),
                np.array([series[right][session] for session in common]),
            )
            rows.append(
                {
                    "left_entity_id": left,
                    "right_entity_id": right,
                    "value_sample_session_count": len(values),
                    "mean_daily_spearman_value_correlation": statistics.fmean(values) if values else None,
                    "median_daily_spearman_value_correlation": statistics.median(values) if values else None,
                    "minimum_daily_spearman_value_correlation": min(values) if values else None,
                    "maximum_daily_spearman_value_correlation": max(values) if values else None,
                    "daily_rank_ic_pair_count": len(common),
                    "daily_rank_ic_correlation": ic_correlation,
                }
            )
    return rows


def _duplicate_membership(
    entities: list[str], pair_rows: list[dict[str, Any]], spec: RedundancyEvaluationSpec
) -> list[dict[str, Any]]:
    parent = {entity_id: entity_id for entity_id in entities}

    def find(item: str) -> str:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    for row in pair_rows:
        left, right = row["left_entity_id"], row["right_entity_id"]
        if _split_entity(left)[1:] != _split_entity(right)[1:]:
            continue
        value_correlation = row["mean_daily_spearman_value_correlation"]
        ic_correlation = row["daily_rank_ic_correlation"]
        if (
            value_correlation is not None
            and ic_correlation is not None
            and value_correlation >= spec.duplicate_value_correlation_threshold
            and ic_correlation >= spec.duplicate_ic_correlation_threshold
        ):
            union(left, right)
    components: dict[str, list[str]] = defaultdict(list)
    for entity_id in entities:
        components[find(entity_id)].append(entity_id)
    ordered_components = sorted(tuple(sorted(members)) for members in components.values())
    rows = []
    for index, members in enumerate(ordered_components, 1):
        canonical = min(
            members,
            key=lambda item: (VARIANT_PRIORITY[_split_entity(item)[0]], item),
        )
        for entity_id in members:
            variant, factor_id, version = _split_entity(entity_id)
            rows.append(
                {
                    "entity_id": entity_id,
                    "variant": variant,
                    "factor_id": factor_id,
                    "factor_version": version,
                    "duplicate_group_id": f"DUP-{index:03d}",
                    "canonical_entity_id": canonical,
                    "is_canonical": entity_id == canonical,
                    "member_count": len(members),
                    "decision": "KEEP_CANONICAL" if entity_id == canonical else "COLLAPSE_NEAR_DUPLICATE",
                }
            )
    return sorted(rows, key=lambda item: item["entity_id"])


def _pair_lookup(pair_rows: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {tuple(sorted((row["left_entity_id"], row["right_entity_id"]))): row for row in pair_rows}


def _clustering(
    canonical: list[str], pair_rows: list[dict[str, Any]], coverage: dict[str, float], spec: RedundancyEvaluationSpec
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    lookup = _pair_lookup(pair_rows)
    distances = {}
    for left_index, left in enumerate(canonical[:-1]):
        for right in canonical[left_index + 1 :]:
            row = lookup[tuple(sorted((left, right)))]
            correlations = [
                abs(float(value))
                for value in (
                    row["mean_daily_spearman_value_correlation"],
                    row["daily_rank_ic_correlation"],
                )
                if value is not None and math.isfinite(float(value))
            ]
            distances[(left, right)] = 1.0 - max(correlations, default=0.0)
    linkage = [
        {
            "step": merge.step,
            "left_members_json": json.dumps(merge.left_members, separators=(",", ":")),
            "right_members_json": json.dumps(merge.right_members, separators=(",", ":")),
            "distance": merge.distance,
            "merged_members_json": json.dumps(merge.merged_members, separators=(",", ":")),
            "merged_member_count": len(merge.merged_members),
        }
        for merge in hierarchical_average_linkage(canonical, distances)
    ]
    clusters = average_linkage_clusters(canonical, distances, threshold=spec.cluster_distance_threshold)
    membership = []
    for index, members in enumerate(clusters, 1):
        mean_distances = {
            member: statistics.fmean(distances[tuple(sorted((member, other)))] for other in members if other != member)
            if len(members) > 1
            else 0.0
            for member in members
        }
        representative = min(
            members,
            key=lambda item: (
                mean_distances[item],
                -coverage.get(item, 0.0),
                VARIANT_PRIORITY[_split_entity(item)[0]],
                item,
            ),
        )
        for member in members:
            variant, factor_id, version = _split_entity(member)
            membership.append(
                {
                    "cluster_id": f"CLUSTER-{index:03d}",
                    "entity_id": member,
                    "variant": variant,
                    "factor_id": factor_id,
                    "factor_version": version,
                    "cluster_member_count": len(members),
                    "mean_distance_to_cluster_members": mean_distances[member],
                    "mean_daily_coverage": coverage.get(member),
                    "representative_entity_id": representative,
                    "is_representative": member == representative,
                    "selection_status": "REPRESENTATIVE_NOT_PROMOTED" if member == representative else "CLUSTER_MEMBER",
                }
            )
    return linkage, sorted(membership, key=lambda item: (item["cluster_id"], item["entity_id"]))


def _coverage(daily_path: Path) -> dict[str, float]:
    with duckdb.connect() as connection:
        rows = connection.execute(
            f"""SELECT variant,factor_id,factor_version,avg(coverage)
            FROM read_parquet('{_sql_path(daily_path)}') GROUP BY 1,2,3"""
        ).fetchall()
    return {_entity(*row[:3]): float(row[3]) for row in rows}


def _year_wide_sql(factor_paths: dict[str, Path], label_path: Path, entities: list[str], year: int) -> str:
    paths = ",".join(_sql_string(_sql_path(path)) for path in factor_paths.values())
    columns = []
    for index, entity_id in enumerate(entities):
        variant, factor_id, version = _split_entity(entity_id)
        condition = (
            f"variant={_sql_string(variant)} AND factor_id={_sql_string(factor_id)} "
            f"AND factor_version={_sql_string(version)}"
        )
        columns.append(f"max(CASE WHEN {condition} THEN factor_rank END) e{index:03d}")
    return f"""WITH selected AS (
      SELECT session,instrument_id,variant,factor_id,factor_version,value
      FROM read_parquet([{paths}],union_by_name=true)
      WHERE year(session)={year} AND value IS NOT NULL
    ), factor_rank_base AS (
      SELECT *,rank() OVER (PARTITION BY session,variant,factor_id,factor_version ORDER BY value) rank_min,
        count(*) OVER (PARTITION BY session,variant,factor_id,factor_version,value) tie_count
      FROM selected
    ), factor_wide AS (
      SELECT session,instrument_id,{",".join(columns)}
      FROM (SELECT *,rank_min+(tie_count-1)/2.0 factor_rank FROM factor_rank_base)
      GROUP BY session,instrument_id
    ), labels AS (
      SELECT signal_session AS session,instrument_id,value
      FROM read_parquet('{_sql_path(label_path)}')
      WHERE year(signal_session)={year} AND is_valid AND value IS NOT NULL
    ), label_rank_base AS (
      SELECT *,rank() OVER (PARTITION BY session ORDER BY value) rank_min,
        count(*) OVER (PARTITION BY session,value) tie_count FROM labels
    )
    SELECT f.*,l.rank_min+(l.tie_count-1)/2.0 label_rank
    FROM factor_wide f JOIN label_rank_base l USING(session,instrument_id)
    ORDER BY session,instrument_id"""


def _process_year_matrix(
    path: Path,
    matrix_entities: list[str],
    candidate_entities: list[str],
    cluster_by_entity: dict[str, str],
    representatives: dict[str, str],
    spec: RedundancyEvaluationSpec,
) -> list[dict[str, Any]]:
    table = pq.read_table(path)
    sessions = table.column("session").to_numpy(zero_copy_only=False)
    unique_sessions, starts = np.unique(sessions, return_index=True)
    ends = np.r_[starts[1:], len(sessions)]
    arrays = {
        entity_id: table.column(f"e{index:03d}").to_numpy(zero_copy_only=False)
        for index, entity_id in enumerate(matrix_entities)
    }
    label = table.column("label_rank").to_numpy(zero_copy_only=False)
    rows = []
    for session, start, end in zip(unique_sessions, starts, ends, strict=True):
        y = label[start:end]
        for candidate in candidate_entities:
            candidate_cluster = cluster_by_entity[candidate]
            controls = [
                representatives[cluster_id] for cluster_id in sorted(representatives) if cluster_id != candidate_cluster
            ]
            matrix_columns = [arrays[candidate][start:end], y, *[arrays[item][start:end] for item in controls]]
            complete = np.ones(end - start, dtype=bool)
            for column in matrix_columns:
                complete &= np.isfinite(column)
            paired_count = int(complete.sum())
            if paired_count < spec.minimum_pairs_per_session:
                continue
            matrix = np.column_stack([column[complete] for column in matrix_columns])
            if np.any(np.std(matrix, axis=0) == 0):
                continue
            correlation = np.corrcoef(matrix, rowvar=False)
            metrics = partial_rank_metrics(correlation, ridge=spec.ridge_regularization)
            rows.append(
                {
                    "session": session.astype("datetime64[D]").astype(object),
                    "candidate_entity_id": candidate,
                    "candidate_cluster_id": candidate_cluster,
                    "control_entity_ids_json": json.dumps(controls, separators=(",", ":")),
                    "control_count": len(controls),
                    "paired_count": paired_count,
                    "raw_rank_ic": float(correlation[0, 1]),
                    "conditional_rank_ic": metrics.conditional_rank_ic,
                    "orthogonal_rank_ic": metrics.orthogonal_rank_ic,
                    "baseline_r_squared": metrics.baseline_r_squared,
                    "full_r_squared": metrics.full_r_squared,
                    "incremental_r_squared": metrics.incremental_r_squared,
                    "condition_number": metrics.condition_number,
                }
            )
    return rows


def _daily_conditional(
    database: Path,
    evidence_store: Path,
    directory: Path,
    factor_paths: dict[str, Path],
    label_path: Path,
    matrix_entities: list[str],
    candidate_entities: list[str],
    cluster_rows: list[dict[str, Any]],
    spec: RedundancyEvaluationSpec,
    window: DateRange,
) -> list[dict[str, Any]]:
    cluster_by_entity = {row["entity_id"]: row["cluster_id"] for row in cluster_rows}
    representatives = {row["cluster_id"]: row["entity_id"] for row in cluster_rows if row["is_representative"]}
    staging = directory / "conditional_staging"
    staging.mkdir(parents=True, exist_ok=True)
    rows = []
    for year in range(window.start.year, window.end.year + 1):
        path = staging / f"wide-{year}.parquet"
        if not path.exists():
            print(f"conditional year={year} materializing", flush=True)
            temporary = path.with_name(f".{path.stem}.{uuid.uuid4().hex}.tmp.parquet")
            sql = _year_wide_sql(factor_paths, label_path, matrix_entities, year)
            with duckdb.connect(str(database), read_only=True) as connection:
                connection.execute("SET memory_limit='10GB'")
                connection.execute(f"SET temp_directory='{_sql_path(evidence_store / 'duckdb_tmp')}'")
                connection.execute(
                    f"COPY ({sql}) TO '{_sql_path(temporary)}' (FORMAT PARQUET,COMPRESSION ZSTD,COMPRESSION_LEVEL 6)"
                )
            os.replace(temporary, path)
        rows.extend(
            _process_year_matrix(
                path,
                matrix_entities,
                candidate_entities,
                cluster_by_entity,
                representatives,
                spec,
            )
        )
    for path in staging.glob("*.parquet"):
        path.unlink()
    staging.rmdir()
    return sorted(rows, key=lambda item: (item["candidate_entity_id"], item["session"]))


def _directions(database: Path) -> dict[tuple[str, str], str]:
    with duckdb.connect(str(database), read_only=True) as connection:
        rows = connection.execute("SELECT factor_id,factor_version,spec_json FROM metadata.factor_registry").fetchall()
    return {(factor_id, version): json.loads(payload)["direction"] for factor_id, version, payload in rows}


def _orientation(
    entity_id: str,
    factor_id: str,
    version: str,
    directions: dict[tuple[str, str], str],
    overrides: dict[str, dict[str, Any]],
) -> tuple[int, str, str]:
    override = overrides.get(entity_id, overrides.get(factor_id))
    if override is not None:
        multiplier = int(override["multiplier"])
        if multiplier not in {-1, 1}:
            raise ValueError(f"direction override multiplier must be -1 or 1: {entity_id}")
        return multiplier, str(override["direction_source"]), str(override.get("focus_hypothesis", "NONE"))
    declared = directions[(factor_id, version)]
    if declared == "POSITIVE":
        return 1, "DECLARED_POSITIVE", "NONE"
    if declared == "NEGATIVE":
        return -1, "DECLARED_NEGATIVE", "NONE"
    return 1, "UNDIRECTED_DIAGNOSTIC", "NONE"


def _incremental_summary(
    database: Path,
    request: RedundancyEvidenceRequest,
    daily_rows: list[dict[str, Any]],
    cluster_rows: list[dict[str, Any]],
    direction_overrides: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in daily_rows:
        grouped[row["candidate_entity_id"]].append(row)
    membership = {row["entity_id"]: row for row in cluster_rows}
    directions = _directions(database)
    summaries = []
    inference = request.evaluation.inference
    for entity_id, observations in sorted(grouped.items()):
        variant, factor_id, version = _split_entity(entity_id)
        multiplier, direction_source, focus = _orientation(
            entity_id, factor_id, version, directions, direction_overrides
        )
        directed = [multiplier * row["orthogonal_rank_ic"] for row in observations]
        hac = newey_west_mean_test(directed, max_lag=inference.hac_max_lag)
        seed = int.from_bytes(hashlib.sha256(f"{inference.random_seed}|{entity_id}".encode()).digest()[:4], "big")
        bootstrap = moving_block_bootstrap_mean(
            directed,
            block_length=inference.bootstrap_block_length,
            resamples=inference.bootstrap_resamples,
            seed=seed,
            confidence_level=inference.bootstrap_confidence_level,
        )
        summaries.append(
            {
                "redundancy_id": request.redundancy_id,
                "multiple_testing_family_id": request.multiple_testing_family_id,
                "candidate_entity_id": entity_id,
                "variant": variant,
                "factor_id": factor_id,
                "factor_version": version,
                "cluster_id": membership[entity_id]["cluster_id"],
                "is_cluster_representative": membership[entity_id]["is_representative"],
                "direction_multiplier": multiplier,
                "direction_source": direction_source,
                "focus_hypothesis": focus,
                "session_count": len(observations),
                "mean_raw_rank_ic": statistics.fmean(row["raw_rank_ic"] for row in observations),
                "mean_conditional_rank_ic": statistics.fmean(row["conditional_rank_ic"] for row in observations),
                "mean_orthogonal_rank_ic_raw": statistics.fmean(row["orthogonal_rank_ic"] for row in observations),
                "mean_orthogonal_rank_ic_directed": hac.mean,
                "mean_incremental_r_squared": statistics.fmean(row["incremental_r_squared"] for row in observations),
                "median_incremental_r_squared": statistics.median(row["incremental_r_squared"] for row in observations),
                "hac_standard_error": hac.standard_error,
                "hac_z_statistic": hac.z_statistic,
                "hac_p_value_two_sided": hac.p_value_two_sided,
                "bootstrap_p_value_two_sided": bootstrap.p_value_two_sided,
                "bootstrap_confidence_lower": bootstrap.confidence_lower,
                "bootstrap_confidence_upper": bootstrap.confidence_upper,
                "bootstrap_seed": bootstrap.seed,
                "sample_classification": request.sample_classification,
            }
        )
    hac_q = benjamini_hochberg([row["hac_p_value_two_sided"] for row in summaries])
    bootstrap_q = benjamini_hochberg([row["bootstrap_p_value_two_sided"] for row in summaries])
    for row, hac_value, bootstrap_value in zip(summaries, hac_q, bootstrap_q, strict=True):
        row["hac_bh_q_value"] = hac_value
        row["bootstrap_bh_q_value"] = bootstrap_value
        row["hac_fdr_reject"] = hac_value is not None and hac_value <= inference.fdr_alpha
        row["bootstrap_fdr_reject"] = bootstrap_value is not None and bootstrap_value <= inference.fdr_alpha
        if row["direction_source"] == "UNDIRECTED_DIAGNOSTIC":
            outcome = "UNDIRECTED_DIAGNOSTIC"
        elif row["hac_fdr_reject"] and row["bootstrap_fdr_reject"]:
            outcome = (
                "INCREMENTAL_DIRECTION_SUPPORTED"
                if row["mean_orthogonal_rank_ic_directed"] > 0
                else "INCREMENTAL_DIRECTION_CONTRADICTED"
            )
        else:
            outcome = "NO_INCREMENTAL_EVIDENCE"
        row["strict_incremental_outcome"] = outcome
        row["promotion_status"] = "NOT_ELIGIBLE_M4_5_DIAGNOSTIC"
    focus = [row for row in summaries if row["focus_hypothesis"] != "NONE"]
    family = {
        "redundancy_id": request.redundancy_id,
        "multiple_testing_family_id": request.multiple_testing_family_id,
        "hypothesis_count": len(summaries),
        "hac_fdr_rejection_count": sum(row["hac_fdr_reject"] for row in summaries),
        "bootstrap_fdr_rejection_count": sum(row["bootstrap_fdr_reject"] for row in summaries),
        "strict_supported_count": sum(
            row["strict_incremental_outcome"] == "INCREMENTAL_DIRECTION_SUPPORTED" for row in summaries
        ),
        "strict_contradicted_count": sum(
            row["strict_incremental_outcome"] == "INCREMENTAL_DIRECTION_CONTRADICTED" for row in summaries
        ),
        "focus_hypothesis_count": len(focus),
        "focus_supported_count": sum(
            row["strict_incremental_outcome"] == "INCREMENTAL_DIRECTION_SUPPORTED" for row in focus
        ),
        "focus_contradicted_count": sum(
            row["strict_incremental_outcome"] == "INCREMENTAL_DIRECTION_CONTRADICTED" for row in focus
        ),
        "decision_status": "NO_PROMOTION_REDUNDANCY_DIAGNOSTIC",
        "sample_classification": request.sample_classification,
    }
    return summaries, family


def _register(database: Path, evidence_store: Path, manifest: RedundancyEvidenceManifest) -> None:
    connection = duckdb.connect(str(database))
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.factor_redundancy_evidence_manifest (
            redundancy_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, source_walk_forward_id VARCHAR,
            multiple_testing_family_id VARCHAR, entity_count BIGINT, canonical_entity_count BIGINT,
            cluster_count BIGINT, conditional_hypothesis_count BIGINT, quality_status VARCHAR,
            decision_status VARCHAR, sample_classification VARCHAR, request_json JSON,
            limitations_json JSON, created_at TIMESTAMPTZ)"""
        )
        manifest_hash = content_hash(manifest)
        existing = connection.execute(
            "SELECT manifest_hash FROM metadata.factor_redundancy_evidence_manifest WHERE redundancy_id=?",
            [manifest.redundancy_id],
        ).fetchone()
        if existing and existing != (manifest_hash,):
            raise ValueError("immutable M4.5 registry conflict")
        connection.execute(
            "INSERT OR IGNORE INTO metadata.factor_redundancy_evidence_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                manifest.redundancy_id,
                manifest_hash,
                manifest.request.source_walk_forward_id,
                manifest.request.multiple_testing_family_id,
                manifest.entity_count,
                manifest.canonical_entity_count,
                manifest.cluster_count,
                manifest.conditional_hypothesis_count,
                manifest.quality_status,
                manifest.decision_status,
                manifest.request.sample_classification,
                json.dumps(manifest.request.model_dump(mode="json"), separators=(",", ":")),
                json.dumps(manifest.limitations, ensure_ascii=False, separators=(",", ":")),
                manifest.created_at,
            ],
        )
        root = _sql_path(evidence_store / "redundancy" / manifest.redundancy_id.removeprefix("sha256:"))
        views = {
            "raw.factor_value_correlation_daily": "daily_value_correlations.parquet",
            "research.factor_correlation_summary": "pair_correlations.parquet",
            "research.factor_variant_deduplication": "variant_deduplication.parquet",
            "raw.factor_hierarchical_linkage": "hierarchical_linkage.parquet",
            "research.factor_clusters": "factor_clusters.parquet",
            "raw.factor_conditional_rank_ic_daily": "daily_conditional_rank_ic.parquet",
            "research.factor_incremental_value_summary": "incremental_value_summary.parquet",
            "research.factor_redundancy_family_summary": "family_summary.parquet",
        }
        for view, filename in views.items():
            connection.execute(
                f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{root}/{filename}',union_by_name=true)"
            )
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def _configuration_family_id(
    family_id: str,
    direction_overrides: dict[str, dict[str, Any]],
    candidate_policy: str,
    bind_configuration_to_asset_identity: bool,
) -> str:
    if not bind_configuration_to_asset_identity:
        return family_id
    configuration_hash = content_hash(
        {"direction_overrides": direction_overrides, "candidate_policy": candidate_policy}
    ).removeprefix("sha256:")[:12]
    return f"{family_id}-cfg-{configuration_hash}"


def publish(
    database: Path,
    factor_store: Path,
    evidence_store: Path,
    *,
    source_walk_forward_id: str = SOURCE_WALK_FORWARD_ID,
    family_id: str = FAMILY_ID,
    direction_overrides: dict[str, dict[str, Any]] | None = None,
    candidate_policy: str = "ALL_CANONICAL",
    bind_configuration_to_asset_identity: bool = True,
) -> dict[str, Any]:
    if candidate_policy not in {"ALL_CANONICAL", "CLUSTER_REPRESENTATIVES_AND_FOCUS"}:
        raise ValueError(f"unknown conditional candidate policy: {candidate_policy}")
    selected_overrides = {} if direction_overrides is None else direction_overrides
    effective_family_id = _configuration_family_id(
        family_id,
        selected_overrides,
        candidate_policy,
        bind_configuration_to_asset_identity,
    )
    source, daily_path, label_manifest, label_path, factor_paths = _source_inputs(
        database, factor_store, evidence_store, source_walk_forward_id
    )
    request = _request(database, source, label_manifest, effective_family_id)
    directory = evidence_store / "redundancy" / request.redundancy_id.removeprefix("sha256:")
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    targets = {
        "daily_conditional_rank_ic": directory / "daily_conditional_rank_ic.parquet",
        "daily_value_correlations": directory / "daily_value_correlations.parquet",
        "factor_clusters": directory / "factor_clusters.parquet",
        "family_summary": directory / "family_summary.parquet",
        "hierarchical_linkage": directory / "hierarchical_linkage.parquet",
        "incremental_value_summary": directory / "incremental_value_summary.parquet",
        "pair_correlations": directory / "pair_correlations.parquet",
        "variant_deduplication": directory / "variant_deduplication.parquet",
    }
    if manifest_path.exists() and all(path.exists() for path in targets.values()):
        manifest = RedundancyEvidenceManifest.model_validate_json(manifest_path.read_bytes())
        hashes = {item.name: item.artifact_hash for item in manifest.files}
        if manifest.request != request or any(_sha256_file(path) != hashes[name] for name, path in targets.items()):
            raise ValueError("cached M4.5 evidence failed immutable verification")
        _register(database, evidence_store, manifest)
        return {"cache_hit": True, "redundancy_id": manifest.redundancy_id, "manifest": str(manifest_path.resolve())}

    entities = _entities(daily_path)
    sample_sessions = _month_end_sessions(daily_path)
    sample_path = directory / "sample_rank_matrix.staging.parquet"
    _materialize_sample_matrix(sample_path, factor_paths, entities, sample_sessions)
    daily_value_rows, grouped_values = _sample_correlations(sample_path, entities)
    pair_rows = _pair_summary(entities, grouped_values, daily_path)
    duplicate_rows = _duplicate_membership(entities, pair_rows, request.evaluation)
    canonical = sorted(row["entity_id"] for row in duplicate_rows if row["is_canonical"])
    linkage_rows, cluster_rows = _clustering(canonical, pair_rows, _coverage(daily_path), request.evaluation)
    _write_parquet(targets["daily_value_correlations"], daily_value_rows)
    _write_parquet(targets["pair_correlations"], pair_rows)
    _write_parquet(targets["variant_deduplication"], duplicate_rows)
    _write_parquet(targets["hierarchical_linkage"], linkage_rows)
    _write_parquet(targets["factor_clusters"], cluster_rows)

    representative_entities = sorted(row["entity_id"] for row in cluster_rows if row["is_representative"])
    focus_entities = sorted(
        entity_id
        for entity_id in canonical
        if (entity_id in selected_overrides or _split_entity(entity_id)[1] in selected_overrides)
        and selected_overrides.get(entity_id, selected_overrides.get(_split_entity(entity_id)[1], {})).get(
            "focus_hypothesis", "NONE"
        )
        != "NONE"
    )
    candidate_entities = (
        canonical if candidate_policy == "ALL_CANONICAL" else sorted(set(representative_entities) | set(focus_entities))
    )
    matrix_entities = sorted(set(representative_entities) | set(candidate_entities))
    daily_conditional = _daily_conditional(
        database,
        evidence_store,
        directory,
        factor_paths,
        label_path,
        matrix_entities,
        candidate_entities,
        cluster_rows,
        request.evaluation,
        request.window,
    )
    summary_rows, family = _incremental_summary(
        database,
        request,
        daily_conditional,
        cluster_rows,
        selected_overrides,
    )
    _write_parquet(targets["daily_conditional_rank_ic"], daily_conditional)
    _write_parquet(targets["incremental_value_summary"], summary_rows)
    _write_parquet(targets["family_summary"], [family])
    sample_path.unlink(missing_ok=True)

    cluster_count = len({row["cluster_id"] for row in cluster_rows})
    quality_failures = []
    expected_pair_count = len(entities) * (len(entities) - 1) // 2
    if len(pair_rows) != expected_pair_count:
        quality_failures.append("unexpected entity or pair count")
    if len(canonical) != len(cluster_rows):
        quality_failures.append("canonical entities do not reconcile to cluster membership")
    if sum(row["is_representative"] for row in cluster_rows) != cluster_count:
        quality_failures.append("each cluster must have exactly one representative")
    if len(summary_rows) != len(candidate_entities):
        quality_failures.append("configured candidates must each have one conditional hypothesis")
    if any(not math.isfinite(row["orthogonal_rank_ic"]) for row in daily_conditional):
        quality_failures.append("non-finite orthogonal RankIC")
    if quality_failures:
        raise ValueError("M4.5 quality gate failed: " + "; ".join(quality_failures))

    files = []
    with duckdb.connect() as connection:
        for name, path in sorted(targets.items()):
            row_count = connection.execute(f"SELECT count(*) FROM read_parquet('{_sql_path(path)}')").fetchone()[0]
            files.append(
                EvidenceFile(
                    name=name,
                    relative_path=path.relative_to(evidence_store).as_posix(),
                    artifact_hash=_sha256_file(path),
                    row_count=row_count,
                )
            )
    manifest = RedundancyEvidenceManifest(
        redundancy_id=request.redundancy_id,
        request=request,
        created_at=datetime.now().astimezone(),
        files=tuple(files),
        entity_count=len(entities),
        canonical_entity_count=len(canonical),
        cluster_count=cluster_count,
        conditional_hypothesis_count=len(summary_rows),
        quality_status="PASS",
        decision_status="NO_PROMOTION_REDUNDANCY_DIAGNOSTIC",
        limitations=(
            (
                f"The complete {request.window.start.isoformat()} to {request.window.end.isoformat()} window is "
                "already exposed research data and is not an unseen holdout."
            ),
            "Month-end factor-value correlations are an unsupervised diagnostic sample, not return evidence.",
            "Cluster representatives are mechanical medoids and are not promoted factors.",
            "Configured post-selection direction overrides remain diagnostics and cannot confirm an Alpha.",
            "Labels remain unaware of price limits, delisting returns, transaction costs, and capacity constraints.",
            "Conditional evidence controls for other cluster representatives but does not prove causal independence.",
        ),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    _register(database, evidence_store, manifest)
    return {
        "cache_hit": False,
        "redundancy_id": manifest.redundancy_id,
        "entity_count": len(entities),
        "canonical_entity_count": len(canonical),
        "cluster_count": cluster_count,
        "conditional_daily_rows": len(daily_conditional),
        "conditional_hypothesis_count": len(summary_rows),
        "focus_supported_count": family["focus_supported_count"],
        "focus_contradicted_count": family["focus_contradicted_count"],
        "decision_status": manifest.decision_status,
        "manifest": str(manifest_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--factor-store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--source-walk-forward-id", default=SOURCE_WALK_FORWARD_ID)
    parser.add_argument("--family-id", default=FAMILY_ID)
    parser.add_argument(
        "--candidate-policy",
        choices=("ALL_CANONICAL", "CLUSTER_REPRESENTATIVES_AND_FOCUS"),
        default="ALL_CANONICAL",
    )
    parser.add_argument("--direction-overrides", type=Path)
    parser.add_argument(
        "--legacy-unbound-configuration",
        action="store_true",
        help="Reproduce a historical asset whose request predates configuration binding.",
    )
    args = parser.parse_args()
    overrides = json.loads(args.direction_overrides.read_text(encoding="utf-8")) if args.direction_overrides else None
    print(
        json.dumps(
            publish(
                args.database,
                args.factor_store,
                args.evidence_store,
                source_walk_forward_id=args.source_walk_forward_id,
                family_id=args.family_id,
                direction_overrides=overrides,
                candidate_policy=args.candidate_policy,
                bind_configuration_to_asset_identity=not args.legacy_unbound_configuration,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
