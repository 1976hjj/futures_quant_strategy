"""Calculate and publish one immutable Qlib Alpha158 factor release.

The adapter deliberately publishes one factor per release.  This keeps calculation
status, retries, and downstream M4 evidence independent for every catalog item.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.factors.alpha158 import (  # noqa: E402
    QLIB_ALPHA158_SOURCE,
    Alpha158CatalogItem,
    alpha158_catalog,
)
from alpha_research_os.factors.assets import FactorAssetRef, FactorAssetRequest, FactorReleaseManifest  # noqa: E402
from alpha_research_os.factors.catalog import (  # noqa: E402
    FactorCatalog,
    FactorCatalogEntry,
    FactorLifecycle,
    FactorSource,
    FactorSourceKind,
)
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash  # noqa: E402
from alpha_research_os.kernel.specs import (  # noqa: E402
    DataDomain,
    FactorDirection,
    FactorSpec,
    ImplementationType,
    SignalCutoff,
)
from scripts.factor_compute_runtime import (  # noqa: E402
    LOCAL_FACTOR_YEAR_WORKERS,
    accuracy_status,
    schedule_accuracy_verification,
    year_ranges,
)
from scripts.publish_factor_release import (  # noqa: E402
    _atomic_write,
    _configure_bounded_connection,
    _lineage,
    _quality,
    _register,
    _sha256_file,
    _sql_path,
    _sql_string,
    _warmup_start,
)

ENGINE_VERSION = "duckdb-qlib-alpha158-adapter-1.1.0"
SIGNAL_CLOCK_VERSION = "cn-close-postclose-v1"


def _catalog(item: Alpha158CatalogItem) -> FactorCatalog:
    field_map = {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume_shares",
        "vwap": "amount_cny",
    }
    required = {field_map[field] for field in item.required_fields}
    if set(item.required_fields) & {"open", "high", "low", "close", "volume", "vwap"}:
        required.add("adj_factor")
    if "vwap" in item.required_fields:
        required.add("volume_shares")
    implementation_hash = content_hash(
        {"engine": ENGINE_VERSION, "factor_id": item.factor_id, "formula": item.formula}
    )
    lookback = (item.window_sessions or 0) + 1 if item.window_sessions else 1
    source = FactorSource(
        source_id=item.source_id,
        kind=FactorSourceKind.OPEN_SOURCE_IMPLEMENTATION,
        title="Microsoft Qlib Alpha158",
        uri=QLIB_ALPHA158_SOURCE,
        original_identifier=item.external_name,
        license_note="Formula reproduced from the Microsoft Qlib source under its repository license.",
        formula_verified_against_primary_source=True,
    )
    spec = FactorSpec(
        factor_id=item.factor_id,
        factor_version=item.factor_version,
        name=f"Qlib Alpha158 {item.external_name}",
        author="alpha-research-os",
        source=source.source_id,
        economic_hypothesis=item.description,
        expected_mechanism=item.description,
        implementation_type=ImplementationType.PYTHON,
        python_entrypoint="scripts.publish_alpha158_factor:publish",
        required_fields=tuple(sorted(required)),
        data_domains=(DataDomain.MARKET, DataDomain.CORPORATE_ACTION),
        lookback_sessions=lookback,
        warmup_sessions=max(0, lookback - 1),
        signal_cutoff=SignalCutoff.POST_CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw_then_cross_section_pipeline",
        allowed_universe_ids=("ALL-A-PIT",),
        direction=FactorDirection.TRAIN_FIT,
        implementation_hash=implementation_hash,
        generation_process="On-demand single-factor Alpha158 adapter selected from the M4 control UI.",
        test_references=(f"alpha158-adapter-{item.external_name.lower()}",),
    )
    catalog = FactorCatalog()
    catalog.register(
        FactorCatalogEntry(
            spec=spec,
            family=f"alpha158-{item.family}",
            source_reference=source,
            adaptation_notes=(
                "Qlib expression evaluated on point-in-time A-share bars. Prices and VWAP use the daily "
                "adjustment factor; volume is inversely adjusted. VWAP is derived from amount/volume."
            ),
            lifecycle=FactorLifecycle.RESEARCH_ONLY,
        )
    )
    return catalog


def _rolling(expr: str, window: int) -> str:
    return f"{expr} OVER (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN {window - 1} PRECEDING AND CURRENT ROW)"


def _factor_sql(item: Alpha158CatalogItem) -> str:
    name = item.external_name
    simple = {
        "KMID": "(close-open)/nullif(open,0)",
        "KLEN": "(high-low)/nullif(open,0)",
        "KMID2": "(close-open)/(high-low+1e-12)",
        "KUP": "(high-greatest(open,close))/nullif(open,0)",
        "KUP2": "(high-greatest(open,close))/(high-low+1e-12)",
        "KLOW": "(least(open,close)-low)/nullif(open,0)",
        "KLOW2": "(least(open,close)-low)/(high-low+1e-12)",
        "KSFT": "(2*close-high-low)/nullif(open,0)",
        "KSFT2": "(2*close-high-low)/(high-low+1e-12)",
        "OPEN0": "open/nullif(close,0)",
        "HIGH0": "high/nullif(close,0)",
        "LOW0": "low/nullif(close,0)",
        "VWAP0": "vwap/nullif(close,0)",
    }
    if name in simple:
        return simple[name]
    operator = name.rstrip("0123456789")
    window = int(name[len(operator) :])
    lag_close = f"lag(close,{window}) OVER (PARTITION BY instrument_id ORDER BY session)"
    frame_count = _rolling("count(close)", window)
    mean = lambda value: _rolling(f"avg({value})", window)
    total = lambda value: _rolling(f"sum({value})", window)
    expressions: dict[str, str] = {
        "ROC": f"{lag_close}/nullif(close,0)",
        "MA": f"{mean('close')}/nullif(close,0)",
        "STD": f"{_rolling('stddev_samp(close)', window)}/nullif(close,0)",
        "BETA": f"{_rolling('regr_slope(close, session_seq)', window)}/nullif(close,0)",
        "RSQR": _rolling("regr_r2(close, session_seq)", window),
        "RESI": (
            f"(close-({_rolling('regr_intercept(close, session_seq)', window)}+"
            f"{_rolling('regr_slope(close, session_seq)', window)}*session_seq))/nullif(close,0)"
        ),
        "MAX": f"{_rolling('max(high)', window)}/nullif(close,0)",
        "MIN": f"{_rolling('min(low)', window)}/nullif(close,0)",
        "QTLU": f"{_rolling('quantile_cont(close, 0.8)', window)}/nullif(close,0)",
        "QTLD": f"{_rolling('quantile_cont(close, 0.2)', window)}/nullif(close,0)",
        "RANK": (
            f"(list_count(list_filter({_rolling('list(close)', window)}, x -> x < close)) + "
            f"(list_count(list_filter({_rolling('list(close)', window)}, x -> x = close)) + 1) / 2.0)"
            f"/nullif({frame_count},0)"
        ),
        "RSV": f"(close-{_rolling('min(low)', window)})/({_rolling('max(high)', window)}-{_rolling('min(low)', window)}+1e-12)",
        "IMAX": f"({_rolling('arg_max(session_seq, high)', window)}-{_rolling('min(session_seq)', window)}+1)/{window}.0",
        "IMIN": f"({_rolling('arg_min(session_seq, low)', window)}-{_rolling('min(session_seq)', window)}+1)/{window}.0",
        "IMXD": (
            f"({_rolling('arg_max(session_seq, high)', window)}-"
            f"{_rolling('arg_min(session_seq, low)', window)})/{window}.0"
        ),
        "CORR": _rolling("corr(close, ln(volume+1))", window),
        "CORD": _rolling("corr(close_ratio, ln(volume_ratio+1))", window),
        "CNTP": mean("CASE WHEN close > previous_close THEN 1.0 ELSE 0.0 END"),
        "CNTN": mean("CASE WHEN close < previous_close THEN 1.0 ELSE 0.0 END"),
        "CNTD": f"{mean('CASE WHEN close > previous_close THEN 1.0 ELSE 0.0 END')}-{mean('CASE WHEN close < previous_close THEN 1.0 ELSE 0.0 END')}",
        "SUMP": f"{total('greatest(close-previous_close,0)')}/({total('abs(close-previous_close)')}+1e-12)",
        "SUMN": f"{total('greatest(previous_close-close,0)')}/({total('abs(close-previous_close)')}+1e-12)",
        "SUMD": f"({total('greatest(close-previous_close,0)')}-{total('greatest(previous_close-close,0)')})/({total('abs(close-previous_close)')}+1e-12)",
        "VMA": f"{mean('volume')}/(volume+1e-12)",
        "VSTD": f"{_rolling('stddev_samp(volume)', window)}/(volume+1e-12)",
        "WVMA": f"{_rolling('stddev_samp(abs(close_ratio-1)*volume)', window)}/({mean('abs(close_ratio-1)*volume')}+1e-12)",
        "VSUMP": f"{total('greatest(volume-previous_volume,0)')}/({total('abs(volume-previous_volume)')}+1e-12)",
        "VSUMN": f"{total('greatest(previous_volume-volume,0)')}/({total('abs(volume-previous_volume)')}+1e-12)",
        "VSUMD": f"({total('greatest(volume-previous_volume,0)')}-{total('greatest(previous_volume-volume,0)')})/({total('abs(volume-previous_volume)')}+1e-12)",
    }
    return expressions[operator]


def _materialization_sql(item: Alpha158CatalogItem, request: FactorAssetRequest, target: Path, warmup: date) -> str:
    factor = request.factors[0]
    value_sql = _factor_sql(item)
    return f"""
    COPY (
      WITH base AS (
        SELECT u.trade_date AS session, u.ts_code AS instrument_id, u.eligible_for_signal,
          row_number() OVER (PARTITION BY u.ts_code ORDER BY u.trade_date)::DOUBLE AS session_seq,
          CASE WHEN a.adj_factor>0 THEN m.open*a.adj_factor END AS open,
          CASE WHEN a.adj_factor>0 THEN m.high*a.adj_factor END AS high,
          CASE WHEN a.adj_factor>0 THEN m.low*a.adj_factor END AS low,
          CASE WHEN a.adj_factor>0 THEN m.close*a.adj_factor END AS close,
          CASE WHEN a.adj_factor>0 AND m.volume_shares>0 THEN m.volume_shares/a.adj_factor END AS volume,
          CASE WHEN a.adj_factor>0 AND m.volume_shares>0 THEN (m.amount_cny/m.volume_shares)*a.adj_factor END AS vwap
        FROM research.security_session_state u
        LEFT JOIN research.market_daily m USING (trade_date, ts_code)
        LEFT JOIN research.adj_factor a USING (trade_date, ts_code)
        WHERE u.trade_date BETWEEN DATE {_sql_string(warmup.isoformat())} AND DATE {_sql_string(request.end.isoformat())}
      ), derived AS (
        SELECT *,
          lag(close) OVER (PARTITION BY instrument_id ORDER BY session) AS previous_close,
          lag(volume) OVER (PARTITION BY instrument_id ORDER BY session) AS previous_volume
        FROM base
      ), prepared AS (
        SELECT *, close/nullif(previous_close,0) AS close_ratio,
          volume/nullif(previous_volume,0) AS volume_ratio
        FROM derived
      ), calculated AS (
        SELECT *, try_cast(({value_sql}) AS DOUBLE) AS candidate_value FROM prepared
      )
      SELECT {_sql_string(request.computation_key)} AS release_id, session, instrument_id,
        {_sql_string(factor.factor_id)} AS factor_id, {_sql_string(factor.factor_version)} AS factor_version,
        'RAW' AS variant, CASE WHEN isfinite(candidate_value) THEN candidate_value END AS value,
        session::TIMESTAMP AT TIME ZONE 'Asia/Shanghai' + INTERVAL 15 HOURS AS available_at,
        {_sql_string(factor.implementation_hash)} AS implementation_hash
      FROM calculated
      WHERE session BETWEEN DATE {_sql_string(request.start.isoformat())} AND DATE {_sql_string(request.end.isoformat())}
        AND eligible_for_signal
      ORDER BY session, instrument_id
    ) TO '{_sql_path(target)}' (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 122880)
    """


def _materialize_yearly_parallel(
    database: Path,
    store: Path,
    request: FactorAssetRequest,
    item: Alpha158CatalogItem,
    target: Path,
    warmup_sessions: int,
) -> None:
    ranges = year_ranges(request.start, request.end)
    with tempfile.TemporaryDirectory(prefix="alpha158-years-", dir=target.parent) as staging_name:
        staging = Path(staging_name)

        def materialize_year(bounds: tuple[date, date]) -> Path:
            lower, upper = bounds
            partition = staging / f"year={lower.year}.parquet"
            yearly_request = request.model_copy(update={"start": lower, "end": upper})
            print(f"factor={item.factor_id} year={lower.year} materializing", flush=True)
            with duckdb.connect(str(database), read_only=True) as connection:
                _configure_bounded_connection(connection, store / "duckdb_tmp")
                warmup = _warmup_start(connection, lower, warmup_sessions)
                connection.execute(_materialization_sql(item, yearly_request, partition, warmup))
            print(f"factor={item.factor_id} year={lower.year} completed", flush=True)
            return partition

        worker_count = min(LOCAL_FACTOR_YEAR_WORKERS, len(ranges))
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="factor-year") as executor:
            partitions = list(executor.map(materialize_year, ranges))

        sources = ",".join(_sql_string(_sql_path(path)) for path in partitions)
        print(f"factor={item.factor_id} combining {len(partitions)} yearly partitions", flush=True)
        with duckdb.connect() as connection:
            _configure_bounded_connection(connection, store / "duckdb_tmp")
            connection.execute(
                f"""COPY (
                  SELECT {_sql_string(request.computation_key)} AS release_id,
                    session, instrument_id, factor_id, factor_version, variant, value,
                    available_at, implementation_hash
                  FROM read_parquet([{sources}])
                  ORDER BY session, instrument_id, factor_id, factor_version
                ) TO '{_sql_path(target)}'
                (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 122880)"""
            )


def publish(database: Path, store: Path, factor_id: str, start: date, end: date) -> dict[str, Any]:
    items = {item.factor_id: item for item in alpha158_catalog()}
    if factor_id not in items:
        raise ValueError(f"unknown Alpha158 factor: {factor_id}")
    item = items[factor_id]
    catalog = _catalog(item)
    cataloged = catalog.list()[0]
    spec = cataloged.entry.spec
    with duckdb.connect(str(database), read_only=True) as connection:
        lineage = _lineage(connection)
        m2b_hash = next(row.checkpoint_hashes[0] for row in lineage if row.manifest_table == "metadata.m2b_archive_manifest")
        request = FactorAssetRequest(
            engine_version=ENGINE_VERSION,
            factors=(FactorAssetRef(factor_id=spec.factor_id, factor_version=spec.factor_version,
                spec_hash=cataloged.spec_hash, implementation_hash=spec.implementation_hash,
                catalog_entry_hash=cataloged.entry_hash),),
            dataset_lineage=lineage,
            universe_id="ALL-A-PIT",
            universe_version=f"m2b-{m2b_hash.removeprefix('sha256:')[:16]}",
            start=start, end=end, signal_clock_version=SIGNAL_CLOCK_VERSION,
        )
        warmup = _warmup_start(connection, start, spec.warmup_sessions)
    release_dir = store / "releases" / request.computation_key.removeprefix("sha256:")
    parquet = release_dir / "raw_factor_values.parquet"
    quality_path = release_dir / "quality_summary.json"
    manifest_path = release_dir / "manifest.json"
    if manifest_path.exists() and parquet.exists() and quality_path.exists():
        manifest = FactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.request != request or _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError("cached Alpha158 release failed immutable identity verification")
        quality = json.loads(quality_path.read_bytes())
        _register(database, store, manifest, content_hash(manifest), catalog, quality["factors"], "alpha158")
        return {"cache_hit": True, "release_id": manifest.release_id,
            "accuracy_status": accuracy_status(release_dir).get("status"),
            "manifest": str(manifest_path.resolve())}
    release_dir.mkdir(parents=True, exist_ok=True)
    temporary = release_dir / f".raw_factor_values.{uuid.uuid4().hex}.tmp.parquet"
    try:
        print(f"factor={factor_id} materializing {start}..{end}", flush=True)
        if (end - start).days > 370:
            _materialize_yearly_parallel(database, store, request, item, temporary, spec.warmup_sessions)
        else:
            with duckdb.connect(str(database), read_only=True) as connection:
                _configure_bounded_connection(connection, store / "duckdb_tmp")
                connection.execute(_materialization_sql(item, request, temporary, warmup))
        print("quality checking", flush=True)
        quality, details = _quality(temporary, 1)
        os.replace(temporary, parquet)
        _atomic_write(quality_path, canonical_json_bytes(quality))
        manifest = FactorReleaseManifest(
            release_id=request.computation_key, request=request, created_at=datetime.now().astimezone(),
            parquet_relative_path=parquet.relative_to(store).as_posix(), parquet_hash=_sha256_file(parquet),
            row_count=quality["row_count"], session_count=quality["session_count"],
            instrument_count=quality["instrument_count"], factor_count=1, quality_status="PASS",
            quality_summary_hash=_sha256_file(quality_path),
        )
        _atomic_write(manifest_path, canonical_json_bytes(manifest))
        print("publishing metadata", flush=True)
        _register(database, store, manifest, content_hash(manifest), catalog, details, "alpha158")
        verification = schedule_accuracy_verification(database, store, manifest.release_id, factor_id)
        return {"cache_hit": False, "release_id": manifest.release_id, "factor_id": factor_id,
            "row_count": manifest.row_count, "session_count": manifest.session_count,
            "instrument_count": manifest.instrument_count, "accuracy_status": verification["status"],
            "manifest": str(manifest_path.resolve())}
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--store", type=Path, default=Path("data/factor_store"))
    parser.add_argument("--factor-id", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    result = publish(args.database, args.store, args.factor_id, args.start, args.end)
    payload = canonical_json_bytes(result) + b"\n"
    if args.result:
        _atomic_write(args.result, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
