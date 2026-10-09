"""Publish one immutable bank-masked factor via the existing factor asset protocol."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import date, datetime
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for import_root in (PROJECT_ROOT, PROJECT_ROOT / "src"):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.factors.assets import (  # noqa: E402
    DatasetLineage,
    FactorAssetRef,
    FactorAssetRequest,
    FactorReleaseManifest,
)
from alpha_research_os.factors.bank import ENGINE_VERSION, bank_catalog, bank_factor_catalog  # noqa: E402
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash  # noqa: E402
from scripts.bank_factor_inputs import build_features, source_inputs  # noqa: E402
from scripts.publish_factor_release import (  # noqa: E402
    _atomic_write,
    _lineage,
    _quality,
    _register,
    _sha256_file,
    _sql_path,
)


def prepare_inputs(root: Path, start: date, end: date):
    facts, events, market, membership, capitals, coverage = source_inputs(root, start, end)
    frames = {
        "facts": facts,
        "dividend_events": events,
        "bank_market": market,
        "membership": membership,
        "capital_reference_prices": capitals,
    }
    hashes = {
        name: content_hash(frame.to_json(orient="split", date_format="iso", default_handler=str))
        for name, frame in frames.items()
    }
    hashes["dividend_coverage"] = content_hash(sorted(coverage))
    hashes["engine_source"] = content_hash(
        {
            path.name: _sha256_file(path)
            for path in (
                root / "scripts/bank_factor_inputs.py",
                root / "src/alpha_research_os/factors/bank.py",
                root / "scripts/publish_bank_factor.py",
            )
        }
    )
    key = content_hash(hashes)
    folder = root / "data/factor_store/bank_inputs" / key.removeprefix("sha256:")
    if (folder / "input_manifest.json").exists():
        manifest = json.loads((folder / "input_manifest.json").read_bytes())
        for name, digest in manifest["files"].items():
            if _sha256_file(folder / name) != digest:
                raise ValueError(f"immutable bank input hash mismatch: {name}")
        return folder, manifest
    folder.mkdir(parents=True, exist_ok=True)
    for name, frame in frames.items():
        frame.to_parquet(folder / f"{name}.parquet", index=False)
    features = build_features(facts, events, market, capitals, coverage)
    if features.empty:
        raise ValueError("no eligible historical bank sessions in selected window")
    features.to_parquet(folder / "features.parquet", index=False)
    manifest = {
        "input_key": key,
        "source_hashes": hashes,
        "files": {p.name: _sha256_file(p) for p in folder.glob("*.parquet")},
        "bank_count": int(market.instrument_id.nunique()),
        "dividend_coverage": sorted(coverage),
        "row_count": len(market),
        "start": str(start),
        "end": str(end),
        "feature_label_access": False,
        "source_priority": "Tushare>original issuer>free, only among comparable and currently available facts",
        "scope": "PIT listed/eligible sessions intersect known SW bank membership; current42 source cohort",
        "historical_status": (
            "RESEARCH_ONLY; original report reconciled; exhaustive version/industry certification incomplete"
        ),
        "unverified_revision_policy": "collection availability; never announcement-date backfill",
        "per_share_policy": "known gifts normalized; non-free reference adjustments suspend old report anchors",
        "capital_reference_limit": (
            "reference-price checks cannot certify every private issuance without ex-right adjustment"
        ),
    }
    _atomic_write(folder / "input_manifest.json", canonical_json_bytes(manifest))
    return folder, manifest


def materialization_sql(item, spec, request, input_folder: Path, target: Path):
    return f"""COPY (SELECT '{request.computation_key}' AS release_id,
      session,instrument_id,'{item.factor_id}' AS factor_id,'{item.factor_version}' AS factor_version,
      'RAW' AS variant, CASE WHEN isfinite({item.field}) THEN {item.field}::DOUBLE END AS value,
      available_at,'{spec.implementation_hash}' AS implementation_hash
      FROM read_parquet('{_sql_path(input_folder / "features.parquet")}')
      ORDER BY session,instrument_id) TO '{_sql_path(target)}' (FORMAT PARQUET, COMPRESSION ZSTD)"""


def verify_bank_values(folder: Path, target: Path, field: str):
    """Exact masked keys plus fresh serial first/last-session witnesses per bank/year."""
    actual = pd.read_parquet(target)
    market = pd.read_parquet(folder / "bank_market.parquet")
    keys = ["session", "instrument_id"]
    if set(map(tuple, actual[keys].itertuples(index=False, name=None))) != set(
        map(tuple, market[keys].itertuples(index=False, name=None))
    ):
        raise ValueError("bank factor differs from eligible bank keys")
    market["year"] = market.session.map(lambda d: d.year)
    grouped = market.groupby(["instrument_id", "year"], sort=True)
    witnesses = pd.concat([grouped.head(1), grouped.tail(1)]).drop_duplicates(keys).drop(columns="year")
    coverage = set(json.loads((folder / "input_manifest.json").read_bytes())["dividend_coverage"])
    reference = build_features(
        pd.read_parquet(folder / "facts.parquet"),
        pd.read_parquet(folder / "dividend_events.parquet"),
        witnesses,
        pd.read_parquet(folder / "capital_reference_prices.parquet"),
        coverage,
    )[keys + [field]]
    paired = reference.merge(actual[keys + ["value"]], on=keys, how="left", validate="one_to_one")
    if not paired[field].isna().equals(paired.value.isna()):
        raise ValueError("bank serial reference missing-mask mismatch")
    differences = (paired[field] - paired.value).abs()
    if (differences > 1e-12 * paired[field].abs().clip(lower=1)).any():
        raise ValueError("bank serial reference numeric mismatch")
    return {
        "status": "PASS",
        "scope": "bank keys and serial arithmetic; not historical source certification",
        "expected_key_count": len(market),
        "serial_reference_sample_count": len(paired),
        "serial_reference_value_difference_count": 0,
        "serial_reference_missing_difference_count": 0,
    }


def publish(root: Path, start: date, end: date, factor_id: str):
    if end < start:
        raise ValueError("end must not precede start")
    catalog = bank_catalog(factor_id)
    entry = catalog.list()[0]
    item = next(x for x in bank_factor_catalog() if x.factor_id == factor_id)
    database = root / "data/warehouse/alpha_research.duckdb"
    with duckdb.connect(str(database), read_only=True) as c:
        lower, upper = c.execute("SELECT min(trade_date),max(trade_date) FROM research.market_daily").fetchone()
        if start < lower or end > upper:
            raise ValueError(f"window outside market coverage {lower}..{upper}")
        lineage = list(_lineage(c))
    folder, inputs = prepare_inputs(root, start, end)
    lineage.append(DatasetLineage(manifest_table="bank-feature-inputs-v1", checkpoint_hashes=(inputs["input_key"],)))
    request = FactorAssetRequest(
        engine_version=ENGINE_VERSION,
        factors=(
            FactorAssetRef(
                factor_id=item.factor_id,
                factor_version=item.factor_version,
                spec_hash=entry.spec_hash,
                implementation_hash=entry.entry.spec.implementation_hash,
                catalog_entry_hash=entry.entry_hash,
            ),
        ),
        dataset_lineage=tuple(sorted(lineage, key=lambda x: x.manifest_table)),
        universe_id="ALL-A-PIT",
        universe_version="bank-masked-" + inputs["source_hashes"]["membership"].removeprefix("sha256:")[:16],
        start=start,
        end=end,
        signal_clock_version="cn-close-postclose-v1",
    )
    store = root / "data/factor_store"
    release = store / "releases" / request.computation_key.removeprefix("sha256:")
    release.mkdir(parents=True, exist_ok=True)
    manifest_path = release / "manifest.json"
    parquet = release / "raw_factor_values.parquet"
    if manifest_path.exists():
        manifest = FactorReleaseManifest.model_validate_json(manifest_path.read_bytes())
        if manifest.request != request or _sha256_file(parquet) != manifest.parquet_hash:
            raise ValueError("cached bank release identity mismatch")
        quality = json.loads((release / "quality_summary.json").read_bytes())
        _register(database, store, manifest, content_hash(manifest), catalog, quality["factors"], "bank")
        calculation = json.loads((release / "calculation_summary.json").read_bytes())
        return {
            "cache_hit": True,
            "release_id": manifest.release_id,
            "manifest": str(manifest_path),
            "calculation": calculation,
            "accuracy_status": "PASS",
        }
    target = release / f".bank-values.{uuid.uuid4().hex}.tmp.parquet"
    with duckdb.connect() as c:
        c.execute(materialization_sql(item, entry.entry.spec, request, folder, target))
        keys = c.execute(f"""SELECT count(*)-count(DISTINCT(session,instrument_id))
                           FROM read_parquet('{_sql_path(target)}')""").fetchone()[0]
        row_count = c.execute(f"SELECT count(*) FROM read_parquet('{_sql_path(target)}')").fetchone()[0]
        if keys or row_count != inputs["row_count"]:
            raise ValueError("bank factor universe-key verification failed")
    quality, details = _quality(target, 1)
    verification = verify_bank_values(folder, target, item.field)
    target.replace(parquet)
    quality["accuracy_gate"] = verification
    _atomic_write(release / "accuracy_verification.json", canonical_json_bytes(verification))
    _atomic_write(release / "quality_summary.json", canonical_json_bytes(quality))
    _atomic_write(release / "bank_input_manifest.json", canonical_json_bytes(inputs))
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
        quality_summary_hash=_sha256_file(release / "quality_summary.json"),
    )
    _atomic_write(manifest_path, canonical_json_bytes(manifest))
    _atomic_write(
        release / "calculation_summary.json",
        canonical_json_bytes(
            {
                "mode": "FULL_IMMUTABLE_BANK_INPUT",
                "message": "银行范围按已公开字段计算；缺失保留，来源证据另存。",
                "input_folder": str(folder),
                "bank_scope": inputs["scope"],
                "historical_status": inputs["historical_status"],
            }
        ),
    )
    _register(database, store, manifest, content_hash(manifest), catalog, details, "bank")
    return {
        "cache_hit": False,
        "release_id": manifest.release_id,
        "manifest": str(manifest_path),
        "row_count": manifest.row_count,
        "bank_count": inputs["bank_count"],
        "coverage": details[0]["coverage"],
        "accuracy_status": "PASS",
        "calculation": json.loads((release / "calculation_summary.json").read_bytes()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factor-id", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--result", type=Path)
    args = parser.parse_args()
    result = publish(PROJECT_ROOT, args.start, args.end, args.factor_id)
    if args.result:
        _atomic_write(args.result, canonical_json_bytes(result))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
