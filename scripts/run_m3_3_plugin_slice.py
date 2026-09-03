"""Run the M3.3 restricted Python sentinel against a small official M2 slice."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import duckdb

from alpha_research_os.factors import FactorCatalog, FeatureInputRow, FeatureValue
from alpha_research_os.factors.plugin import PythonPluginRuntime, publish_python_plugin
from alpha_research_os.factors.plugin_library import conditional_close_location_plugin
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash
from alpha_research_os.kernel.specs import DataDomain


def _register(
    database: Path,
    plugin_store: Path,
    cataloged,
    plugin,
    plugin_reference,
) -> None:
    spec = cataloged.entry.spec
    registered_at = datetime.now().astimezone()
    with duckdb.connect(str(database)) as connection:
        connection.execute("BEGIN TRANSACTION")
        try:
            connection.execute("CREATE SCHEMA IF NOT EXISTS metadata")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS metadata.python_plugin_registry (
                plugin_id VARCHAR, plugin_version VARCHAR, implementation_hash VARCHAR,
                entrypoint VARCHAR, sandbox_policy VARCHAR, source_artifact_id VARCHAR,
                source_manifest_id VARCHAR, artifact_root VARCHAR, registered_at TIMESTAMPTZ,
                PRIMARY KEY (plugin_id, plugin_version))"""
            )
            existing_plugin = connection.execute(
                """SELECT implementation_hash, source_artifact_id, source_manifest_id
                FROM metadata.python_plugin_registry WHERE plugin_id=? AND plugin_version=?""",
                [plugin.plugin_id, plugin.plugin_version],
            ).fetchone()
            expected_plugin = (
                plugin.implementation_hash,
                plugin_reference.artifact_id,
                plugin_reference.manifest_id,
            )
            if existing_plugin and existing_plugin != expected_plugin:
                raise ValueError("immutable Python plugin registry conflict")
            connection.execute(
                """INSERT OR IGNORE INTO metadata.python_plugin_registry
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    plugin.plugin_id,
                    plugin.plugin_version,
                    plugin.implementation_hash,
                    plugin.entrypoint_ref,
                    "restricted-python-factor-v1",
                    plugin_reference.artifact_id,
                    plugin_reference.manifest_id,
                    plugin_store.resolve().as_posix(),
                    registered_at,
                ],
            )
            existing_factor = connection.execute(
                """SELECT spec_hash, catalog_entry_hash FROM metadata.factor_registry
                WHERE factor_id=? AND factor_version=?""",
                [spec.factor_id, spec.factor_version],
            ).fetchone()
            if existing_factor and existing_factor != (cataloged.spec_hash, cataloged.entry_hash):
                raise ValueError("immutable Python factor registry conflict")
            connection.execute(
                """INSERT OR IGNORE INTO metadata.factor_registry
                (factor_id, factor_version, spec_hash, implementation_hash, catalog_entry_hash,
                 family, lifecycle, source_id, spec_json, registered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    spec.factor_id,
                    spec.factor_version,
                    cataloged.spec_hash,
                    spec.implementation_hash,
                    cataloged.entry_hash,
                    cataloged.entry.family,
                    cataloged.entry.lifecycle.value,
                    cataloged.entry.source_reference.source_id,
                    json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")),
                    registered_at,
                ],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise


def load_rows(database: Path, start: date, end: date) -> tuple[FeatureInputRow, ...]:
    codes = ("600036.SH", "000001.SZ", "000858.SZ")
    with duckdb.connect(str(database), read_only=True) as connection:
        records = connection.execute(
            """SELECT u.trade_date, u.ts_code, m.close, m.high, m.low
            FROM research.universe_daily u
            JOIN research.market_daily m USING (trade_date, ts_code)
            WHERE u.eligible_for_signal
              AND u.ts_code IN (?, ?, ?)
              AND u.trade_date BETWEEN ? AND ?
            ORDER BY u.trade_date, u.ts_code""",
            [*codes, start, end],
        ).fetchall()
    return tuple(
        FeatureInputRow(
            session=session,
            instrument_id=instrument_id,
            available_at=session.strftime("%Y-%m-%dT15:00:00+08:00"),
            values=(
                FeatureValue(name="close", value=close),
                FeatureValue(name="high", value=high),
                FeatureValue(name="low", value=low),
            ),
        )
        for session, instrument_id, close, high, low in records
    )


def run(database: Path, output: Path, plugin_store: Path, start: date, end: date) -> dict[str, object]:
    entry, plugin = conditional_close_location_plugin()
    catalog = FactorCatalog()
    cataloged, registered = catalog.register(entry)
    rows = load_rows(database, start, end)
    runtime = PythonPluginRuntime({"close": DataDomain.MARKET, "high": DataDomain.MARKET, "low": DataDomain.MARKET})
    result = runtime.run(registered, plugin, rows)
    output.mkdir(parents=True, exist_ok=True)
    plugin_reference = publish_python_plugin(ArtifactStore(plugin_store), plugin)
    _register(database, plugin_store, cataloged, plugin, plugin_reference)
    artifact_store = ArtifactStore(output / "store")
    value_payload = canonical_json_bytes([item.model_dump(mode="json") for item in result.values])
    value_reference = artifact_store.put_bytes(
        value_payload,
        media_type="application/vnd.alpha-research-os.raw-python-factor-values+json",
        metadata={
            "factor_id": entry.spec.factor_id,
            "factor_version": entry.spec.factor_version,
            "input_hash": result.input_hash,
            "plugin_hash": result.plugin_hash,
        },
    )
    summary = {
        "catalog_entry_hash": cataloged.entry_hash,
        "factor_id": entry.spec.factor_id,
        "factor_version": entry.spec.factor_version,
        "input_hash": result.input_hash,
        "input_rows": len(rows),
        "instruments": sorted({item.instrument_id for item in result.values}),
        "non_missing_values": sum(item.value is not None for item in result.values),
        "output_artifact_id": value_reference.artifact_id,
        "output_rows": len(result.values),
        "plugin_artifact_id": plugin_reference.artifact_id,
        "plugin_manifest_id": plugin_reference.manifest_id,
        "plugin_registry": "metadata.python_plugin_registry",
        "plugin_store": plugin_store.resolve().as_posix(),
        "plugin_hash": result.plugin_hash,
        "policy_version": result.policy_version,
        "runtime_version": result.runtime_version,
        "start": start.isoformat(),
        "end": end.isoformat(),
    }
    summary["summary_hash"] = content_hash(summary)
    (output / "summary.json").write_bytes(canonical_json_bytes(summary))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/m3_3_plugin_slice"))
    parser.add_argument("--plugin-store", type=Path, default=Path("data/factor_store/plugin_artifacts"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 3, 25))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 3, 29))
    args = parser.parse_args()
    print(
        json.dumps(
            run(args.database, args.output, args.plugin_store, args.start, args.end),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
