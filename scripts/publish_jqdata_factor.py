"""Fetch and publish one immutable factor release from the official JQData factor API."""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
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

PROVIDER_ENGINE_VERSION = "jqdata-factor-values-1.0.0"
LOCAL_ENGINE_VERSION = "jqdata-public-formula-duckdb-1.2.0"
FUNDAMENTAL_ENGINE_VERSION = "jqdata-public-formula-duckdb-1.1.0"
SIGNAL_CLOCK_VERSION = "cn-close-postclose-v1"
SECURITY_BATCH_SIZE = 400
LOCAL_FORMULA_FACTORS = {
    "ACCA",
    "ATR6",
    "DAVOL10",
    "Rank1M",
    "Variance20",
    "adjusted_profit_to_total_profit",
    "beta",
    "book_to_price_ratio",
    "cash_flow_to_price_ratio",
    "cash_earnings_to_price_ratio",
    "debt_to_equity_ratio",
    "earnings_to_price_ratio",
    "growth",
    "liquidity",
    "momentum",
    "natural_log_of_market_cap",
    "net_operating_cash_flow_coverage",
    "roa_ttm",
    "roe_ttm",
    "sharpe_ratio_60",
    "share_turnover_monthly",
    "daily_standard_deviation",
}
FUNDAMENTAL_FORMULA_FACTORS = {
    "ACCA",
    "adjusted_profit_to_total_profit",
    "cash_flow_to_price_ratio",
    "cash_earnings_to_price_ratio",
    "debt_to_equity_ratio",
    "earnings_to_price_ratio",
    "growth",
    "net_operating_cash_flow_coverage",
    "roa_ttm",
    "roe_ttm",
}

LOOKBACK_SESSIONS = {
    "ATR6": 6,
    "DAVOL10": 120,
    "Rank1M": 21,
    "Variance20": 20,
    "beta": 252,
    "daily_standard_deviation": 252,
    "liquidity": 21,
    "momentum": 252,
    "sharpe_ratio_60": 60,
    "share_turnover_monthly": 21,
}


def _register_with_lock_retry(*args: Any) -> None:
    """Let DuckDB release completed read-only handles before opening the writer on Windows."""

    gc.collect()
    for attempt in range(5):
        try:
            _register(*args)
            return
        except duckdb.IOException:
            if attempt == 4:
                raise
            gc.collect()
            time.sleep(0.25 * (attempt + 1))


def _engine_version(item: JQDataCatalogItem) -> str:
    if item.external_name in FUNDAMENTAL_FORMULA_FACTORS:
        return FUNDAMENTAL_ENGINE_VERSION
    if item.external_name in LOCAL_FORMULA_FACTORS:
        return LOCAL_ENGINE_VERSION
    return PROVIDER_ENGINE_VERSION


def _catalog(item: JQDataCatalogItem) -> FactorCatalog:
    local_formula = item.external_name in LOCAL_FORMULA_FACTORS
    engine_version = _engine_version(item)
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
            if item.external_name in FUNDAMENTAL_FORMULA_FACTORS
            else (DataDomain.MARKET,)
        ),
        lookback_sessions=LOOKBACK_SESSIONS.get(item.external_name, 0),
        warmup_sessions=max(LOOKBACK_SESSIONS.get(item.external_name, 1) - 1, 0),
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
    connection.close()
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
        connection.close()


