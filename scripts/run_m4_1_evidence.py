"""Publish provisional 5D labels and a basic immutable factor evidence bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from alpha_research_os.evaluation import (
    BasicEvidenceRequest,
    EvidenceBundleManifest,
    EvidenceFile,
    ExecutionConstraintLevel,
    LabelAssetRequest,
    LabelReleaseManifest,
    default_forward_5d_label_spec,
)
from alpha_research_os.factors.assets import FactorReleaseManifest
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash

LABEL_ENGINE_VERSION = "duckdb-forward-return-label-1.0.0"
EVALUATOR_VERSION = "duckdb-basic-cross-sectional-evidence-1.0.0"
DEFAULT_FACTOR_RELEASE_ID = "sha256:2faab50ecfd39544d40c7c451b9f9325cdab57f075d12b6a52a50593fe18778e"


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


def _factor_manifest(factor_store: Path, release_id: str) -> tuple[FactorReleaseManifest, Path]:
    release_dir = factor_store / "releases" / release_id.removeprefix("sha256:")
    manifest = FactorReleaseManifest.model_validate_json((release_dir / "manifest.json").read_bytes())
    parquet = release_dir / manifest.parquet_relative_path.split("/", 2)[-1]
    if manifest.release_id != release_id or _sha256_file(parquet) != manifest.parquet_hash:
        raise ValueError("factor release failed immutable input verification")
    return manifest, parquet


def _label_sql(
    factor_parquet: Path,
    target: Path,
    label_release_id: str,
    label_id: str,
    label_version: str,
    start: date,
    end: date,
) -> str:
    return f"""
    COPY (
      WITH calendar AS (
        SELECT cal_date, row_number() OVER (ORDER BY cal_date) AS session_number
        FROM research.trading_calendar WHERE exchange='SSE' AND is_open
      ), signals AS (
        SELECT DISTINCT session AS signal_session, instrument_id
        FROM read_parquet('{_sql_path(factor_parquet)}')
      ), market_scope AS (
        SELECT * FROM research.market_daily
        WHERE trade_date BETWEEN DATE {_sql_string(start.isoformat())}
                             AND DATE {_sql_string(end.isoformat())} + INTERVAL 30 DAYS
      ), adjustment_scope AS (
        SELECT * FROM research.adj_factor
        WHERE trade_date BETWEEN DATE {_sql_string(start.isoformat())}
                             AND DATE {_sql_string(end.isoformat())} + INTERVAL 30 DAYS
      ), state_scope AS (
        SELECT * FROM research.security_session_state
        WHERE trade_date BETWEEN DATE {_sql_string(start.isoformat())}
                             AND DATE {_sql_string(end.isoformat())} + INTERVAL 30 DAYS
      ), targets AS (
        SELECT s.*, entry.cal_date AS entry_session, exit.cal_date AS exit_session
        FROM signals s
        LEFT JOIN calendar signal ON signal.cal_date=s.signal_session
        LEFT JOIN calendar entry ON entry.session_number=signal.session_number+1
        LEFT JOIN calendar exit ON exit.session_number=signal.session_number+6
      ), inputs AS (
        SELECT
          t.*,
          signal_state.eligible_for_signal AS signal_eligible,
          entry_market.open AS entry_open,
          entry_adj.adj_factor AS entry_adj_factor,
          entry_state.ts_code IS NOT NULL AS has_entry_state,
          entry_state.is_suspended AS entry_suspended,
          entry_state.is_tradeable_bar AS entry_tradeable,
          exit_market.close AS exit_close,
          exit_adj.adj_factor AS exit_adj_factor,
          exit_state.ts_code IS NOT NULL AS has_exit_state,
          exit_state.is_suspended AS exit_suspended,
          exit_state.is_tradeable_bar AS exit_tradeable
        FROM targets t
        LEFT JOIN state_scope signal_state
          ON signal_state.trade_date=t.signal_session AND signal_state.ts_code=t.instrument_id
        LEFT JOIN market_scope entry_market
          ON entry_market.trade_date=t.entry_session AND entry_market.ts_code=t.instrument_id
        LEFT JOIN adjustment_scope entry_adj
          ON entry_adj.trade_date=t.entry_session AND entry_adj.ts_code=t.instrument_id
        LEFT JOIN state_scope entry_state
          ON entry_state.trade_date=t.entry_session AND entry_state.ts_code=t.instrument_id
        LEFT JOIN market_scope exit_market
          ON exit_market.trade_date=t.exit_session AND exit_market.ts_code=t.instrument_id
        LEFT JOIN adjustment_scope exit_adj
          ON exit_adj.trade_date=t.exit_session AND exit_adj.ts_code=t.instrument_id
        LEFT JOIN state_scope exit_state
          ON exit_state.trade_date=t.exit_session AND exit_state.ts_code=t.instrument_id
      ), classified AS (
        SELECT *, CASE
          WHEN coalesce(signal_eligible, false)=false THEN 'SIGNAL_NOT_ELIGIBLE'
          WHEN entry_session IS NULL OR exit_session IS NULL THEN 'INSUFFICIENT_FUTURE_SESSIONS'
          WHEN entry_open IS NULL OR NOT has_entry_state THEN 'ENTRY_OBSERVATION_MISSING'
          WHEN exit_close IS NULL OR NOT has_exit_state THEN 'EXIT_OBSERVATION_MISSING'
          WHEN coalesce(entry_suspended, true) OR coalesce(entry_tradeable, false)=false THEN 'ENTRY_UNTRADABLE'
          WHEN coalesce(exit_suspended, true) OR coalesce(exit_tradeable, false)=false THEN 'EXIT_UNTRADABLE'
          WHEN entry_adj_factor IS NULL OR exit_adj_factor IS NULL
            OR entry_adj_factor<=0 OR exit_adj_factor<=0 THEN 'ADJUSTMENT_MISSING'
          WHEN entry_open<=0 OR exit_close<=0 THEN 'PRICE_INVALID'
        END AS invalid_reason
        FROM inputs
      )
      SELECT
        {_sql_string(label_release_id)} AS label_release_id,
        signal_session,
        instrument_id,
        {_sql_string(label_id)} AS label_id,
        {_sql_string(label_version)} AS label_version,
        CASE WHEN invalid_reason IS NULL
          THEN (exit_close*exit_adj_factor)/(entry_open*entry_adj_factor)-1 END::DOUBLE AS value,
        entry_session,
        exit_session,
        CASE WHEN invalid_reason IS NULL THEN entry_open*entry_adj_factor END::DOUBLE AS entry_adjusted_price,
        CASE WHEN invalid_reason IS NULL THEN exit_close*exit_adj_factor END::DOUBLE AS exit_adjusted_price,
        CASE WHEN invalid_reason IS NULL
          THEN exit_session::TIMESTAMP AT TIME ZONE 'Asia/Shanghai' + INTERVAL 15 HOURS END AS available_at,
        invalid_reason IS NULL AS is_valid,
        invalid_reason,
        'BAR_AND_SUSPENSION_ONLY' AS constraint_level
      FROM classified ORDER BY signal_session, instrument_id
    ) TO '{_sql_path(target)}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6)
    """


def _publish_labels(
    database: Path,
    evidence_store: Path,
    factor_manifest: FactorReleaseManifest,
    factor_parquet: Path,
) -> tuple[LabelReleaseManifest, Path, bool]:
    label_spec = default_forward_5d_label_spec()
    request = LabelAssetRequest(
        engine_version=LABEL_ENGINE_VERSION,
        label_id=label_spec.label_id,
        label_version=label_spec.label_version,
        label_spec_hash=content_hash(label_spec),
        source_factor_release_id=factor_manifest.release_id,
        dataset_lineage=factor_manifest.request.dataset_lineage,
        universe_id=factor_manifest.request.universe_id,
        universe_version=factor_manifest.request.universe_version,
        start=factor_manifest.request.start,
        end=factor_manifest.request.end,
        constraint_level=ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY,
    )
    release_dir = evidence_store / "labels" / request.computation_key.removeprefix("sha256:")
    parquet = release_dir / "forward_return_labels.parquet"
    manifest_path = release_dir / "manifest.json"
    if parquet.exists() and manifest_path.exists():
        manifest = LabelReleaseManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.request != request or _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError("cached label release failed immutable verification")
        return manifest, parquet, True
    release_dir.mkdir(parents=True, exist_ok=True)
    temporary = release_dir / f".labels.{uuid.uuid4().hex}.tmp.parquet"
    with duckdb.connect(str(database), read_only=True) as connection:
        connection.execute("SET TimeZone='Asia/Shanghai'")
        connection.execute(
            _label_sql(
                factor_parquet,
                temporary,
                request.computation_key,
                label_spec.label_id,
                label_spec.label_version,
                request.start,
                request.end,
            )
        )
    with duckdb.connect() as connection:
        source = f"read_parquet('{_sql_path(temporary)}')"
        row_count, valid_count, invalid_count, duplicates, nonfinite = connection.execute(
            f"""SELECT count(*), count(*) FILTER (WHERE is_valid), count(*) FILTER (WHERE NOT is_valid),
            count(*)-count(DISTINCT (signal_session, instrument_id)),
            count(*) FILTER (WHERE value IS NOT NULL AND NOT isfinite(value)) FROM {source}"""
        ).fetchone()
    if duplicates or nonfinite or row_count != factor_manifest.row_count // factor_manifest.factor_count:
        raise ValueError(f"label quality gate failed rows={row_count} duplicates={duplicates} nonfinite={nonfinite}")
    os.replace(temporary, parquet)
    manifest = LabelReleaseManifest(
        release_id=request.computation_key,
        request=request,
        created_at=datetime.now().astimezone(),
        parquet_relative_path=parquet.relative_to(evidence_store).as_posix(),
        parquet_hash=_sha256_file(parquet),
        row_count=row_count,
        valid_count=valid_count,
        invalid_count=invalid_count,
        quality_status="PASS",
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    _atomic_write(release_dir / "label_spec.json", canonical_json_bytes(label_spec))
    return manifest, parquet, False


def _paired_ctes(factor_path: str, label_path: str) -> str:
    return f"""
      joined AS (
        SELECT f.session, f.instrument_id, f.factor_id, f.factor_version, f.value AS factor_value,
               l.value AS label_value, l.is_valid AS label_valid
        FROM read_parquet('{factor_path}') f
        LEFT JOIN read_parquet('{label_path}') l
          ON l.signal_session=f.session AND l.instrument_id=f.instrument_id
      ), paired_base AS (
        SELECT * FROM joined WHERE factor_value IS NOT NULL AND label_valid AND label_value IS NOT NULL
      ), ranked_base AS (
        SELECT *,
          rank() OVER (PARTITION BY session, factor_id, factor_version ORDER BY factor_value) AS factor_rank_min,
          count(*) OVER (PARTITION BY session, factor_id, factor_version, factor_value) AS factor_ties,
          rank() OVER (PARTITION BY session, factor_id, factor_version ORDER BY label_value) AS label_rank_min,
          count(*) OVER (PARTITION BY session, factor_id, factor_version, label_value) AS label_ties,
          count(*) OVER (PARTITION BY session, factor_id, factor_version) AS pair_count
        FROM paired_base
      ), ranked AS (
        SELECT *, factor_rank_min+(factor_ties-1)/2.0 AS factor_rank,
                  label_rank_min+(label_ties-1)/2.0 AS label_rank
        FROM ranked_base
      )
    """


def _daily_sql(factor: Path, labels: Path, target: Path, evidence_id: str, minimum_pairs: int) -> str:
    ctes = _paired_ctes(_sql_path(factor), _sql_path(labels))
    return f"""
    COPY (WITH {ctes}, base AS (
      SELECT session, factor_id, factor_version, count(*) AS universe_count,
        count(factor_value) AS factor_present_count,
        count(*) FILTER (WHERE label_valid) AS valid_label_count,
        count(*) FILTER (WHERE factor_value IS NOT NULL AND label_valid AND label_value IS NOT NULL) AS paired_count,
        count(factor_value)::DOUBLE/count(*) AS coverage,
        corr(factor_value, label_value) FILTER
          (WHERE factor_value IS NOT NULL AND label_valid AND label_value IS NOT NULL) AS pearson_raw
      FROM joined GROUP BY 1,2,3
    ), rank_ic AS (
      SELECT session, factor_id, factor_version, count(*) AS ranked_count,
             corr(factor_rank, label_rank) AS rank_raw
      FROM ranked GROUP BY 1,2,3
    )
    SELECT {_sql_string(evidence_id)} AS evidence_id, b.* EXCLUDE (pearson_raw),
      CASE WHEN b.paired_count>={minimum_pairs} AND isfinite(b.pearson_raw) THEN b.pearson_raw END AS pearson_ic,
      CASE WHEN r.ranked_count>={minimum_pairs} AND isfinite(r.rank_raw) THEN r.rank_raw END AS rank_ic
    FROM base b LEFT JOIN rank_ic r USING (session, factor_id, factor_version)
    ORDER BY session, factor_id, factor_version)
    TO '{_sql_path(target)}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """


