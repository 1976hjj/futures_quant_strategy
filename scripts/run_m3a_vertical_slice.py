"""Run the frozen M3-A seed catalog on a small audited real-data slice."""

from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from alpha_research_os.factors import FeatureInputRow, FeatureRuntime, FeatureValue, build_initial_catalog
from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import DataDomain

CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
FIELD_DOMAINS = {
    "open": DataDomain.MARKET,
    "high": DataDomain.MARKET,
    "low": DataDomain.MARKET,
    "close": DataDomain.MARKET,
    "volume_shares": DataDomain.MARKET,
    "return_1d": DataDomain.MARKET,
    "illiquidity_1d": DataDomain.MARKET,
    "pb": DataDomain.MARKET,
    "pe_ttm": DataDomain.MARKET,
    "total_mv": DataDomain.MARKET,
    "roe": DataDomain.FUNDAMENTAL,
    "debt_to_assets": DataDomain.FUNDAMENTAL,
}


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _rows(connection: duckdb.DuckDBPyConnection, instruments: tuple[str, ...], start: date, end: date):
    placeholders = ",".join("?" for _ in instruments)
    query = f"""
        SELECT
            m.trade_date, m.ts_code, m.open, m.high, m.low, m.close, m.volume_shares,
            CASE WHEN m.pre_close > 0 THEN m.close / m.pre_close - 1 END AS return_1d,
            CASE WHEN m.pre_close > 0 AND m.amount_cny > 0
                 THEN abs(m.close / m.pre_close - 1) / m.amount_cny END AS illiquidity_1d,
            CASE WHEN b.pb > 0 THEN b.pb END AS pb,
            CASE WHEN b.pe_ttm > 0 THEN b.pe_ttm END AS pe_ttm,
            CASE WHEN b.total_mv > 0 THEN b.total_mv END AS total_mv,
            f.roe, f.debt_to_assets
        FROM research.market_daily m
        JOIN research.universe_daily u USING (trade_date, ts_code)
        LEFT JOIN research.daily_basic b USING (trade_date, ts_code)
        LEFT JOIN LATERAL (
            SELECT roe, debt_to_assets
            FROM research.financial_pit_asof p
            WHERE p.source_api = 'fina_indicator_vip'
              AND p.ts_code = m.ts_code
              AND p.valid_from < m.trade_date
              AND (p.valid_to IS NULL OR m.trade_date < p.valid_to)
            ORDER BY p.valid_from DESC, p.revision_number DESC
            LIMIT 1
        ) f ON true
        WHERE m.ts_code IN ({placeholders})
          AND m.trade_date BETWEEN ? AND ?
          AND u.eligible_for_signal
        ORDER BY m.trade_date, m.ts_code
    """
    return connection.execute(query, [*instruments, start, end]).fetchall()


def run(database: Path, output: Path, start: date, end: date, instruments: tuple[str, ...]) -> dict[str, object]:
    catalog = build_initial_catalog()
    runtime = FeatureRuntime(FIELD_DOMAINS)
    with duckdb.connect(str(database), read_only=True) as connection:
        input_rows = _rows(connection, instruments, start, end)
        lineage_tables = (
            "metadata.archive_manifest",
            "metadata.m2b_archive_manifest",
            "metadata.m2c_archive_manifest",
            "metadata.m2d_archive_manifest",
        )
        lineage = {}
        for table_name in lineage_tables:
            rows = connection.execute(f"SELECT DISTINCT checkpoint_hash FROM {table_name} ORDER BY 1").fetchall()
            lineage[table_name] = [row[0] for row in rows]

    features = tuple(FIELD_DOMAINS)
    prepared = tuple(
        FeatureInputRow(
            session=row[0],
            instrument_id=row[1],
            available_at=datetime.combine(row[0], time(15, 0), tzinfo=CHINA_STANDARD_TIME),
            values=tuple(FeatureValue(name=name, value=row[index + 2]) for index, name in enumerate(features)),
        )
        for row in input_rows
    )
    output_rows = []
    factor_stats = []
    for cataloged in catalog.list():
        registered = catalog.registry.get(cataloged.entry.spec.factor_id, cataloged.entry.spec.factor_version)
        values = runtime.run(registered, prepared)
        present = [float(item.value) for item in values if item.value is not None]
        if any(not math.isfinite(value) for value in present):
            raise ValueError(f"non-finite factor output: {registered.spec.factor_id}")
        factor_stats.append(
            {
                "factor_id": registered.spec.factor_id,
                "factor_version": registered.spec.factor_version,
                "lifecycle": cataloged.entry.lifecycle.value,
                "rows": len(values),
                "present": len(present),
                "coverage": len(present) / len(values) if values else 0.0,
                "minimum": min(present) if present else None,
                "maximum": max(present) if present else None,
            }
        )
        output_rows.extend(
            {
                "session": item.session,
                "instrument_id": item.instrument_id,
                "factor_id": item.factor_id,
                "factor_version": item.factor_version,
                "variant": item.variant,
                "value": item.value,
                "available_at": item.available_at,
                "implementation_hash": item.implementation_hash,
            }
            for item in values
        )
    output.mkdir(parents=True, exist_ok=True)
    parquet = output / "factor_values.parquet"
    temporary = parquet.with_name(f".{parquet.name}.{uuid.uuid4().hex}.tmp")
    pq.write_table(pa.Table.from_pylist(output_rows), temporary, compression="zstd", compression_level=6)
    os.replace(temporary, parquet)
    catalog_document = {
        "schema": "m3-factor-catalog-v1",
        "entries": [item.entry.model_dump(mode="json") | {"entry_hash": item.entry_hash} for item in catalog.list()],
    }
    _atomic_json(output / "factor_catalog.json", catalog_document)
    manifest = {
        "schema": "m3a-real-data-vertical-slice-v1",
        "created_at": datetime.now().astimezone().isoformat(),
        "database": str(database.resolve()),
        "coverage": {"start": start.isoformat(), "end": end.isoformat()},
        "instruments": list(instruments),
        "dataset_lineage": lineage,
        "catalog_hash": content_hash(catalog_document),
        "factor_count": len(catalog.list()),
        "input_rows": len(prepared),
        "output_rows": len(output_rows),
        "factor_stats": factor_stats,
        "neutralization_status": "DEFERRED_UNTIL_M2E_HISTORICAL_INDUSTRY_IS_PUBLISHED",
        "label_metrics_status": "OUT_OF_SCOPE_UNTIL_M4_EVIDENCE_FACTORY",
        "status": "PASS",
    }
    _atomic_json(output / "manifest.json", manifest)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/m3a_vertical_slice"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2023, 10, 1))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 3, 31))
    parser.add_argument("--instrument", action="append")
    args = parser.parse_args()
    instruments = tuple(args.instrument or ("600036.SH", "000001.SZ", "000858.SZ"))
    print(json.dumps(run(args.database, args.output, args.start, args.end, instruments), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