def _fundamental_materialization_body(
    item: JQDataCatalogItem,
    request: FactorAssetRequest,
    common_select: str,
) -> str:
    expressions = {
        "cash_flow_to_price_ratio": "c.net_cashflow_ttm / nullif(u.total_market_cap, 0)",
        "cash_earnings_to_price_ratio": "c.operating_cashflow_ttm / nullif(u.total_market_cap, 0)",
        "earnings_to_price_ratio": "i.net_income_parent_ttm / nullif(u.total_market_cap, 0)",
        "roe_ttm": "i.net_income_parent_ttm / nullif(b.equity_parent, 0)",
        "roa_ttm": "i.net_income_parent_ttm / nullif(b.total_assets, 0)",
        "ACCA": "(i.net_income_parent_ttm-c.operating_cashflow_ttm) / nullif(b.total_assets, 0)",
        "adjusted_profit_to_total_profit": "f.profit_dedt / nullif(i.total_profit_ttm, 0)",
        "net_operating_cash_flow_coverage": (
            "c.operating_cashflow_ttm / nullif(i.net_income_parent_ttm, 0)"
        ),
        "debt_to_equity_ratio": "b.total_liabilities / nullif(b.equity_parent, 0)",
        "growth": (
            "(coalesce(f.or_yoy,0)+coalesce(f.netprofit_yoy,0)+coalesce(f.assets_yoy,0)) "
            "/ nullif((f.or_yoy IS NOT NULL)::INT+(f.netprofit_yoy IS NOT NULL)::INT+"
            "(f.assets_yoy IS NOT NULL)::INT,0) / 100.0"
        ),
    }
    expression = expressions[item.external_name]
    start = _sql_string(request.start.isoformat())
    end = _sql_string(request.end.isoformat())
    return f"""
      WITH income_versions AS (
        SELECT ts_code,
          coalesce(try_strptime(f_ann_date,'%Y%m%d')::DATE,try_strptime(ann_date,'%Y%m%d')::DATE) AS available_date,
          try_strptime(end_date,'%Y%m%d')::DATE AS period_end,
          try_cast(n_income_attr_p AS DOUBLE) AS net_income_parent,
          try_cast(total_profit AS DOUBLE) AS total_profit
        FROM raw.income_statement_versions
        WHERE coalesce(f_ann_date,ann_date) IS NOT NULL AND end_date IS NOT NULL
        QUALIFY row_number() OVER (
          PARTITION BY ts_code,available_date,period_end ORDER BY try_cast(update_flag AS INT) DESC,
          try_cast(source_retrieved_at AS TIMESTAMPTZ) DESC
        )=1
      ), income_current AS (
        SELECT * FROM income_versions
        QUALIFY period_end=max(period_end) OVER (PARTITION BY ts_code,available_date)
      ), income_ttm AS (
        SELECT p.ts_code,p.available_date,
          CASE WHEN month(p.period_end)=12 THEN p.net_income_parent
            ELSE p.net_income_parent+a.net_income_parent-q.net_income_parent END net_income_parent_ttm,
          CASE WHEN month(p.period_end)=12 THEN p.total_profit
            ELSE p.total_profit+a.total_profit-q.total_profit END total_profit_ttm
        FROM income_current p
        LEFT JOIN LATERAL (
          SELECT x.net_income_parent,x.total_profit FROM income_versions x
          WHERE x.ts_code=p.ts_code AND x.period_end=make_date(year(p.period_end)-1,12,31)
            AND x.available_date<=p.available_date ORDER BY x.available_date DESC LIMIT 1
        ) a ON true
        LEFT JOIN LATERAL (
          SELECT x.net_income_parent,x.total_profit FROM income_versions x
          WHERE x.ts_code=p.ts_code AND x.period_end=p.period_end-INTERVAL 1 YEAR
            AND x.available_date<=p.available_date ORDER BY x.available_date DESC LIMIT 1
        ) q ON true
      ), cash_versions AS (
        SELECT ts_code,
          coalesce(try_strptime(f_ann_date,'%Y%m%d')::DATE,try_strptime(ann_date,'%Y%m%d')::DATE) AS available_date,
          try_strptime(end_date,'%Y%m%d')::DATE AS period_end,
          try_cast(n_cashflow_act AS DOUBLE) AS operating_cashflow,
          try_cast(n_incr_cash_cash_equ AS DOUBLE) AS net_cashflow
        FROM raw.cashflow_statement_versions
        WHERE coalesce(f_ann_date,ann_date) IS NOT NULL AND end_date IS NOT NULL
        QUALIFY row_number() OVER (
          PARTITION BY ts_code,available_date,period_end ORDER BY try_cast(update_flag AS INT) DESC,
          try_cast(source_retrieved_at AS TIMESTAMPTZ) DESC
        )=1
      ), cash_current AS (
        SELECT * FROM cash_versions
        QUALIFY period_end=max(period_end) OVER (PARTITION BY ts_code,available_date)
      ), cash_ttm AS (
        SELECT p.ts_code,p.available_date,
          CASE WHEN month(p.period_end)=12 THEN p.operating_cashflow
            ELSE p.operating_cashflow+a.operating_cashflow-q.operating_cashflow END operating_cashflow_ttm,
          CASE WHEN month(p.period_end)=12 THEN p.net_cashflow
            ELSE p.net_cashflow+a.net_cashflow-q.net_cashflow END net_cashflow_ttm
        FROM cash_current p
        LEFT JOIN LATERAL (
          SELECT x.operating_cashflow,x.net_cashflow FROM cash_versions x
          WHERE x.ts_code=p.ts_code AND x.period_end=make_date(year(p.period_end)-1,12,31)
            AND x.available_date<=p.available_date ORDER BY x.available_date DESC LIMIT 1
        ) a ON true
        LEFT JOIN LATERAL (
          SELECT x.operating_cashflow,x.net_cashflow FROM cash_versions x
          WHERE x.ts_code=p.ts_code AND x.period_end=p.period_end-INTERVAL 1 YEAR
            AND x.available_date<=p.available_date ORDER BY x.available_date DESC LIMIT 1
        ) q ON true
      ), balance_events AS (
        SELECT ts_code,
          coalesce(try_strptime(f_ann_date,'%Y%m%d')::DATE,try_strptime(ann_date,'%Y%m%d')::DATE) AS available_date,
          try_strptime(end_date,'%Y%m%d')::DATE AS period_end,
          try_cast(total_assets AS DOUBLE) AS total_assets,
          try_cast(total_liab AS DOUBLE) AS total_liabilities,
          try_cast(total_hldr_eqy_exc_min_int AS DOUBLE) AS equity_parent
        FROM raw.balance_sheet_versions
        WHERE coalesce(f_ann_date,ann_date) IS NOT NULL AND end_date IS NOT NULL
        QUALIFY row_number() OVER (
          PARTITION BY ts_code,available_date ORDER BY period_end DESC,try_cast(update_flag AS INT) DESC,
          try_cast(source_retrieved_at AS TIMESTAMPTZ) DESC
        )=1
      ), indicator_events AS (
        SELECT ts_code,
          coalesce(try_strptime(ann_date,'%Y%m%d')::DATE,try_strptime(end_date,'%Y%m%d')::DATE) AS available_date,
          try_strptime(end_date,'%Y%m%d')::DATE AS period_end,
          try_cast(profit_dedt AS DOUBLE) AS profit_dedt,try_cast(or_yoy AS DOUBLE) AS or_yoy,
          try_cast(netprofit_yoy AS DOUBLE) AS netprofit_yoy,try_cast(assets_yoy AS DOUBLE) AS assets_yoy
        FROM raw.financial_indicator_versions
        WHERE coalesce(ann_date,end_date) IS NOT NULL
        QUALIFY row_number() OVER (
          PARTITION BY ts_code,available_date ORDER BY period_end DESC,try_cast(update_flag AS INT) DESC,
          try_cast(source_retrieved_at AS TIMESTAMPTZ) DESC
        )=1
      ), universe AS (
        SELECT s.trade_date AS session,s.ts_code AS instrument_id,s.eligible_for_signal,
          d.total_mv*10000.0 AS total_market_cap
        FROM research.security_session_state s LEFT JOIN research.daily_basic d USING(trade_date,ts_code)
        WHERE s.trade_date BETWEEN DATE {start} AND DATE {end}
      ), calculated AS (
        SELECT u.*,{expression} candidate_value
        FROM universe u
        ASOF LEFT JOIN income_ttm i ON u.instrument_id=i.ts_code AND u.session>i.available_date
        ASOF LEFT JOIN cash_ttm c ON u.instrument_id=c.ts_code AND u.session>c.available_date
        ASOF LEFT JOIN balance_events b ON u.instrument_id=b.ts_code AND u.session>b.available_date
        ASOF LEFT JOIN indicator_events f ON u.instrument_id=f.ts_code AND u.session>f.available_date
      )
      SELECT {common_select} FROM calculated WHERE eligible_for_signal
    """