def _quantile_sql(
    factor: Path,
    labels: Path,
    target: Path,
    evidence_id: str,
    quantiles: int,
    minimum_pairs: int,
) -> str:
    ctes = _paired_ctes(_sql_path(factor), _sql_path(labels))
    return f"""
    COPY (WITH {ctes}, assigned AS (
      SELECT *, least({quantiles}, floor((factor_rank-1)*{quantiles}/pair_count)+1)::INTEGER AS quantile
      FROM ranked WHERE pair_count>={minimum_pairs}
    )
    SELECT {_sql_string(evidence_id)} AS evidence_id, session, factor_id, factor_version, quantile,
           count(*) AS observation_count, avg(label_value) AS mean_return
    FROM assigned GROUP BY 1,2,3,4,5 ORDER BY session, factor_id, factor_version, quantile)
    TO '{_sql_path(target)}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """


def _summary_sql(
    factor: Path,
    labels: Path,
    daily: Path,
    quantile: Path,
    target: Path,
    evidence_id: str,
    quantiles: int,
    minimum_pairs: int,
) -> str:
    ctes = _paired_ctes(_sql_path(factor), _sql_path(labels))
    return f"""
    COPY (WITH {ctes}, assigned AS (
      SELECT session, instrument_id, factor_id, factor_version,
             least({quantiles}, floor((factor_rank-1)*{quantiles}/pair_count)+1)::INTEGER AS quantile
      FROM ranked WHERE pair_count>={minimum_pairs}
    ), session_links AS (
      SELECT factor_id, factor_version, session,
             lag(session) OVER (PARTITION BY factor_id, factor_version ORDER BY session) AS previous_session
      FROM (SELECT DISTINCT factor_id, factor_version, session FROM assigned)
    ), assignment_counts AS (
      SELECT factor_id, factor_version, session, quantile, count(*) AS member_count
      FROM assigned GROUP BY 1,2,3,4
    ), turnover AS (
      SELECT current.factor_id, current.factor_version, current.session, current.quantile,
        1-count(previous.instrument_id)*least(1.0/current_count.member_count,1.0/previous_count.member_count) AS value
      FROM assigned current
      JOIN session_links link USING (factor_id, factor_version, session)
      JOIN assignment_counts current_count USING (factor_id, factor_version, session, quantile)
      JOIN assignment_counts previous_count
        ON previous_count.factor_id=current.factor_id
       AND previous_count.factor_version=current.factor_version
       AND previous_count.session=link.previous_session
       AND previous_count.quantile=current.quantile
      LEFT JOIN assigned previous
        ON previous.factor_id=current.factor_id
       AND previous.factor_version=current.factor_version
       AND previous.session=link.previous_session
       AND previous.quantile=current.quantile
       AND previous.instrument_id=current.instrument_id
      WHERE link.previous_session IS NOT NULL
      GROUP BY 1,2,3,4,current_count.member_count,previous_count.member_count
    ), daily_summary AS (
      SELECT factor_id, factor_version, count(*) AS sessions, sum(paired_count) AS paired_observations,
             avg(coverage) AS mean_coverage, count(pearson_ic) AS pearson_ic_sessions,
             avg(pearson_ic) AS mean_pearson_ic, count(rank_ic) AS rank_ic_sessions, avg(rank_ic) AS mean_rank_ic
      FROM read_parquet('{_sql_path(daily)}') GROUP BY 1,2
    ), quantile_summary AS (
      SELECT factor_id, factor_version,
        avg(mean_return) FILTER (WHERE quantile={quantiles})-
          avg(mean_return) FILTER (WHERE quantile=1) AS raw_q_high_minus_low
      FROM read_parquet('{_sql_path(quantile)}') GROUP BY 1,2
    ), turnover_summary AS (
      SELECT factor_id, factor_version,
        avg(value) FILTER (WHERE quantile={quantiles}) AS top_quantile_turnover,
        avg(value) FILTER (WHERE quantile=1) AS bottom_quantile_turnover
      FROM turnover GROUP BY 1,2
    )
    SELECT {_sql_string(evidence_id)} AS evidence_id, d.*, q.raw_q_high_minus_low,
           t.top_quantile_turnover, t.bottom_quantile_turnover,
           'DESCRIPTIVE_ONLY_NOT_OOS' AS evidence_status
    FROM daily_summary d
    LEFT JOIN quantile_summary q USING (factor_id, factor_version)
    LEFT JOIN turnover_summary t USING (factor_id, factor_version)
    ORDER BY factor_id, factor_version)
    TO '{_sql_path(target)}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """


