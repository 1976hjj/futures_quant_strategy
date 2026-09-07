"""Publish M4.6 execution-aware evidence from a generic daily score input."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

from alpha_research_os.evaluation import (
    EvidenceFile,
    ExecutionEvidenceManifest,
    ExecutionEvidenceRequest,
    ScoreInputRef,
)
from alpha_research_os.factors.assets import FactorReleaseManifest
from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.portfolio import DailyBarExecutionSpec

ENGINE_VERSION = "duckdb-generic-score-execution-1.0.0"
DEFAULT_FACTOR_RELEASE_ID = "sha256:3e3d4e69428ce879ee9b53ffc6c39bc8b17b8d49780d305ecff8c0e96ee94fe7"


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


def _factor_input(factor_store: Path, release_id: str) -> tuple[FactorReleaseManifest, Path, Path]:
    path = factor_store / "releases" / release_id.removeprefix("sha256:") / "manifest.json"
    manifest = FactorReleaseManifest.model_validate_json(path.read_bytes())
    parquet = factor_store / manifest.parquet_relative_path
    if manifest.release_id != release_id or _sha256_file(parquet) != manifest.parquet_hash:
        raise ValueError("factor score input failed immutable verification")
    return manifest, path, parquet


def _m2e_core_hash(database: Path) -> str:
    with duckdb.connect(str(database), read_only=True) as connection:
        rows = connection.execute(
            "SELECT DISTINCT core_checkpoint_hash FROM metadata.m2e_core_archive_manifest ORDER BY 1"
        ).fetchall()
        if len(rows) != 1:
            raise ValueError("M2-E priority core is not published as one immutable vintage")
        duplicate_limits = connection.execute(
            """
            SELECT count(*) FROM (
              SELECT trade_date, ts_code FROM research.price_limits
              GROUP BY 1,2 HAVING count(*) > 1
            )
            """
        ).fetchone()[0]
        if duplicate_limits:
            raise ValueError("published price limits contain duplicate keys")
        return str(rows[0][0])


def _request(
    database: Path,
    manifest: FactorReleaseManifest,
    manifest_path: Path,
    start: date,
    end: date,
    spec: DailyBarExecutionSpec,
    capital: tuple[int, ...],
    engine_version: str = ENGINE_VERSION,
    selection_quantile: float = 0.20,
) -> ExecutionEvidenceRequest:
    return ExecutionEvidenceRequest(
        engine_version=engine_version,
        score_input=ScoreInputRef(
            input_id=manifest.release_id,
            manifest_hash=_sha256_file(manifest_path),
            parquet_hash=manifest.parquet_hash,
            score_namespace="FACTOR_AND_EQUAL_WEIGHT_RANK_COMBINATION",
        ),
        m2e_core_checkpoint_hash=_m2e_core_hash(database),
        execution_spec_hash=spec.spec_hash,
        universe_id=manifest.request.universe_id,
        start=start,
        end=end,
        selection_quantile=selection_quantile,
        capital_scenarios_cny=capital,
    )


def _raw_score_sql(factor_path: Path, start: date, end: date) -> str:
    return f"""
    SELECT session, instrument_id, factor_id AS score_id, factor_version AS score_version, value AS score
    FROM read_parquet('{_sql_path(factor_path)}')
    WHERE session BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}'
      AND isfinite(value)
    """


def _selected_sql(
    raw_score_path: Path,
    start: date,
    end: date,
    quantile: float,
    holding_sessions: int,
) -> str:
    threshold = 1.0 - quantile
    scope_days = holding_sessions * 2 + 14
    return f"""
    WITH raw_scores AS (
      SELECT * FROM read_parquet('{_sql_path(raw_score_path)}')
    ), ranked_factors AS (
      SELECT *, percent_rank() OVER (PARTITION BY session, score_id ORDER BY score) AS score_rank
      FROM raw_scores
    ), combination AS (
      SELECT session, instrument_id,
             'equal-weight-rank-combination' AS score_id, '1.0.0' AS score_version,
             avg(score_rank) AS score
      FROM ranked_factors GROUP BY 1,2 HAVING count(*) >= 5
    ), all_scores AS (
      SELECT session, instrument_id, score_id, score_version, score FROM raw_scores
      UNION ALL SELECT * FROM combination
    ), ranked AS (
      SELECT *, percent_rank() OVER (PARTITION BY session, score_id ORDER BY score) AS selection_rank
      FROM all_scores
    ), selected AS (
      SELECT *, count(*) OVER (PARTITION BY session, score_id) AS selected_count
      FROM ranked WHERE selection_rank >= {threshold}
    ), calendar AS (
      SELECT cal_date, row_number() OVER (ORDER BY cal_date) AS session_number
      FROM research.trading_calendar WHERE exchange='SSE' AND is_open
    ), market_scope AS (
      SELECT * FROM research.market_daily
      WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}' + INTERVAL {scope_days} DAYS
    ), adjustment_scope AS (
      SELECT * FROM research.adj_factor
      WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}' + INTERVAL {scope_days} DAYS
    ), state_scope AS (
      SELECT * FROM research.security_session_state
      WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}' + INTERVAL {scope_days} DAYS
    ), limit_scope AS (
      SELECT * FROM research.price_limits
      WHERE trade_date BETWEEN DATE '{start.isoformat()}' AND DATE '{end.isoformat()}' + INTERVAL {scope_days} DAYS
    ), targets AS (
      SELECT s.*, entry.cal_date AS entry_session, scheduled.cal_date AS scheduled_exit_session
      FROM selected s
      JOIN calendar signal ON signal.cal_date=s.session
      LEFT JOIN calendar entry ON entry.session_number=signal.session_number+1
      LEFT JOIN calendar scheduled ON scheduled.session_number=signal.session_number+{holding_sessions + 1}
    ), effective_targets AS (
      SELECT t.*, sm.delist_date,
             CASE WHEN sm.delist_date BETWEEN t.entry_session AND t.scheduled_exit_session
                  THEN sm.delist_date ELSE t.scheduled_exit_session END AS exit_session,
             sm.delist_date BETWEEN t.entry_session AND t.scheduled_exit_session AS delisting_exit
      FROM targets t LEFT JOIN research.security_master sm ON sm.ts_code=t.instrument_id
    )
    SELECT t.*,
           signal_state.eligible_for_signal AS signal_eligible,
           entry_state.is_suspended AS entry_suspended,
           entry_state.is_tradeable_bar AS entry_tradeable,
           em.open AS entry_price, ea.adj_factor AS entry_adj_factor,
           em.amount_cny AS entry_amount_cny, el.up_limit AS entry_up_limit,
           exit_state.is_suspended AS exit_suspended,
           exit_state.is_tradeable_bar AS exit_tradeable,
           xm.close AS exit_price, xa.adj_factor AS exit_adj_factor,
           xm.amount_cny AS exit_amount_cny, xl.down_limit AS exit_down_limit
    FROM effective_targets t
    LEFT JOIN state_scope signal_state
      ON signal_state.trade_date=t.session AND signal_state.ts_code=t.instrument_id
    LEFT JOIN state_scope entry_state
      ON entry_state.trade_date=t.entry_session AND entry_state.ts_code=t.instrument_id
    LEFT JOIN market_scope em
      ON em.trade_date=t.entry_session AND em.ts_code=t.instrument_id
    LEFT JOIN adjustment_scope ea
      ON ea.trade_date=t.entry_session AND ea.ts_code=t.instrument_id
    LEFT JOIN limit_scope el
      ON el.trade_date=t.entry_session AND el.ts_code=t.instrument_id
    LEFT JOIN state_scope exit_state
      ON exit_state.trade_date=t.exit_session AND exit_state.ts_code=t.instrument_id
    LEFT JOIN market_scope xm
      ON xm.trade_date=t.exit_session AND xm.ts_code=t.instrument_id
    LEFT JOIN adjustment_scope xa
      ON xa.trade_date=t.exit_session AND xa.ts_code=t.instrument_id
    LEFT JOIN limit_scope xl
      ON xl.trade_date=t.exit_session AND xl.ts_code=t.instrument_id
    """


def _outcomes_sql(selected_path: Path, spec: DailyBarExecutionSpec, capital: tuple[int, ...]) -> str:
    values = ",".join(f"({value})" for value in capital)
    tol = spec.limit_price_tolerance
    return f"""
    WITH capital(capital_cny) AS (VALUES {values}), expanded AS (
      SELECT s.*, c.capital_cny, c.capital_cny / s.selected_count AS order_notional_cny,
             (c.capital_cny / s.selected_count) / s.entry_amount_cny AS entry_participation,
             (c.capital_cny / s.selected_count) / s.exit_amount_cny AS exit_participation
      FROM read_parquet('{_sql_path(selected_path)}') s CROSS JOIN capital c
    ), classified AS (
      SELECT *, CASE
        WHEN NOT coalesce(signal_eligible, false) THEN 'SIGNAL_INELIGIBLE'
        WHEN entry_session IS NULL OR scheduled_exit_session IS NULL THEN 'INSUFFICIENT_FUTURE_SESSIONS'
        WHEN entry_price IS NULL OR entry_adj_factor IS NULL THEN 'ENTRY_OBSERVATION_MISSING'
        WHEN coalesce(entry_suspended, true) OR NOT coalesce(entry_tradeable, false)
          THEN 'ENTRY_SUSPENDED_OR_UNTRADABLE'
        WHEN entry_up_limit IS NULL THEN 'ENTRY_LIMIT_UNKNOWN'
        WHEN entry_price >= entry_up_limit * (1 - {tol}) THEN 'ENTRY_LIMIT_UP_BLOCKED'
        WHEN entry_amount_cny IS NULL OR entry_amount_cny <= 0 THEN 'ENTRY_LIQUIDITY_INVALID'
        WHEN entry_participation > {spec.maximum_participation_rate} THEN 'ENTRY_CAPACITY_BLOCKED'
        WHEN exit_price IS NULL OR exit_adj_factor IS NULL THEN
          CASE WHEN delisting_exit THEN 'DELISTING_RETURN_UNAVAILABLE' ELSE 'EXIT_OBSERVATION_MISSING' END
        WHEN coalesce(exit_suspended, true) OR NOT coalesce(exit_tradeable, false) THEN 'EXIT_SUSPENDED_OR_UNTRADABLE'
        WHEN exit_down_limit IS NULL THEN 'EXIT_LIMIT_UNKNOWN'
        WHEN exit_price <= exit_down_limit * (1 + {tol}) THEN 'EXIT_LIMIT_DOWN_BLOCKED'
        WHEN exit_amount_cny IS NULL OR exit_amount_cny <= 0 THEN 'EXIT_LIQUIDITY_INVALID'
        WHEN exit_participation > {spec.maximum_participation_rate} THEN 'EXIT_CAPACITY_BLOCKED'
        ELSE 'FILLED' END AS outcome
      FROM expanded
    ), costs AS (
      SELECT *, least(
               {spec.maximum_slippage_bps},
               {spec.base_slippage_bps}+{spec.square_root_impact_bps}*sqrt(entry_participation)
             ) AS buy_slippage_bps,
             least(
               {spec.maximum_slippage_bps},
               {spec.base_slippage_bps}+{spec.square_root_impact_bps}*sqrt(exit_participation)
             ) AS sell_slippage_bps
      FROM classified
    )
    SELECT *,
      CASE WHEN outcome='FILLED' THEN (exit_price*exit_adj_factor)/(entry_price*entry_adj_factor)-1 END AS gross_return,
      CASE WHEN outcome='FILLED' THEN
        ((exit_price*exit_adj_factor)*(1-sell_slippage_bps/10000.0))/
        ((entry_price*entry_adj_factor)*(1+buy_slippage_bps/10000.0))-1
        - ({spec.buy_commission_bps}+{spec.sell_commission_bps}+{spec.sell_stamp_duty_bps})/10000.0
      END AS net_return,
      CASE WHEN outcome='FILLED' THEN buy_slippage_bps+sell_slippage_bps+
        {spec.buy_commission_bps}+{spec.sell_commission_bps}+{spec.sell_stamp_duty_bps} END AS total_cost_bps
    FROM costs
    """


def _copy(connection: duckdb.DuckDBPyConnection, query: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    connection.execute(f"COPY ({query}) TO '{_sql_path(temporary)}' (FORMAT PARQUET, COMPRESSION ZSTD)")
    os.replace(temporary, target)


def _register(database: Path, evidence_store: Path, manifest: ExecutionEvidenceManifest) -> None:
    manifest_path = (
        evidence_store / "execution" / manifest.execution_evidence_id.removeprefix("sha256:") / "manifest.json"
    )
    with duckdb.connect(str(database)) as connection:
        connection.execute("CREATE SCHEMA IF NOT EXISTS metadata")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata.execution_evidence_registry (
              execution_evidence_id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ NOT NULL,
              score_input_id VARCHAR NOT NULL, execution_spec_hash VARCHAR NOT NULL,
              m2e_core_checkpoint_hash VARCHAR NOT NULL, manifest_path VARCHAR NOT NULL,
              quality_status VARCHAR NOT NULL, decision_status VARCHAR NOT NULL
            )
            """
        )
        connection.execute(
            "DELETE FROM metadata.execution_evidence_registry WHERE execution_evidence_id=?",
            [manifest.execution_evidence_id],
        )
        connection.execute(
            "INSERT INTO metadata.execution_evidence_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                manifest.execution_evidence_id,
                manifest.created_at,
                manifest.request.score_input.input_id,
                manifest.request.execution_spec_hash,
                manifest.request.m2e_core_checkpoint_hash,
                str(manifest_path.resolve()),
                manifest.quality_status,
                manifest.decision_status,
            ],
        )


