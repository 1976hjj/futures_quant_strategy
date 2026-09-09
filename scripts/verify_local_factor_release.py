"""Verify a published local factor candidate without blocking its initial use."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from alpha_research_os.factors.alpha158 import alpha158_catalog  # noqa: E402
from alpha_research_os.factors.assets import FactorReleaseManifest  # noqa: E402
from alpha_research_os.factors.jqdata import jqdata_catalog  # noqa: E402
from scripts.factor_compute_runtime import (  # noqa: E402
    ACCURACY_STATUS_FILE,
    _write_status,
    reference_window,
    validate_local_factor_output,
)
from scripts.publish_alpha158_factor import _materialization_sql as _alpha_sql  # noqa: E402
from scripts.publish_factor_release import _configure_bounded_connection, _warmup_start  # noqa: E402
from scripts.publish_jqdata_factor import LOCAL_FORMULA_FACTORS, _local_materialization_sql  # noqa: E402


def verify(database: Path, store: Path, release_id: str, factor_id: str) -> dict:
    release_dir = store / "releases" / release_id.removeprefix("sha256:")
    status_path = release_dir / ACCURACY_STATUS_FILE
    reference = release_dir / f".accuracy-reference.{uuid.uuid4().hex}.tmp.parquet"
    try:
        manifest = FactorReleaseManifest.model_validate_json((release_dir / "manifest.json").read_bytes())
        request = manifest.request
        target = store / manifest.parquet_relative_path
        with duckdb.connect(str(database), read_only=True) as connection:
            lower, upper = reference_window(connection, request.start, request.end)
        reference_request = request.model_copy(update={"start": lower, "end": upper})
        alpha_items = {item.factor_id: item for item in alpha158_catalog()}
        jq_items = {item.factor_id: item for item in jqdata_catalog()}
        if factor_id in alpha_items:
            item = alpha_items[factor_id]
            from scripts.publish_alpha158_factor import _catalog as alpha_catalog

            warmup_sessions = alpha_catalog(item).list()[0].entry.spec.warmup_sessions
            with duckdb.connect(str(database), read_only=True) as connection:
                warmup = _warmup_start(connection, lower, warmup_sessions)
                _configure_bounded_connection(connection, store / "duckdb_tmp")
                connection.execute(_alpha_sql(item, reference_request, reference, warmup))
        elif factor_id in jq_items and jq_items[factor_id].external_name in LOCAL_FORMULA_FACTORS:
            item = jq_items[factor_id]
            from scripts.publish_jqdata_factor import _catalog as jq_catalog

            warmup_sessions = jq_catalog(item).list()[0].entry.spec.warmup_sessions
            with duckdb.connect(str(database), read_only=True) as connection:
                warmup = _warmup_start(connection, lower, warmup_sessions)
                _configure_bounded_connection(connection, store / "duckdb_tmp")
                connection.execute(_local_materialization_sql(item, reference_request, reference, warmup))
        else:
            raise ValueError(f"factor is not a locally computed factor: {factor_id}")
        result = validate_local_factor_output(database, target, reference, request.start, request.end)
        payload = {
            **result,
            "factor_id": factor_id,
            "release_id": release_id,
            "completed_at": datetime.now().astimezone().isoformat(),
        }
        _write_status(status_path, payload)
        print(json.dumps(payload, ensure_ascii=False))
        return payload
    except Exception as error:
        payload = {
            "status": "FAIL",
            "factor_id": factor_id,
            "release_id": release_id,
            "error": str(error),
            "completed_at": datetime.now().astimezone().isoformat(),
        }
        _write_status(status_path, payload)
        raise
    finally:
        reference.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--factor-id", required=True)
    args = parser.parse_args()
    verify(args.database, args.store, args.release_id, args.factor_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