def _market_materialization_body(
    item: JQDataCatalogItem,
    request: FactorAssetRequest,
    common_select: str,
    warmup: date,
) -> str:
    start = _sql_string(request.start.isoformat())
    end = _sql_string(request.end.isoformat())
    warm = _sql_string(warmup.isoformat())
    if item.external_name in {"book_to_price_ratio", "natural_log_of_market_cap"}:
        expression = (
            "1.0/nullif(b.pb,0)"
            if item.external_name == "book_to_price_ratio"
            else "ln(b.total_mv*10000.0)"
        )
        return f"""
          WITH calculated AS (
            SELECT u.trade_date AS session,u.ts_code AS instrument_id,u.eligible_for_signal,
              {expression} AS candidate_value
            FROM research.security_session_state u LEFT JOIN research.daily_basic b USING(trade_date,ts_code)
            WHERE u.trade_date BETWEEN DATE {start} AND DATE {end}
          ) SELECT {common_select} FROM calculated WHERE eligible_for_signal
        """
    return_expression = "m.close/nullif(m.pre_close,0)-1"
    calculated = {
        "ATR6": "CASE WHEN count(true_range) OVER w6=6 THEN avg(true_range/nullif(close,0)) OVER w6 END",
        "DAVOL10": (
            "CASE WHEN count(turnover_ratio) OVER w120=120 "
            "THEN avg(turnover_ratio) OVER w10/nullif(avg(turnover_ratio) OVER w120,0)-1 END"
        ),
        "Variance20": "CASE WHEN count(return_1d) OVER w20=20 THEN var_samp(return_1d) OVER w20*250.0 END",
        "liquidity": "CASE WHEN count(turnover_ratio) OVER w21=21 THEN ln(avg(turnover_ratio) OVER w21) END",
        "momentum": "lag(adjusted_close,21) OVER wp/nullif(lag(adjusted_close,252) OVER wp,0)-1",
        "sharpe_ratio_60": (
            "CASE WHEN count(return_1d) OVER w60=60 "
            "AND stddev_samp(return_1d) OVER w60>1e-8 "
            "THEN avg(return_1d) OVER w60/stddev_samp(return_1d) OVER w60*sqrt(250.0) END"
        ),
    }
    if item.external_name == "Rank1M":
        final_value = (
            "CASE WHEN trailing_return IS NOT NULL THEN 1-percent_rank() "
            "OVER (PARTITION BY session ORDER BY trailing_return NULLS LAST) END"
        )
    elif item.external_name == "beta":
        final_value = (
            "CASE WHEN count(return_1d) OVER w252=252 "
            "AND count(market_return) OVER w252=252 "
            "AND var_samp(market_return) OVER w252>1e-12 "
            "THEN covar_samp(return_1d,market_return) OVER w252/"
            "var_samp(market_return) OVER w252 END"
        )
    else:
        final_value = calculated[item.external_name]
    return f"""
      WITH base0 AS (
        SELECT u.trade_date AS session,u.ts_code AS instrument_id,u.eligible_for_signal,
          m.close,m.high,m.low,m.pre_close,{return_expression} AS return_1d,
          b.turnover_rate/100.0 AS turnover_ratio,m.close*a.adj_factor AS adjusted_close,
          greatest(m.high-m.low,abs(m.high-m.pre_close),abs(m.low-m.pre_close)) AS true_range
        FROM research.security_session_state u
        LEFT JOIN research.market_daily m USING(trade_date,ts_code)
        LEFT JOIN research.daily_basic b USING(trade_date,ts_code)
        LEFT JOIN research.adj_factor a USING(trade_date,ts_code)
        WHERE u.trade_date BETWEEN DATE {warm} AND DATE {end}
      ), base AS (
        SELECT *,avg(return_1d) FILTER(eligible_for_signal) OVER (PARTITION BY session) market_return,
          adjusted_close/nullif(lag(adjusted_close,20) OVER wp,0)-1 trailing_return
        FROM base0 WINDOW wp AS (PARTITION BY instrument_id ORDER BY session)
      ), calculated AS (
        SELECT *,{final_value} candidate_value FROM base
        WINDOW wp AS (PARTITION BY instrument_id ORDER BY session),
          w6 AS (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 5 PRECEDING AND CURRENT ROW),
          w10 AS (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
          w20 AS (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
          w21 AS (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 20 PRECEDING AND CURRENT ROW),
          w60 AS (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 59 PRECEDING AND CURRENT ROW),
          w120 AS (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 119 PRECEDING AND CURRENT ROW),
          w252 AS (PARTITION BY instrument_id ORDER BY session ROWS BETWEEN 251 PRECEDING AND CURRENT ROW)
      ) SELECT {common_select} FROM calculated
      WHERE session BETWEEN DATE {start} AND DATE {end} AND eligible_for_signal
    """


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
    if item.external_name in FUNDAMENTAL_FORMULA_FACTORS:
        body = _fundamental_materialization_body(item, request, common_select)
    elif item.external_name in {
        "ATR6", "DAVOL10", "Rank1M", "Variance20", "beta", "book_to_price_ratio",
        "liquidity", "momentum", "natural_log_of_market_cap", "sharpe_ratio_60",
    }:
        body = _market_materialization_body(item, request, common_select, warmup)
    elif item.external_name == "share_turnover_monthly":
        body = f"""
          WITH base AS (
            SELECT u.trade_date AS session, u.ts_code AS instrument_id, u.eligible_for_signal,
              b.turnover_rate / 100.0 AS turnover_ratio
            FROM research.security_session_state u
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
    elif item.external_name == "daily_standard_deviation":
        body = f"""
          WITH base AS (
            SELECT u.trade_date AS session, u.ts_code AS instrument_id, u.eligible_for_signal,
              row_number() OVER (PARTITION BY u.ts_code ORDER BY u.trade_date)::DOUBLE AS session_seq,
              CASE WHEN m.pre_close>0 THEN m.close/m.pre_close-1 END AS return_1d
            FROM research.security_session_state u
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


def _materialize_local_yearly(
    database: Path,
    store: Path,
    request: FactorAssetRequest,
    item: JQDataCatalogItem,
    target: Path,
    warmup_sessions: int,
) -> None:
    ranges = year_ranges(request.start, request.end)
    with tempfile.TemporaryDirectory(prefix="jqdata-local-years-", dir=target.parent) as staging_name:
        staging = Path(staging_name)

        def materialize_year(bounds: tuple[date, date]) -> Path:
            lower, upper = bounds
            partition = staging / f"year={lower.year}.parquet"
            yearly_request = request.model_copy(update={"start": lower, "end": upper})
            print(f"factor={item.factor_id} year={lower.year} materializing", flush=True)
            with duckdb.connect(str(database), read_only=True) as connection:
                _configure_bounded_connection(connection, store / "duckdb_tmp")
                warmup = _warmup_start(connection, lower, warmup_sessions)
                connection.execute(_local_materialization_sql(item, yearly_request, partition, warmup))
            connection.close()
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


def publish(
    database: Path,
    store: Path,
    factor_id: str,
    start: date,
    end: date,
    *,
    schedule_verification: bool = True,
) -> dict[str, Any]:
    items = {item.factor_id: item for item in jqdata_catalog()}
    if factor_id not in items:
        raise ValueError(f"unknown JQData factor: {factor_id}")
    item = items[factor_id]
    catalog = _catalog(item)
    cataloged = catalog.list()[0]
    spec = cataloged.entry.spec
    local_formula = item.external_name in LOCAL_FORMULA_FACTORS
    engine_version = _engine_version(item)
    with duckdb.connect(str(database), read_only=True) as connection:
        lineage = _lineage(connection)
        m2b_hash = next(
            row.checkpoint_hashes[0] for row in lineage if row.manifest_table == "metadata.m2b_archive_manifest"
        )
        warmup = _warmup_start(connection, start, spec.warmup_sessions)
    connection.close()
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
        _register_with_lock_retry(
            database, store, manifest, content_hash(manifest), catalog, quality["factors"], "jqdata"
        )
        return {"cache_hit": True, "release_id": manifest.release_id,
            "accuracy_status": accuracy_status(release_dir).get("status"),
            "manifest": str(manifest_path.resolve())}

    release_dir.mkdir(parents=True, exist_ok=True)
    temporary = release_dir / f".raw_factor_values.{uuid.uuid4().hex}.tmp.parquet"
    try:
        print(f"factor={factor_id} materializing {start}..{end}", flush=True)
        if local_formula:
            if (end - start).days > 370:
                _materialize_local_yearly(database, store, request, item, temporary, spec.warmup_sessions)
            else:
                with duckdb.connect(str(database), read_only=True) as connection:
                    _configure_bounded_connection(connection, store / "duckdb_tmp")
                    connection.execute(_local_materialization_sql(item, request, temporary, warmup))
                connection.close()
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
        _register_with_lock_retry(
            database, store, manifest, content_hash(manifest), catalog, details, "jqdata"
        )
        verification = (
            schedule_accuracy_verification(database, store, manifest.release_id, factor_id)
            if local_formula and schedule_verification else {"status": "NOT_REQUIRED"}
        )
        return {
            "cache_hit": False,
            "release_id": manifest.release_id,
            "factor_id": factor_id,
            "row_count": manifest.row_count,
            "session_count": manifest.session_count,
            "instrument_count": manifest.instrument_count,
            "accuracy_status": verification["status"],
            "manifest": str(manifest_path.resolve()),
        }
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
    parser.add_argument("--skip-verification", action="store_true")
    args = parser.parse_args()
    result = publish(
        args.database,
        args.store,
        args.factor_id,
        args.start,
        args.end,
        schedule_verification=not args.skip_verification,
    )
    payload = canonical_json_bytes(result) + b"\n"
    if args.result:
        _atomic_write(args.result, payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