def publish(
    database: Path,
    factor_store: Path,
    evidence_store: Path,
    release_id: str,
    start: date,
    end: date,
    *,
    holding_sessions: int = 5,
    selection_quantile: float = 0.20,
    capital: tuple[int, ...] = (1_000_000, 10_000_000, 100_000_000),
    execution_spec: DailyBarExecutionSpec | None = None,
) -> dict[str, Any]:
    if holding_sessions not in {5, 10, 20, 30}:
        raise ValueError("supported holding periods are 5, 10, 20, and 30 sessions")
    manifest, factor_manifest_path, factor_path = _factor_input(factor_store, release_id)
    spec = execution_spec or DailyBarExecutionSpec()
    engine_version = ENGINE_VERSION if holding_sessions == 5 else f"{ENGINE_VERSION}-hold-{holding_sessions}"
    request = _request(
        database,
        manifest,
        factor_manifest_path,
        start,
        end,
        spec,
        capital,
        engine_version,
        selection_quantile,
    )
    directory = evidence_store / "execution" / request.execution_evidence_id.removeprefix("sha256:")
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    targets = {
        "daily_execution": directory / "daily_execution.parquet",
        "entity_summary": directory / "entity_summary.parquet",
        "rejection_summary": directory / "rejection_summary.parquet",
        "turnover_summary": directory / "turnover_summary.parquet",
    }
    if manifest_path.exists() and all(path.exists() for path in targets.values()):
        existing = ExecutionEvidenceManifest.model_validate_json(manifest_path.read_bytes())
        hashes = {item.name: item.artifact_hash for item in existing.files}
        if existing.request != request or any(_sha256_file(path) != hashes[name] for name, path in targets.items()):
            raise ValueError("cached M4.6 evidence failed immutable verification")
        _register(database, evidence_store, existing)
        return {"cache_hit": True, "execution_evidence_id": existing.execution_evidence_id}

    temp = evidence_store / "duckdb_tmp" / request.execution_evidence_id.removeprefix("sha256:")
    temp.mkdir(parents=True, exist_ok=True)
    selected_paths = []
    outcomes_path = temp / "outcomes.parquet"
    with duckdb.connect() as connection:
        connection.execute(f"ATTACH '{_sql_path(database)}' AS warehouse (READ_ONLY)")
        connection.execute("SET memory_limit='10GB'")
        connection.execute("SET threads=2")
        connection.execute("SET preserve_insertion_order=false")
        connection.execute(f"SET temp_directory='{_sql_path(temp)}'")
        connection.execute("SET TimeZone='Asia/Shanghai'")
        connection.execute("USE warehouse")
        for year in range(start.year, end.year + 1):
            for month in range(1, 13):
                partition_start = max(start, date(year, month, 1))
                partition_end = min(end, date(year, month, monthrange(year, month)[1]))
                if partition_start > partition_end:
                    continue
                partition = f"{year}{month:02d}"
                raw_score_path = temp / "raw_scores" / f"partition={partition}" / "data.parquet"
                selected_path = temp / "selected" / f"partition={partition}" / "data.parquet"
                if not raw_score_path.exists():
                    _copy(connection, _raw_score_sql(factor_path, partition_start, partition_end), raw_score_path)
                _copy(
                    connection,
                    _selected_sql(
                        raw_score_path,
                        partition_start,
                        partition_end,
                        request.selection_quantile,
                        holding_sessions,
                    ),
                    selected_path,
                )
                raw_score_path.unlink()
                selected_paths.append(selected_path)
        connection.execute("USE memory")
        selected_glob = temp / "selected" / "partition=*" / "data.parquet"
        _copy(connection, _outcomes_sql(selected_glob, spec, capital), outcomes_path)
        outcome = f"read_parquet('{_sql_path(outcomes_path)}')"
        _copy(
            connection,
            f"""SELECT score_id, score_version, session, capital_cny,
                count(*) AS selected_count,
                count(*) FILTER (WHERE outcome='FILLED') AS filled_count,
                avg(gross_return) FILTER (WHERE outcome='FILLED') AS gross_return,
                avg(net_return) FILTER (WHERE outcome='FILLED') AS net_return,
                avg(total_cost_bps) FILTER (WHERE outcome='FILLED') AS average_cost_bps,
                count(*) FILTER (WHERE delisting_exit) AS delisting_exit_count
              FROM {outcome} GROUP BY 1,2,3,4""",
            targets["daily_execution"],
        )
        _copy(
            connection,
            f"""SELECT score_id, score_version, capital_cny, count(DISTINCT session) AS sessions,
                sum(selected_count) AS selected_orders, sum(filled_count) AS filled_orders,
                sum(filled_count)::DOUBLE/nullif(sum(selected_count),0) AS fill_rate,
                avg(gross_return) AS average_daily_gross_return,
                avg(net_return) AS average_daily_net_return,
                avg(average_cost_bps) AS average_cost_bps,
                sum(delisting_exit_count) AS delisting_exit_count
              FROM read_parquet('{_sql_path(targets['daily_execution'])}') GROUP BY 1,2,3""",
            targets["entity_summary"],
        )
        _copy(
            connection,
            f"""SELECT score_id, score_version, capital_cny, outcome, count(*) AS order_count
              FROM {outcome} GROUP BY 1,2,3,4""",
            targets["rejection_summary"],
        )
        _copy(
            connection,
            f"""WITH membership AS (
                SELECT DISTINCT score_id, session, instrument_id FROM {outcome}
              ), ordered AS (
                SELECT DISTINCT score_id, session,
                  lag(session) OVER (PARTITION BY score_id ORDER BY session) AS previous_session
                FROM membership
              ), counts AS (
                SELECT o.score_id, o.session, count(c.instrument_id) AS selected_count,
                  count(p.instrument_id) AS retained_count
                FROM ordered o JOIN membership c ON c.score_id=o.score_id AND c.session=o.session
                LEFT JOIN membership p ON p.score_id=o.score_id AND p.session=o.previous_session
                  AND p.instrument_id=c.instrument_id
                GROUP BY 1,2
              ) SELECT score_id, session, selected_count, retained_count,
                  1-retained_count::DOUBLE/nullif(selected_count,0) AS one_way_turnover
                FROM counts""",
            targets["turnover_summary"],
        )

    for selected_path in selected_paths:
        selected_path.unlink(missing_ok=True)
    outcomes_path.unlink(missing_ok=True)
    files = []
    with duckdb.connect() as connection:
        for name, path in sorted(targets.items()):
            rows = connection.execute(f"SELECT count(*) FROM read_parquet('{_sql_path(path)}')").fetchone()[0]
            files.append(
                EvidenceFile(
                    name=name,
                    relative_path=path.relative_to(evidence_store).as_posix(),
                    artifact_hash=_sha256_file(path),
                    row_count=rows,
                )
            )
        score_count = connection.execute(
            f"SELECT count(DISTINCT score_id) FROM read_parquet('{_sql_path(targets['entity_summary'])}')"
        ).fetchone()[0]
    published = ExecutionEvidenceManifest(
        execution_evidence_id=request.execution_evidence_id,
        request=request,
        created_at=datetime.now().astimezone(),
        files=tuple(files),
        score_count=score_count,
        quality_status="PASS",
        decision_status="EXECUTION_DIAGNOSTIC_EXPOSED_SAMPLE",
        limitations=(
            f"The {start.isoformat()} to {end.isoformat()} research window is diagnostic, not an unseen holdout.",
            "Daily bars cannot observe queue position; touching a limit is conservatively treated as no fill.",
            (
                "Delisting uses the observed delisting-session close when present and otherwise records "
                "an invalid outcome."
            ),
            "Impact is a frozen square-root proxy, not a broker fill or intraday order-book reconstruction.",
            "Factor and combination results are diagnostics and cannot promote a factor to the Core Pool.",
        ),
    )
    _atomic_write(manifest_path, canonical_json_bytes(published))
    _register(database, evidence_store, published)
    return {
        "cache_hit": False,
        "execution_evidence_id": published.execution_evidence_id,
        "score_count": score_count,
        "manifest": str(manifest_path.resolve()),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--factor-store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--evidence-store", type=Path, default=Path("data/evidence_store"))
    parser.add_argument("--factor-release-id", default=DEFAULT_FACTOR_RELEASE_ID)
    parser.add_argument("--start", type=date.fromisoformat, default=date(2020, 1, 2))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2025, 12, 31))
    parser.add_argument("--holding-sessions", type=int, choices=(5, 10, 20, 30), default=5)
    parser.add_argument("--selection-quantile", type=float, default=0.20)
    parser.add_argument("--capital", type=int, action="append")
    args = parser.parse_args()
    result = publish(
        args.database,
        args.factor_store,
        args.evidence_store,
        args.factor_release_id,
        args.start,
        args.end,
        holding_sessions=args.holding_sessions,
        selection_quantile=args.selection_quantile,
        capital=tuple(sorted(set(args.capital or (1_000_000, 10_000_000, 100_000_000)))),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