def _publish_evidence(
    database: Path,
    evidence_store: Path,
    factor_manifest: FactorReleaseManifest,
    factor_parquet: Path,
    label_manifest: LabelReleaseManifest,
    label_parquet: Path,
) -> tuple[EvidenceBundleManifest, bool]:
    request = BasicEvidenceRequest(
        evaluator_version=EVALUATOR_VERSION,
        factor_release_id=factor_manifest.release_id,
        label_release_id=label_manifest.release_id,
        quantile_count=5,
        minimum_pairs_per_session=20,
    )
    bundle_dir = evidence_store / "bundles" / request.evidence_id.removeprefix("sha256:")
    manifest_path = bundle_dir / "manifest.json"
    targets = {
        "daily_metrics": bundle_dir / "daily_metrics.parquet",
        "factor_summary": bundle_dir / "factor_summary.parquet",
        "quantile_returns": bundle_dir / "quantile_returns.parquet",
    }
    if manifest_path.exists() and all(path.exists() for path in targets.values()):
        manifest = EvidenceBundleManifest.model_validate_json(manifest_path.read_bytes())
        hashes = {item.name: item.artifact_hash for item in manifest.files}
        if manifest.request != request or any(_sha256_file(path) != hashes[name] for name, path in targets.items()):
            raise ValueError("cached evidence bundle failed immutable verification")
        return manifest, True
    bundle_dir.mkdir(parents=True, exist_ok=True)
    temporary = {name: path.with_name(f".{name}.{uuid.uuid4().hex}.tmp.parquet") for name, path in targets.items()}
    with duckdb.connect(str(database), read_only=True) as connection:
        connection.execute(
            _daily_sql(factor_parquet, label_parquet, temporary["daily_metrics"], request.evidence_id, 20)
        )
        connection.execute(
            _quantile_sql(
                factor_parquet,
                label_parquet,
                temporary["quantile_returns"],
                request.evidence_id,
                5,
                20,
            )
        )
        connection.execute(
            _summary_sql(
                factor_parquet,
                label_parquet,
                temporary["daily_metrics"],
                temporary["quantile_returns"],
                temporary["factor_summary"],
                request.evidence_id,
                5,
                20,
            )
        )
    files = []
    for name, path in sorted(targets.items()):
        os.replace(temporary[name], path)
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
    with duckdb.connect() as connection:
        factor_count, nonfinite = connection.execute(
            f"""SELECT count(*), count(*) FILTER (WHERE
            (mean_coverage IS NOT NULL AND NOT isfinite(mean_coverage)) OR
            (mean_pearson_ic IS NOT NULL AND NOT isfinite(mean_pearson_ic)) OR
            (mean_rank_ic IS NOT NULL AND NOT isfinite(mean_rank_ic)) OR
            (raw_q_high_minus_low IS NOT NULL AND NOT isfinite(raw_q_high_minus_low)))
            FROM read_parquet('{_sql_path(targets["factor_summary"])}')"""
        ).fetchone()
    if factor_count != factor_manifest.factor_count or nonfinite:
        raise ValueError(f"evidence quality gate failed factors={factor_count} nonfinite={nonfinite}")
    manifest = EvidenceBundleManifest(
        evidence_id=request.evidence_id,
        request=request,
        created_at=datetime.now().astimezone(),
        files=tuple(files),
        factor_count=factor_count,
        quality_status="PASS",
        limitations=(
            "Label execution checks bars and suspensions but is not price-limit aware "
            "until M2-E stk_limit is published.",
            "Statistics are descriptive in one short in-sample window; no HAC, "
            "multiple-testing correction, or OOS claim.",
            "Returns exclude an explicit transaction-cost and delisting-return model.",
        ),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    return manifest, False


def _register(
    database: Path,
    evidence_store: Path,
    label_manifest: LabelReleaseManifest,
    evidence_manifest: EvidenceBundleManifest,
) -> None:
    connection = duckdb.connect(str(database))
    try:
        connection.execute("BEGIN TRANSACTION")
        connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
        connection.execute("CREATE SCHEMA IF NOT EXISTS research")
        connection.execute("CREATE SCHEMA IF NOT EXISTS metadata")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS metadata.label_release_manifest (
            release_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, parquet_path VARCHAR, parquet_hash VARCHAR,
            label_id VARCHAR, label_version VARCHAR, start_date DATE, end_date DATE, constraint_level VARCHAR,
            row_count BIGINT, valid_count BIGINT, invalid_count BIGINT, request_json JSON, created_at TIMESTAMPTZ)"""
        )
        existing_label = connection.execute(
            "SELECT manifest_hash, parquet_hash FROM metadata.label_release_manifest WHERE release_id=?",
            [label_manifest.release_id],
        ).fetchone()
        expected_label = (content_hash(label_manifest), label_manifest.parquet_hash)
        if existing_label and existing_label != expected_label:
            raise ValueError("immutable label release registry conflict")
        connection.execute(
            """INSERT OR IGNORE INTO metadata.label_release_manifest VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                label_manifest.release_id,
                expected_label[0],
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
            """CREATE TABLE IF NOT EXISTS metadata.evidence_bundle_manifest (
            evidence_id VARCHAR PRIMARY KEY, manifest_hash VARCHAR, factor_release_id VARCHAR,
            label_release_id VARCHAR, evaluator_version VARCHAR, factor_count BIGINT,
            quality_status VARCHAR, request_json JSON, limitations_json JSON, created_at TIMESTAMPTZ)"""
        )
        evidence_hash = content_hash(evidence_manifest)
        existing_evidence = connection.execute(
            "SELECT manifest_hash FROM metadata.evidence_bundle_manifest WHERE evidence_id=?",
            [evidence_manifest.evidence_id],
        ).fetchone()
        if existing_evidence and existing_evidence[0] != evidence_hash:
            raise ValueError("immutable evidence bundle registry conflict")
        connection.execute(
            """INSERT OR IGNORE INTO metadata.evidence_bundle_manifest VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                evidence_manifest.evidence_id,
                evidence_hash,
                evidence_manifest.request.factor_release_id,
                evidence_manifest.request.label_release_id,
                evidence_manifest.request.evaluator_version,
                evidence_manifest.factor_count,
                evidence_manifest.quality_status,
                json.dumps(evidence_manifest.request.model_dump(mode="json"), separators=(",", ":")),
                json.dumps(evidence_manifest.limitations, ensure_ascii=False),
                evidence_manifest.created_at,
            ],
        )
        label_glob = _sql_path(evidence_store / "labels" / "*" / "forward_return_labels.parquet")
        bundle_glob = _sql_path(evidence_store / "bundles" / "*")
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.forward_return_labels AS
            SELECT * FROM read_parquet('{label_glob}', union_by_name=true)"""
        )
        connection.execute(
            "CREATE OR REPLACE VIEW research.forward_return_labels AS SELECT * FROM raw.forward_return_labels"
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_daily_evidence AS
            SELECT * FROM read_parquet('{bundle_glob}/daily_metrics.parquet', union_by_name=true)"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW raw.factor_quantile_returns AS
            SELECT * FROM read_parquet('{bundle_glob}/quantile_returns.parquet', union_by_name=true)"""
        )
        connection.execute(
            f"""CREATE OR REPLACE VIEW research.factor_evidence_summary AS
            SELECT * FROM read_parquet('{bundle_glob}/factor_summary.parquet', union_by_name=true)"""
        )
        connection.execute("COMMIT")
        connection.execute("CHECKPOINT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()


def run(database: Path, factor_store: Path, evidence_store: Path, factor_release_id: str) -> dict[str, Any]:
    factor_manifest, factor_parquet = _factor_manifest(factor_store, factor_release_id)
    label_manifest, label_parquet, label_cache_hit = _publish_labels(
        database, evidence_store, factor_manifest, factor_parquet
    )
    evidence_manifest, evidence_cache_hit = _publish_evidence(
        database,
        evidence_store,
        factor_manifest,
        factor_parquet,
        label_manifest,
        label_parquet,
    )
    _register(database, evidence_store, label_manifest, evidence_manifest)
    return {
        "evidence_cache_hit": evidence_cache_hit,
        "evidence_id": evidence_manifest.evidence_id,
        "evidence_manifest": str(
            (
                evidence_store / "bundles" / evidence_manifest.evidence_id.removeprefix("sha256:") / "manifest.json"
            ).resolve()
        ),
        "factor_count": evidence_manifest.factor_count,
        "label_cache_hit": label_cache_hit,
        "label_invalid_count": label_manifest.invalid_count,
        "label_release_id": label_manifest.release_id,
        "label_valid_count": label_manifest.valid_count,
        "label_row_count": label_manifest.row_count,
        "limitations": evidence_manifest.limitations,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--factor-store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--factor-release-id", default=DEFAULT_FACTOR_RELEASE_ID)
    args = parser.parse_args()
    print(json.dumps(run(args.database, args.factor_store, args.evidence_store, args.factor_release_id), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
