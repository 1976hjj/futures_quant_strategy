"""Publish M4.4 long-window walk-forward and PIT regime evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import uuid
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from run_m4_1_evidence import _factor_manifest, _publish_labels

from alpha_research_os.evaluation import (
    EvidenceFile,
    FactorVariantReleaseRef,
    LabelReleaseManifest,
    StatisticalInferenceSpec,
    WalkForwardEvaluationSpec,
    WalkForwardEvidenceManifest,
    WalkForwardEvidenceRequest,
    WalkForwardFoldSpec,
    benjamini_hochberg,
    moving_block_bootstrap_mean,
    newey_west_mean_test,
)
from alpha_research_os.factors import FactorReleaseManifest
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash
from alpha_research_os.kernel.specs import DateRange

ENGINE_VERSION = "duckdb-python-walk-forward-1.1.0"
FAMILY_ID = "M4-4-2023-2025-RANKIC-ALL-FACTORS-ALL-VARIANTS-v1"
RAW_RELEASE_ID = "sha256:3e3d4e69428ce879ee9b53ffc6c39bc8b17b8d49780d305ecff8c0e96ee94fe7"
PROCESSED_RELEASE_IDS = (
    "sha256:b18a177256ad433e20cf97d543a5e68d324c6c3c4d9859be8981a41ee8761009",
    "sha256:0e3b87d5c2fbdae6b46569d633e2a370add03eae27c49d4bee8a31266fc6a91a",
)


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


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _configure(connection: duckdb.DuckDBPyConnection, temporary_directory: Path) -> None:
    temporary_directory.mkdir(parents=True, exist_ok=True)
    connection.execute("SET TimeZone='Asia/Shanghai'")
    connection.execute("SET memory_limit='10GB'")
    connection.execute(f"SET temp_directory='{_sql_path(temporary_directory)}'")


def _folds() -> tuple[WalkForwardFoldSpec, ...]:
    return (
        WalkForwardFoldSpec(
            fold_id="WF-2023",
            train=DateRange(start=date(2020, 1, 2), end=date(2021, 12, 24)),
            validation=DateRange(start=date(2022, 1, 11), end=date(2022, 12, 23)),
            test=DateRange(start=date(2023, 1, 10), end=date(2023, 12, 29)),
            exposure_status="RETROSPECTIVE_DIAGNOSTIC",
        ),
        WalkForwardFoldSpec(
            fold_id="WF-2024",
            train=DateRange(start=date(2020, 1, 2), end=date(2022, 12, 23)),
            validation=DateRange(start=date(2023, 1, 10), end=date(2023, 12, 22)),
            test=DateRange(start=date(2024, 1, 9), end=date(2024, 12, 31)),
            exposure_status="RETROSPECTIVE_DIAGNOSTIC",
        ),
        WalkForwardFoldSpec(
            fold_id="WF-2025",
            train=DateRange(start=date(2020, 1, 2), end=date(2023, 12, 22)),
            validation=DateRange(start=date(2024, 1, 9), end=date(2024, 12, 24)),
            test=DateRange(start=date(2025, 1, 9), end=date(2025, 12, 31)),
            exposure_status="FROZEN_RESEARCH_UNSEEN",
        ),
    )


def _load_factor_inputs(
    factor_store: Path, release_ids: tuple[str, ...]
) -> tuple[tuple[FactorVariantReleaseRef, ...], dict[str, tuple[Any, Path]]]:
    loaded: dict[str, tuple[Any, Path]] = {}
    references: list[FactorVariantReleaseRef] = []
    for release_id in release_ids:
        manifest, parquet = _factor_manifest(factor_store, release_id)
        references.append(
            FactorVariantReleaseRef(
                release_id=release_id,
                manifest_hash=content_hash(manifest),
                parquet_hash=manifest.parquet_hash,
                variant=manifest.request.variant,
            )
        )
        loaded[manifest.request.variant] = (manifest, parquet)
    if "RAW" not in loaded:
        raise ValueError("walk-forward evaluation requires exactly one RAW factor release")
    if len(loaded) != len(release_ids):
        raise ValueError("factor releases must have unique variants")
    return tuple(sorted(references, key=lambda item: (item.variant, item.release_id))), loaded


def _request(
    references: tuple[FactorVariantReleaseRef, ...],
    label_manifest: LabelReleaseManifest,
    *,
    family_id: str,
    folds: tuple[WalkForwardFoldSpec, ...],
    window: DateRange,
    engine_version: str,
) -> WalkForwardEvidenceRequest:
    evaluation = WalkForwardEvaluationSpec(
        folds=folds,
        inference=StatisticalInferenceSpec(
            spec_id="m4-4-fold-rank-ic-inference",
            spec_version="1.0.0",
        ),
    )
    return WalkForwardEvidenceRequest(
        engine_version=engine_version,
        multiple_testing_family_id=family_id,
        factor_inputs=references,
        label_release_id=label_manifest.release_id,
        label_manifest_hash=content_hash(label_manifest),
        window=window,
        evaluation=evaluation,
    )


def _reserve_exposure(database: Path, request: WalkForwardEvidenceRequest) -> None:
    now = datetime.now().astimezone()
    connection = duckdb.connect(str(database))
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.holdout_exposure_ledger (
            event_id VARCHAR PRIMARY KEY, walk_forward_id VARCHAR, fold_id VARCHAR,
            test_start DATE, test_end DATE, prior_exposure_status VARCHAR,
            event_type VARCHAR, recorded_at TIMESTAMPTZ)"""
        )
        for fold in request.evaluation.folds:
            payload = {
                "event_type": "RESERVED_BEFORE_STATISTICAL_READ",
                "fold_id": fold.fold_id,
                "walk_forward_id": request.walk_forward_id,
            }
            connection.execute(
                "INSERT OR IGNORE INTO metadata.holdout_exposure_ledger VALUES (?,?,?,?,?,?,?,?)",
                [
                    content_hash(payload),
                    request.walk_forward_id,
                    fold.fold_id,
                    fold.test.start,
                    fold.test.end,
                    fold.exposure_status,
                    payload["event_type"],
                    now,
                ],
            )
        connection.execute("CHECKPOINT")
    finally:
        connection.close()


