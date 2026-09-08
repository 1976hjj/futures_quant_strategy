"""Fetch and publish one immutable factor release from the official JQData factor API."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import uuid
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.factors.assets import FactorAssetRef, FactorAssetRequest, FactorReleaseManifest  # noqa: E402
from alpha_research_os.factors.catalog import (  # noqa: E402
    FactorCatalog,
    FactorCatalogEntry,
    FactorLifecycle,
    FactorSource,
    FactorSourceKind,
)
from alpha_research_os.factors.jqdata import JQDATA_FACTOR_SOURCE, JQDataCatalogItem, jqdata_catalog  # noqa: E402
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash  # noqa: E402
from alpha_research_os.kernel.specs import (  # noqa: E402
    DataDomain,
    FactorDirection,
    FactorSpec,
    ImplementationType,
    SignalCutoff,
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

PROVIDER_ENGINE_VERSION = "jqdata-factor-values-1.0.0"
LOCAL_ENGINE_VERSION = "jqdata-public-formula-duckdb-1.0.0"
SIGNAL_CLOCK_VERSION = "cn-close-postclose-v1"
SECURITY_BATCH_SIZE = 400
LOCAL_FORMULA_FACTORS = {
    "cash_earnings_to_price_ratio",
    "earnings_to_price_ratio",
    "share_turnover_monthly",
    "daily_standard_deviation",
}


def _catalog(item: JQDataCatalogItem) -> FactorCatalog:
    local_formula = item.external_name in LOCAL_FORMULA_FACTORS
    engine_version = LOCAL_ENGINE_VERSION if local_formula else PROVIDER_ENGINE_VERSION
    implementation_hash = content_hash(
        {"engine": engine_version, "external_name": item.external_name, "formula": item.formula}
    )
    source = FactorSource(
        source_id=item.source_id,
        kind=FactorSourceKind.DATA_VENDOR,
        title="JoinQuant JQData factor library",
        uri=JQDATA_FACTOR_SOURCE,
        original_identifier=item.external_name,
        license_note="Public factor definition from JoinQuant; provider values remain subject to JQData terms.",
        formula_verified_against_primary_source=True,
    )
    spec = FactorSpec(
        factor_id=item.factor_id,
        factor_version=item.factor_version,
        name=f"JQData {item.external_name}",
        author="alpha-research-os",
        source=source.source_id,
        economic_hypothesis=item.description,
        expected_mechanism=item.description,
        implementation_type=ImplementationType.PYTHON,
        python_entrypoint="scripts.publish_jqdata_factor:publish",
        required_fields=item.required_fields,
        data_domains=(
            (DataDomain.MARKET, DataDomain.FUNDAMENTAL)
            if item.external_name in {"cash_earnings_to_price_ratio", "earnings_to_price_ratio"}
            else (DataDomain.MARKET,)
        ),
        lookback_sessions=(
            21 if item.external_name == "share_turnover_monthly"
            else 252 if item.external_name == "daily_standard_deviation"
            else 0
        ),
        warmup_sessions=(
            20 if item.external_name == "share_turnover_monthly"
            else 251 if item.external_name == "daily_standard_deviation"
            else 0
        ),
        signal_cutoff=SignalCutoff.POST_CLOSE,
        missing_value_policy="preserve-provider-missing",
        infinite_value_policy="to-missing",
        outlier_policy="raw-provider-value-then-cross-section-pipeline",
        allowed_universe_ids=("ALL-A-PIT",),
        direction=FactorDirection.POSITIVE if item.expected_direction == "HIGH" else FactorDirection.NEGATIVE,
        implementation_hash=implementation_hash,
        generation_process=(
            "On-demand local evaluation of the public JQData formula on governed PIT inputs."
            if local_formula
            else "On-demand single-factor retrieval from the official JQData get_factor_values API."
        ),
        test_references=(f"jqdata-factor-{item.external_name}",),
    )
    catalog = FactorCatalog()
    catalog.register(
        FactorCatalogEntry(
            spec=spec,
            family=item.family,
            source_reference=source,
            adaptation_notes=(
                "The public JQData formula is evaluated on local PIT inputs; source-data differences can cause "
                "small differences from the JQData board."
                if local_formula
                else "No local proxy formula is used. JQData's published point-in-time factor value is joined "
                "to the system's point-in-time eligible universe by session and security code."
            ),
            lifecycle=FactorLifecycle.RESEARCH_ONLY,
        )
    )
    return catalog


def _jq_code(ts_code: str) -> str:
    code, exchange = ts_code.split(".", 1)
    suffix = {"SH": "XSHG", "SZ": "XSHE", "BJ": "XBJE"}.get(exchange)
    if suffix is None:
        raise ValueError(f"unsupported A-share exchange code: {ts_code}")
    return f"{code}.{suffix}"


def _chunks(values: list[str], size: int) -> Iterable[list[str]]:
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _credentials() -> tuple[str, str]:
    username = os.environ.get("JQDATA_USERNAME", "").strip()
    password = os.environ.get("JQDATA_PASSWORD", "")
    if not username or not password:
        raise RuntimeError(
            "JQData 尚未配置：请在启动后端前设置 JQDATA_USERNAME 和 JQDATA_PASSWORD 环境变量"
        )
    return username, password


def _sdk() -> Any:
    try:
        import jqdatasdk  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError("缺少 jqdatasdk；请安装项目的 data-jqdata 可选依赖") from error
    username, password = _credentials()
    authenticated = jqdatasdk.auth(username, password)
    if authenticated is False:
        raise RuntimeError("JQData 登录失败，请检查账号、密码和数据权限")
    return jqdatasdk


def _write_provider_chunks(
    sdk: Any,
    item: JQDataCatalogItem,
    ts_codes: list[str],
    start: date,
    end: date,
    directory: Path,
) -> list[Path]:
    result: list[Path] = []
    reverse_codes = {_jq_code(code): code for code in ts_codes}
    for index, security_batch in enumerate(_chunks(sorted(reverse_codes), SECURITY_BATCH_SIZE)):
        print(
            f"fetching JQData {item.external_name}: securities {index * SECURITY_BATCH_SIZE + 1}-"
            f"{index * SECURITY_BATCH_SIZE + len(security_batch)} / {len(reverse_codes)}",
            flush=True,
        )
        response = sdk.get_factor_values(
            security_batch,
            factors=[item.external_name],
            start_date=start,
            end_date=end,
        )
        frame = response.get(item.external_name)
        if frame is None or frame.empty:
            continue
        frame.index.name = "session"
        long_frame = frame.reset_index().melt(id_vars="session", var_name="jq_code", value_name="value")
        long_frame["session"] = long_frame["session"].astype("datetime64[ns]").dt.date
        long_frame["instrument_id"] = long_frame["jq_code"].map(reverse_codes)
        long_frame = long_frame[["session", "instrument_id", "value"]]
        path = directory / f"provider-{index:04d}.parquet"
        pq.write_table(pa.Table.from_pandas(long_frame, preserve_index=False), path, compression="zstd")
        result.append(path)
    return result


def _materialize(
    database: Path,
    store: Path,
    request: FactorAssetRequest,
    item: JQDataCatalogItem,
    target: Path,
) -> None:
    factor = request.factors[0]
    with duckdb.connect(str(database), read_only=True) as connection:
        ts_codes = [
            row[0]
            for row in connection.execute(
                """SELECT DISTINCT ts_code FROM research.universe_daily
                WHERE trade_date BETWEEN ? AND ? AND eligible_for_signal ORDER BY ts_code""",
                [request.start, request.end],
            ).fetchall()
        ]
    sdk = _sdk()
    with tempfile.TemporaryDirectory(prefix="jqdata-factor-") as temporary_name:
        temporary = Path(temporary_name)
        chunks = _write_provider_chunks(sdk, item, ts_codes, request.start, request.end, temporary)
        with duckdb.connect(str(database), read_only=True) as connection:
            _configure_bounded_connection(connection, store / "duckdb_tmp")
            if chunks:
                provider_source = f"read_parquet('{_sql_path(temporary / 'provider-*.parquet')}')"
            else:
                provider_source = (
                    "(SELECT NULL::DATE session, NULL::VARCHAR instrument_id, "
                    "NULL::DOUBLE value WHERE false)"
                )
            connection.execute(
                f"""
                COPY (
                  SELECT {_sql_string(request.computation_key)} AS release_id,
                    u.trade_date AS session, u.ts_code AS instrument_id,
                    {_sql_string(factor.factor_id)} AS factor_id,
                    {_sql_string(factor.factor_version)} AS factor_version,
                    'RAW' AS variant,
                    CASE WHEN isfinite(p.value) THEN p.value END AS value,
                    u.trade_date::TIMESTAMP AT TIME ZONE 'Asia/Shanghai' + INTERVAL 15 HOURS AS available_at,
                    {_sql_string(factor.implementation_hash)} AS implementation_hash
                  FROM research.universe_daily u
                  LEFT JOIN {provider_source} p
                    ON p.session=u.trade_date AND p.instrument_id=u.ts_code
                  WHERE u.trade_date BETWEEN DATE {_sql_string(request.start.isoformat())}
                    AND DATE {_sql_string(request.end.isoformat())} AND u.eligible_for_signal
                  ORDER BY session, instrument_id
                ) TO '{_sql_path(target)}'
                (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 122880)
                """
            )


def _local_materialization_sql(
    item: JQDataCatalogItem,
    request: FactorAssetRequest,
    target: Path,
    warmup: date,
) -> str:
    factor = request.factors[0]
    common_select = f"""
      {_sql_string(request.computation_key)} AS release_id, session, instrument_id,
      {_sql_string(factor.factor_id)} AS factor_id,
      {_sql_string(factor.factor_version)} AS factor_version, 'RAW' AS variant,
      CASE WHEN isfinite(candidate_value) THEN candidate_value END AS value,
      session::TIMESTAMP AT TIME ZONE 'Asia/Shanghai' + INTERVAL 15 HOURS AS available_at,
      {_sql_string(factor.implementation_hash)} AS implementation_hash
    """
    if item.external_name == "share_turnover_monthly":
        body = f"""
          WITH base AS (
            SELECT u.trade_date AS session, u.ts_code AS instrument_id, u.eligible_for_signal,
              b.turnover_rate / 100.0 AS turnover_ratio
            FROM research.universe_daily u
            LEFT JOIN research.daily_basic b USING (trade_date, ts_code)
            WHERE u.trade_date BETWEEN DATE {_sql_string(warmup.isoformat())}
              AND DATE {_sql_string(request.end.isoformat())}
          ), calculated AS (
            SELECT *, CASE WHEN count(turnover_ratio) OVER rolling_window = 21
              AND sum(turnover_ratio) OVER rolling_window > 0
              THEN ln(sum(turnover_ratio) OVER rolling_window) END AS candidate_value
            FROM base
            WINDOW rolling_window AS (
              PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 20 PRECEDING AND CURRENT ROW
            )
          )
          SELECT {common_select} FROM calculated
          WHERE session BETWEEN DATE {_sql_string(request.start.isoformat())}
            AND DATE {_sql_string(request.end.isoformat())} AND eligible_for_signal
        """
    elif item.external_name == "cash_earnings_to_price_ratio":
        body = f"""
          WITH cashflow_versions AS (
            SELECT ts_code, available_date, end_date, operating_cashflow,
              max(end_date) OVER (
                PARTITION BY ts_code ORDER BY available_date, end_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
              ) AS latest_end_date
            FROM research.financial_pit_asof
            WHERE source_api='cashflow_vip' AND operating_cashflow IS NOT NULL
            QUALIFY row_number() OVER (
              PARTITION BY ts_code, available_date, end_date
              ORDER BY revision_number DESC, source_retrieved_at DESC
            ) = 1
          ), current_events AS (
            SELECT * FROM cashflow_versions WHERE end_date=latest_end_date
          ), ttm_events AS (
            SELECT p.ts_code, p.available_date,
              CASE WHEN month(p.end_date)=12 THEN p.operating_cashflow
                ELSE p.operating_cashflow + annual.operating_cashflow - prior.operating_cashflow
              END AS operating_cashflow_ttm
            FROM current_events p
            LEFT JOIN LATERAL (
              SELECT x.operating_cashflow FROM cashflow_versions x
              WHERE x.ts_code=p.ts_code AND x.end_date=make_date(year(p.end_date)-1,12,31)
                AND x.available_date<=p.available_date
              ORDER BY x.available_date DESC LIMIT 1
            ) annual ON true
            LEFT JOIN LATERAL (
              SELECT x.operating_cashflow FROM cashflow_versions x
              WHERE x.ts_code=p.ts_code AND x.end_date=p.end_date-INTERVAL 1 YEAR
                AND x.available_date<=p.available_date
              ORDER BY x.available_date DESC LIMIT 1
            ) prior ON true
          ), universe AS (
            SELECT u.trade_date AS session, u.ts_code AS instrument_id, u.eligible_for_signal,
              b.total_mv * 10000.0 AS total_market_cap
            FROM research.universe_daily u
            LEFT JOIN research.daily_basic b USING (trade_date, ts_code)
            WHERE u.trade_date BETWEEN DATE {_sql_string(request.start.isoformat())}
              AND DATE {_sql_string(request.end.isoformat())}
          ), calculated AS (
            SELECT u.*, t.operating_cashflow_ttm / nullif(u.total_market_cap,0) AS candidate_value
            FROM universe u ASOF LEFT JOIN ttm_events t
              ON u.instrument_id=t.ts_code AND u.session>t.available_date
          )
          SELECT {common_select} FROM calculated WHERE eligible_for_signal
        """
    elif item.external_name == "earnings_to_price_ratio":
        body = f"""
          WITH income_versions AS (
            SELECT ts_code, available_date, end_date, net_income_parent,
              max(end_date) OVER (
                PARTITION BY ts_code ORDER BY available_date, end_date
                ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
              ) AS latest_end_date
            FROM research.financial_pit_asof
            WHERE source_api='income_vip' AND net_income_parent IS NOT NULL
            QUALIFY row_number() OVER (
              PARTITION BY ts_code, available_date, end_date
              ORDER BY revision_number DESC, source_retrieved_at DESC
            ) = 1
          ), current_events AS (
            SELECT * FROM income_versions WHERE end_date=latest_end_date
          ), ttm_events AS (
            SELECT p.ts_code, p.available_date,
              CASE WHEN month(p.end_date)=12 THEN p.net_income_parent
                ELSE p.net_income_parent + annual.net_income_parent - prior.net_income_parent
              END AS net_income_parent_ttm
            FROM current_events p
            LEFT JOIN LATERAL (
              SELECT x.net_income_parent FROM income_versions x
              WHERE x.ts_code=p.ts_code AND x.end_date=make_date(year(p.end_date)-1,12,31)
                AND x.available_date<=p.available_date
              ORDER BY x.available_date DESC LIMIT 1
            ) annual ON true
            LEFT JOIN LATERAL (
              SELECT x.net_income_parent FROM income_versions x
              WHERE x.ts_code=p.ts_code AND x.end_date=p.end_date-INTERVAL 1 YEAR
                AND x.available_date<=p.available_date
              ORDER BY x.available_date DESC LIMIT 1
            ) prior ON true
          ), universe AS (
            SELECT u.trade_date AS session, u.ts_code AS instrument_id, u.eligible_for_signal,
              b.total_mv * 10000.0 AS total_market_cap
            FROM research.universe_daily u
            LEFT JOIN research.daily_basic b USING (trade_date, ts_code)
            WHERE u.trade_date BETWEEN DATE {_sql_string(request.start.isoformat())}
              AND DATE {_sql_string(request.end.isoformat())}
          ), calculated AS (
            SELECT u.*, t.net_income_parent_ttm / nullif(u.total_market_cap,0) AS candidate_value
            FROM universe u ASOF LEFT JOIN ttm_events t
              ON u.instrument_id=t.ts_code AND u.session>t.available_date
          )
          SELECT {common_select} FROM calculated WHERE eligible_for_signal
        """
    elif item.external_name == "daily_standard_deviation":
        body = f"""
          WITH base AS (
            SELECT u.trade_date AS session, u.ts_code AS instrument_id, u.eligible_for_signal,
              row_number() OVER (PARTITION BY u.ts_code ORDER BY u.trade_date)::DOUBLE AS session_seq,
              CASE WHEN m.pre_close>0 THEN m.close/m.pre_close-1 END AS return_1d
            FROM research.universe_daily u
            LEFT JOIN research.market_daily m USING (trade_date, ts_code)
            WHERE u.trade_date BETWEEN DATE {_sql_string(warmup.isoformat())}
              AND DATE {_sql_string(request.end.isoformat())}
          ), weighted AS (
            SELECT *, power(0.5, -session_seq/42.0) AS raw_weight
            FROM base
          ), aggregates AS (
            SELECT *, count(return_1d) OVER rolling_window AS observation_count,
              sum(raw_weight) OVER rolling_window AS weight_sum,
              sum(raw_weight*return_1d) OVER rolling_window AS weighted_return_sum,
              sum(raw_weight*return_1d*return_1d) OVER rolling_window AS weighted_square_sum
            FROM weighted
            WINDOW rolling_window AS (
              PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
            )
          ), calculated AS (
            SELECT *, CASE WHEN observation_count=252 THEN sqrt(greatest(
              weighted_square_sum/nullif(weight_sum,0)
              - power(weighted_return_sum/nullif(weight_sum,0),2), 0
            )) END AS candidate_value
            FROM aggregates
          )
          SELECT {common_select} FROM calculated
          WHERE session BETWEEN DATE {_sql_string(request.start.isoformat())}
            AND DATE {_sql_string(request.end.isoformat())} AND eligible_for_signal
        """
    else:
        raise ValueError(f"factor does not have a local formula adapter: {item.external_name}")
    return f"""
      COPY ({body}) TO '{_sql_path(target)}'
      (FORMAT PARQUET, COMPRESSION ZSTD, COMPRESSION_LEVEL 6, ROW_GROUP_SIZE 122880)
    """


def publish(database: Path, store: Path, factor_id: str, start: date, end: date) -> dict[str, Any]:
    items = {item.factor_id: item for item in jqdata_catalog()}
    if factor_id not in items:
        raise ValueError(f"unknown JQData factor: {factor_id}")
    item = items[factor_id]
    catalog = _catalog(item)
    cataloged = catalog.list()[0]
    spec = cataloged.entry.spec
    local_formula = item.external_name in LOCAL_FORMULA_FACTORS
    engine_version = LOCAL_ENGINE_VERSION if local_formula else PROVIDER_ENGINE_VERSION
    with duckdb.connect(str(database), read_only=True) as connection:
        lineage = _lineage(connection)
        m2b_hash = next(
            row.checkpoint_hashes[0] for row in lineage if row.manifest_table == "metadata.m2b_archive_manifest"
        )
        warmup = _warmup_start(connection, start, spec.warmup_sessions)
    request = FactorAssetRequest(
        engine_version=engine_version,
        factors=(FactorAssetRef(
            factor_id=spec.factor_id,
            factor_version=spec.factor_version,
            spec_hash=cataloged.spec_hash,
            implementation_hash=spec.implementation_hash,
            catalog_entry_hash=cataloged.entry_hash,
        ),),
        dataset_lineage=lineage,
        universe_id="ALL-A-PIT",
        universe_version=f"m2b-{m2b_hash.removeprefix('sha256:')[:16]}",
        start=start,
        end=end,
        signal_clock_version=SIGNAL_CLOCK_VERSION,
    )
    release_dir = store / "releases" / request.computation_key.removeprefix("sha256:")
    parquet = release_dir / "raw_factor_values.parquet"
    quality_path = release_dir / "quality_summary.json"
    manifest_path = release_dir / "manifest.json"
    if manifest_path.exists() and parquet.exists() and quality_path.exists():
        manifest = FactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.request != request or _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError("cached JQData release failed immutable identity verification")
        quality = json.loads(quality_path.read_bytes())
        _register(database, store, manifest, content_hash(manifest), catalog, quality["factors"], "jqdata")
        return {"cache_hit": True, "release_id": manifest.release_id, "manifest": str(manifest_path.resolve())}

    release_dir.mkdir(parents=True, exist_ok=True)
    temporary = release_dir / f".raw_factor_values.{uuid.uuid4().hex}.tmp.parquet"
    print(f"factor={factor_id} materializing {start}..{end}", flush=True)
    if local_formula:
        with duckdb.connect(str(database), read_only=True) as connection:
            _configure_bounded_connection(connection, store / "duckdb_tmp")
            connection.execute(_local_materialization_sql(item, request, temporary, warmup))
    else:
        _materialize(database, store, request, item, temporary)
    print("quality checking", flush=True)
    quality, details = _quality(temporary, 1)
    os.replace(temporary, parquet)
    _atomic_write(quality_path, canonical_json_bytes(quality))
    manifest = FactorReleaseManifest(
        release_id=request.computation_key,
        request=request,
        created_at=datetime.now().astimezone(),
        parquet_relative_path=parquet.relative_to(store).as_posix(),
        parquet_hash=_sha256_file(parquet),
        row_count=quality["row_count"],
        session_count=quality["session_count"],
        instrument_count=quality["instrument_count"],
        factor_count=1,
        quality_status="PASS",
        quality_summary_hash=_sha256_file(quality_path),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    print("publishing metadata", flush=True)
    _register(database, store, manifest, content_hash(manifest), catalog, details, "jqdata")
    return {
        "cache_hit": False,
        "release_id": manifest.release_id,
        "factor_id": factor_id,
        "row_count": manifest.row_count,
        "session_count": manifest.session_count,
        "instrument_count": manifest.instrument_count,
        "manifest": str(manifest_path.resolve()),
    }


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