def _daily_sql(
    factor_path: Path,
    label_path: Path,
    target: Path,
    walk_forward_id: str,
    year: int,
) -> str:
    return f"""
      COPY (WITH joined AS (
        SELECT f.session,f.instrument_id,f.factor_id,f.factor_version,f.variant,
               f.value factor_value,l.value label_value,l.is_valid label_valid
        FROM read_parquet('{_sql_path(factor_path)}') f
        LEFT JOIN read_parquet('{_sql_path(label_path)}') l
          ON l.signal_session=f.session AND l.instrument_id=f.instrument_id
        WHERE year(f.session)={year} AND year(l.signal_session)={year}
      ), paired AS (
        SELECT * FROM joined WHERE factor_value IS NOT NULL AND label_valid AND label_value IS NOT NULL
      ), ranked_base AS (
        SELECT *,rank() OVER (PARTITION BY session,factor_id,factor_version ORDER BY factor_value) factor_rank_min,
          count(*) OVER (PARTITION BY session,factor_id,factor_version,factor_value) factor_ties,
          rank() OVER (PARTITION BY session,factor_id,factor_version ORDER BY label_value) label_rank_min,
          count(*) OVER (PARTITION BY session,factor_id,factor_version,label_value) label_ties
        FROM paired
      ), ranked AS (
        SELECT *,factor_rank_min+(factor_ties-1)/2.0 factor_rank,
                 label_rank_min+(label_ties-1)/2.0 label_rank FROM ranked_base
      ), base AS (
        SELECT session,factor_id,factor_version,min(variant) variant,count(*) universe_count,
          count(factor_value) factor_present_count,count(*) FILTER (WHERE label_valid) valid_label_count,
          count(*) FILTER (WHERE factor_value IS NOT NULL AND label_valid AND label_value IS NOT NULL) paired_count,
          count(factor_value)::DOUBLE/count(*) coverage
        FROM joined GROUP BY 1,2,3
      ), rank_ic AS (
        SELECT session,factor_id,factor_version,corr(factor_rank,label_rank) rank_value
        FROM ranked GROUP BY 1,2,3
      )
      SELECT {_sql_string(walk_forward_id)} walk_forward_id,b.variant,b.session,b.factor_id,b.factor_version,
        b.universe_count,b.factor_present_count,b.valid_label_count,b.paired_count,b.coverage,
        CASE WHEN b.paired_count>=20 AND isfinite(r.rank_value) THEN r.rank_value END rank_ic
      FROM base b LEFT JOIN rank_ic r USING (session,factor_id,factor_version)
      ORDER BY b.variant,b.factor_id,b.factor_version,b.session)
      TO '{_sql_path(target)}' (FORMAT PARQUET,COMPRESSION ZSTD,COMPRESSION_LEVEL 6)
    """


def _publish_daily(
    database: Path,
    evidence_store: Path,
    directory: Path,
    request: WalkForwardEvidenceRequest,
    loaded: dict[str, tuple[Any, Path]],
    label_path: Path,
) -> Path:
    target = directory / "daily_rank_ic.parquet"
    if target.exists():
        return target
    staging = directory / "daily_staging"
    staging.mkdir(parents=True, exist_ok=True)
    partitions = []
    for variant in sorted(loaded):
        _, factor_path = loaded[variant]
        for year in range(request.window.start.year, request.window.end.year + 1):
            partition = staging / f"variant={variant}.year={year}.parquet"
            partitions.append(partition)
            if partition.exists():
                print(f"daily variant={variant} year={year} cache_hit", flush=True)
                continue
            temporary = partition.with_name(f".{partition.stem}.{uuid.uuid4().hex}.tmp.parquet")
            print(f"daily variant={variant} year={year} materializing", flush=True)
            with duckdb.connect(str(database), read_only=True) as connection:
                _configure(connection, evidence_store / "duckdb_tmp")
                connection.execute(_daily_sql(factor_path, label_path, temporary, request.walk_forward_id, year))
            os.replace(temporary, partition)
    parquet_list = ",".join(_sql_string(_sql_path(path)) for path in partitions)
    temporary_target = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.tmp.parquet")
    with duckdb.connect() as connection:
        connection.execute(
            f"""COPY (SELECT * FROM read_parquet([{parquet_list}]) ORDER BY variant,factor_id,factor_version,session)
            TO '{_sql_path(temporary_target)}' (FORMAT PARQUET,COMPRESSION ZSTD,COMPRESSION_LEVEL 6)"""
        )
    os.replace(temporary_target, target)
    for partition in partitions:
        partition.unlink()
    staging.rmdir()
    return target


def _directions(database: Path) -> dict[tuple[str, str], str]:
    with duckdb.connect(str(database), read_only=True) as connection:
        rows = connection.execute("SELECT factor_id,factor_version,spec_json FROM metadata.factor_registry").fetchall()
    return {(factor_id, version): json.loads(spec_json)["direction"] for factor_id, version, spec_json in rows}


def _market_regimes(database: Path, raw_path: Path) -> dict[date, tuple[float, float]]:
    with duckdb.connect(str(database), read_only=True) as connection:
        _configure(connection, Path("data/evidence_store/duckdb_tmp"))
        rows = connection.execute(
            f"""WITH universe_keys AS (
              SELECT DISTINCT session,instrument_id FROM read_parquet('{_sql_path(raw_path)}')
            ), daily AS (
              SELECT f.session,avg(CASE WHEN m.pre_close>0 THEN m.close/m.pre_close-1 END) market_return
              FROM universe_keys f
              LEFT JOIN research.market_daily m ON m.trade_date=f.session AND m.ts_code=f.instrument_id
              GROUP BY 1
            ), features AS (
              SELECT session,
                sum(ln(1+market_return)) OVER (ORDER BY session ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) trend_score,
                stddev_pop(market_return) OVER (ORDER BY session ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) volatility
              FROM daily)
            SELECT session,trend_score,volatility FROM features ORDER BY session"""
        ).fetchall()
    return {session: (trend, volatility) for session, trend, volatility in rows}


def _seed(base: int, *parts: str) -> int:
    return int.from_bytes(hashlib.sha256((str(base) + "|" + "|".join(parts)).encode()).digest()[:4], "big")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _calculate_fold_and_regime(
    database: Path,
    daily_path: Path,
    raw_path: Path,
    request: WalkForwardEvidenceRequest,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    directions = _directions(database)
    regimes = _market_regimes(database, raw_path)
    grouped: dict[tuple[str, str, str], list[tuple[date, float | None]]] = defaultdict(list)
    with duckdb.connect() as connection:
        rows = connection.execute(
            f"""SELECT variant,factor_id,factor_version,session,rank_ic
            FROM read_parquet('{_sql_path(daily_path)}') ORDER BY 1,2,3,4"""
        ).fetchall()
    for variant, factor_id, version, session, rank_ic in rows:
        grouped[(variant, factor_id, version)].append((session, rank_ic))
    hypotheses: list[dict[str, Any]] = []
    regime_rows: list[dict[str, Any]] = []
    inference = request.evaluation.inference
    for fold in request.evaluation.folds:
        train_volatility = [
            value[1] for session, value in regimes.items() if fold.train.start <= session <= fold.train.end
        ]
        volatility_threshold = statistics.median(train_volatility)
        for (variant, factor_id, version), observations in sorted(grouped.items()):
            train = [
                value
                for session, value in observations
                if fold.train.start <= session <= fold.train.end and value is not None
            ]
            validation = [
                value
                for session, value in observations
                if fold.validation.start <= session <= fold.validation.end and value is not None
            ]
            test_pairs = [
                (session, value)
                for session, value in observations
                if fold.test.start <= session <= fold.test.end and value is not None
            ]
            test = [float(value) for _, value in test_pairs]
            declared = directions[(factor_id, version)]
            if declared == "POSITIVE":
                multiplier = 1
                direction_source = "DECLARED_POSITIVE"
            elif declared == "NEGATIVE":
                multiplier = -1
                direction_source = "DECLARED_NEGATIVE"
            else:
                multiplier = 1 if sum(train) / len(train) >= 0 else -1
                direction_source = "TRAIN_MEAN_SIGN"
            directed_test = [multiplier * value for value in test]
            hac = newey_west_mean_test(directed_test, max_lag=inference.hac_max_lag)
            bootstrap = moving_block_bootstrap_mean(
                directed_test,
                block_length=inference.bootstrap_block_length,
                resamples=inference.bootstrap_resamples,
                seed=_seed(inference.random_seed, fold.fold_id, variant, factor_id, version),
                confidence_level=inference.bootstrap_confidence_level,
            )
            hypotheses.append(
                {
                    "walk_forward_id": request.walk_forward_id,
                    "multiple_testing_family_id": request.multiple_testing_family_id,
                    "fold_id": fold.fold_id,
                    "exposure_status_before_run": fold.exposure_status,
                    "variant": variant,
                    "factor_id": factor_id,
                    "factor_version": version,
                    "direction_multiplier": multiplier,
                    "direction_source": direction_source,
                    "train_session_count": len(train),
                    "validation_session_count": len(validation),
                    "test_session_count": len(test),
                    "train_mean_rank_ic": _mean(train),
                    "validation_mean_rank_ic": _mean(validation),
                    "test_mean_rank_ic_raw": _mean(test),
                    "test_mean_rank_ic_directed": hac.mean,
                    "hac_standard_error": hac.standard_error,
                    "hac_z_statistic": hac.z_statistic,
                    "hac_p_value_two_sided": hac.p_value_two_sided,
                    "bootstrap_p_value_two_sided": bootstrap.p_value_two_sided,
                    "bootstrap_confidence_lower": bootstrap.confidence_lower,
                    "bootstrap_confidence_upper": bootstrap.confidence_upper,
                    "bootstrap_seed": bootstrap.seed,
                    "evidence_status": "PSEUDO_OOS_FIRST_READ" if "UNSEEN" in fold.exposure_status else "RETROSPECTIVE",
                }
            )
            buckets = {
                ("TREND", "UP"): [],
                ("TREND", "DOWN"): [],
                ("VOLATILITY", "HIGH"): [],
                ("VOLATILITY", "LOW"): [],
            }
            for session, value in test_pairs:
                trend, volatility = regimes[session]
                buckets[("TREND", "UP" if trend >= 0 else "DOWN")].append(float(value))
                buckets[("VOLATILITY", "HIGH" if volatility >= volatility_threshold else "LOW")].append(float(value))
            for (dimension, regime), values in buckets.items():
                regime_rows.append(
                    {
                        "walk_forward_id": request.walk_forward_id,
                        "fold_id": fold.fold_id,
                        "variant": variant,
                        "factor_id": factor_id,
                        "factor_version": version,
                        "regime_dimension": dimension,
                        "regime": regime,
                        "session_count": len(values),
                        "mean_rank_ic_raw": _mean(values),
                        "mean_rank_ic_directed": _mean([multiplier * value for value in values]),
                        "volatility_train_median_threshold": (
                            volatility_threshold if dimension == "VOLATILITY" else None
                        ),
                        "evidence_status": (
                            "DIAGNOSTIC"
                            if len(values) >= request.evaluation.minimum_regime_sessions
                            else "INSUFFICIENT_SESSIONS"
                        ),
                    }
                )
    hypotheses.sort(key=lambda item: (item["fold_id"], item["variant"], item["factor_id"], item["factor_version"]))
    hac_q = benjamini_hochberg([item["hac_p_value_two_sided"] for item in hypotheses])
    bootstrap_q = benjamini_hochberg([item["bootstrap_p_value_two_sided"] for item in hypotheses])
    for item, hac_value, bootstrap_value in zip(hypotheses, hac_q, bootstrap_q, strict=True):
        item["hac_bh_q_value"] = hac_value
        item["bootstrap_bh_q_value"] = bootstrap_value
        item["hac_fdr_reject"] = hac_value is not None and hac_value <= inference.fdr_alpha
        item["bootstrap_fdr_reject"] = bootstrap_value is not None and bootstrap_value <= inference.fdr_alpha
    regime_rows.sort(
        key=lambda item: (
            item["fold_id"],
            item["variant"],
            item["factor_id"],
            item["factor_version"],
            item["regime_dimension"],
            item["regime"],
        )
    )
    frozen = [item for item in hypotheses if item["evidence_status"] == "PSEUDO_OOS_FIRST_READ"]
    summary = {
        "walk_forward_id": request.walk_forward_id,
        "multiple_testing_family_id": request.multiple_testing_family_id,
        "fold_count": len(request.evaluation.folds),
        "variant_count": len(request.factor_inputs),
        "factor_count": len(grouped) // len(request.factor_inputs),
        "hypothesis_count": len(hypotheses),
        "frozen_first_read_hypothesis_count": len(frozen),
        "hac_fdr_rejection_count": sum(item["hac_fdr_reject"] for item in hypotheses),
        "bootstrap_fdr_rejection_count": sum(item["bootstrap_fdr_reject"] for item in hypotheses),
        "frozen_hac_fdr_rejection_count": sum(item["hac_fdr_reject"] for item in frozen),
        "frozen_bootstrap_fdr_rejection_count": sum(item["bootstrap_fdr_reject"] for item in frozen),
        "minimum_hac_bh_q_value": min(item["hac_bh_q_value"] for item in hypotheses),
        "minimum_bootstrap_bh_q_value": min(item["bootstrap_bh_q_value"] for item in hypotheses),
        "decision_status": "NO_PROMOTION_DIAGNOSTIC_AND_PSEUDO_OOS",
    }
    return hypotheses, regime_rows, summary


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd", compression_level=6)


def _register(
    database: Path,
    evidence_store: Path,
    label_manifest: LabelReleaseManifest,
    manifest: WalkForwardEvidenceManifest,
) -> None:
    connection = duckdb.connect(str(database))
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.label_release_manifest (
            release_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, parquet_path VARCHAR, parquet_hash VARCHAR,
            label_id VARCHAR, label_version VARCHAR, start_date DATE, end_date DATE, constraint_level VARCHAR,
            row_count BIGINT, valid_count BIGINT, invalid_count BIGINT, request_json JSON, created_at TIMESTAMPTZ)"""
        )
        connection.execute(
            """INSERT OR IGNORE INTO metadata.label_release_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                label_manifest.release_id,
                content_hash(label_manifest),
                label_manifest.parquet_relative_path,
                label_manifest.parquet_hash,
                label_manifest.request.label_id,
                label_manifest.request.label_version,
                label_manifest.request.start,
                label_manifest.request.end,
                label_manifest.request.constraint_level.value,
                label_manifest.row_count,
                label_manifest.valid_count,
                label_manifest.invalid_count,
                json.dumps(label_manifest.request.model_dump(mode="json"), separators=(",", ":")),
                label_manifest.created_at,
            ],
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.walk_forward_evidence_manifest (
            walk_forward_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, multiple_testing_family_id VARCHAR,
            label_release_id VARCHAR, daily_row_count BIGINT, fold_hypothesis_count BIGINT,
            regime_row_count BIGINT, quality_status VARCHAR, decision_status VARCHAR,
            request_json JSON, limitations_json JSON, created_at TIMESTAMPTZ)"""
        )
        manifest_hash = content_hash(manifest)
        existing = connection.execute(
            "SELECT manifest_hash FROM metadata.walk_forward_evidence_manifest WHERE walk_forward_id=?",
            [manifest.walk_forward_id],
        ).fetchone()
        if existing and existing != (manifest_hash,):
            raise ValueError("immutable walk-forward registry conflict")
        connection.execute(
            """INSERT OR IGNORE INTO metadata.walk_forward_evidence_manifest VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                manifest.walk_forward_id,
                manifest_hash,
                manifest.request.multiple_testing_family_id,
                manifest.request.label_release_id,
                manifest.daily_row_count,
                manifest.fold_hypothesis_count,
                manifest.regime_row_count,
                manifest.quality_status,
                manifest.decision_status,
                json.dumps(manifest.request.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                json.dumps(manifest.limitations, ensure_ascii=False, separators=(",", ":")),
                manifest.created_at,
            ],
        )
        root = _sql_path(evidence_store / "walk_forward" / "*")
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_walk_forward_daily AS
            SELECT * FROM read_parquet('{root}/daily_rank_ic.parquet',union_by_name=true)"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW research.factor_walk_forward_summary AS
            SELECT * FROM read_parquet('{root}/fold_statistics.parquet',union_by_name=true)"""
        )
        connection.execute(
            """CREATE OR REPLACE VIEW research.factor_walk_forward_decisions AS
            SELECT *,
                CASE
                    WHEN NOT hac_fdr_reject THEN 'NOT_REJECTED'
                    WHEN test_mean_rank_ic_directed > 0 THEN 'DIRECTION_SUPPORTED'
                    ELSE 'DIRECTION_CONTRADICTED'
                END AS hac_direction_outcome,
                CASE
                    WHEN NOT bootstrap_fdr_reject THEN 'NOT_REJECTED'
                    WHEN test_mean_rank_ic_directed > 0 THEN 'DIRECTION_SUPPORTED'
                    ELSE 'DIRECTION_CONTRADICTED'
                END AS bootstrap_direction_outcome
            FROM research.factor_walk_forward_summary"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_regime_statistics AS
            SELECT * FROM read_parquet('{root}/regime_statistics.parquet',union_by_name=true)"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW research.walk_forward_family_summary AS
            SELECT * FROM read_parquet('{root}/family_summary.parquet',union_by_name=true)"""
        )
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def publish(
    database: Path,
    factor_store: Path,
    evidence_store: Path,
    *,
    raw_release_id: str = RAW_RELEASE_ID,
    processed_release_ids: tuple[str, ...] = PROCESSED_RELEASE_IDS,
    family_id: str = FAMILY_ID,
    folds: tuple[WalkForwardFoldSpec, ...] | None = None,
    window: DateRange | None = None,
    engine_version: str = ENGINE_VERSION,
) -> dict[str, Any]:
    selected_folds = folds or _folds()
    selected_window = window or DateRange(start=date(2020, 1, 2), end=date(2025, 12, 31))
    references, loaded = _load_factor_inputs(factor_store, (raw_release_id, *processed_release_ids))
    raw_manifest, raw_path = loaded["RAW"]
    if not isinstance(raw_manifest, FactorReleaseManifest):
        raise ValueError("RAW input must be a FactorReleaseManifest")
    label_manifest, label_path, label_cache = _publish_labels(database, evidence_store, raw_manifest, raw_path)
    request = _request(
        references,
        label_manifest,
        family_id=family_id,
        folds=selected_folds,
        window=selected_window,
        engine_version=engine_version,
    )
    _reserve_exposure(database, request)
    directory = evidence_store / "walk_forward" / request.walk_forward_id.removeprefix("sha256:")
    manifest_path = directory / "manifest.json"
    targets = {
        "daily_rank_ic": directory / "daily_rank_ic.parquet",
        "family_summary": directory / "family_summary.parquet",
        "fold_statistics": directory / "fold_statistics.parquet",
        "regime_statistics": directory / "regime_statistics.parquet",
    }
    if manifest_path.exists() and all(path.exists() for path in targets.values()):
        manifest = WalkForwardEvidenceManifest.model_validate_json(manifest_path.read_bytes())
        hashes = {item.name: item.artifact_hash for item in manifest.files}
        if manifest.request != request or any(_sha256_file(path) != hashes[name] for name, path in targets.items()):
            raise ValueError("cached walk-forward release failed immutable verification")
        _register(database, evidence_store, label_manifest, manifest)
        return {
            "cache_hit": True,
            "walk_forward_id": manifest.walk_forward_id,
            "manifest": str(manifest_path.resolve()),
        }
    directory.mkdir(parents=True, exist_ok=True)
    daily_path = _publish_daily(database, evidence_store, directory, request, loaded, label_path)
    hypotheses, regimes, summary = _calculate_fold_and_regime(database, daily_path, raw_path, request)
    _write_parquet(targets["fold_statistics"], hypotheses)
    _write_parquet(targets["regime_statistics"], regimes)
    _write_parquet(targets["family_summary"], [summary])
    with duckdb.connect() as connection:
        daily_count = connection.execute(f"SELECT count(*) FROM read_parquet('{_sql_path(daily_path)}')").fetchone()[0]
        duplicate_count = connection.execute(
            f"""SELECT count(*) FROM (SELECT variant,session,factor_id,factor_version,count(*) n
            FROM read_parquet('{_sql_path(daily_path)}') GROUP BY 1,2,3,4 HAVING n>1)"""
        ).fetchone()[0]
    entity_count = len({(item["variant"], item["factor_id"], item["factor_version"]) for item in hypotheses})
    expected_hypotheses = len(request.evaluation.folds) * entity_count
    expected_regimes = expected_hypotheses * 4
    if duplicate_count or len(hypotheses) != expected_hypotheses or len(regimes) != expected_regimes:
        raise ValueError(
            f"walk-forward quality gate failed duplicates={duplicate_count} "
            f"hypotheses={len(hypotheses)} regimes={len(regimes)}"
        )
    files = []
    for name, path in sorted(targets.items()):
        with duckdb.connect() as connection:
            row_count = connection.execute(f"SELECT count(*) FROM read_parquet('{_sql_path(path)}')").fetchone()[0]
        files.append(
            EvidenceFile(
                name=name,
                relative_path=path.relative_to(evidence_store).as_posix(),
                artifact_hash=_sha256_file(path),
                row_count=row_count,
            )
        )
    manifest = WalkForwardEvidenceManifest(
        walk_forward_id=request.walk_forward_id,
        request=request,
        created_at=datetime.now().astimezone(),
        files=tuple(files),
        daily_row_count=daily_count,
        fold_hypothesis_count=len(hypotheses),
        regime_row_count=len(regimes),
        quality_status="PASS",
        decision_status="NO_PROMOTION_DIAGNOSTIC_AND_PSEUDO_OOS",
        limitations=(
            "Fold exposure status is recorded per configured fold; retrospective folds are not unseen tests.",
            "A frozen historical first read is pseudo-OOS, not live prospective OOS.",
            "The provisional label is not price-limit, delisting-return, or transaction-cost aware.",
            "Market regimes are PIT trailing diagnostics and do not prove structural stability.",
            (
                f"All {expected_hypotheses} configured fold-factor-variant tests share one BH-FDR family; "
                "no failed test may be removed."
            ),
        ),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    _register(database, evidence_store, label_manifest, manifest)
    return {
        "cache_hit": False,
        "walk_forward_id": manifest.walk_forward_id,
        "label_cache_hit": label_cache,
        "label_release_id": label_manifest.release_id,
        "daily_row_count": daily_count,
        "fold_hypothesis_count": len(hypotheses),
        "regime_row_count": len(regimes),
        **{key: value for key, value in summary.items() if key.endswith("rejection_count")},
        "minimum_hac_bh_q_value": summary["minimum_hac_bh_q_value"],
        "minimum_bootstrap_bh_q_value": summary["minimum_bootstrap_bh_q_value"],
        "decision_status": manifest.decision_status,
        "manifest": str(manifest_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--factor-store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--raw-release-id", default=RAW_RELEASE_ID)
    parser.add_argument("--processed-release-id", action="append", dest="processed_release_ids")
    parser.add_argument("--family-id", default=FAMILY_ID)
    args = parser.parse_args()
    processed_release_ids = tuple(args.processed_release_ids) if args.processed_release_ids else PROCESSED_RELEASE_IDS
    print(
        json.dumps(
            publish(
                args.database,
                args.factor_store,
                args.evidence_store,
                raw_release_id=args.raw_release_id,
                processed_release_ids=processed_release_ids,
                family_id=args.family_id,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
