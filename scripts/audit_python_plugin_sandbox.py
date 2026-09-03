"""Audit the frozen M3.3 plugin, registry, artifacts, and isolated replay."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import duckdb

from alpha_research_os.factors import FactorCatalog
from alpha_research_os.factors._plugin_worker import PluginSourceError, validate_plugin_source
from alpha_research_os.factors.plugin import PythonPluginRuntime
from alpha_research_os.factors.plugin_library import conditional_close_location_plugin
from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash, sha256_bytes
from alpha_research_os.kernel.specs import DataDomain
from scripts.run_m3_3_plugin_slice import load_rows


def _object_path(root: Path, artifact_id: str) -> Path:
    digest = artifact_id.removeprefix("sha256:")
    return root / "objects" / "sha256" / digest[:2] / digest


def _manifest_path(root: Path, manifest_id: str) -> Path:
    digest = manifest_id.removeprefix("sha256:")
    return root / "manifests" / "sha256" / digest[:2] / f"{digest}.json"


def audit(database: Path, summary_path: Path, start: date, end: date) -> dict[str, object]:
    summary = json.loads(summary_path.read_bytes())
    failures: list[str] = []
    declared_summary_hash = summary.pop("summary_hash")
    if content_hash(summary) != declared_summary_hash:
        failures.append("vertical-slice summary hash mismatch")
    plugin_store = Path(summary["plugin_store"])
    source_path = _object_path(plugin_store, summary["plugin_artifact_id"])
    manifest_path = _manifest_path(plugin_store, summary["plugin_manifest_id"])
    if not source_path.exists() or sha256_bytes(source_path.read_bytes()) != summary["plugin_artifact_id"]:
        failures.append("plugin source artifact is missing or hash-invalid")
    if not manifest_path.exists() or sha256_bytes(manifest_path.read_bytes()) != summary["plugin_manifest_id"]:
        failures.append("plugin artifact manifest is missing or hash-invalid")

    entry, plugin = conditional_close_location_plugin()
    if source_path.exists() and source_path.read_text(encoding="utf-8") != plugin.source:
        failures.append("published plugin source differs from the governed library")
    if plugin.implementation_hash != summary["plugin_hash"]:
        failures.append("plugin implementation hash differs from the slice summary")
    catalog = FactorCatalog()
    cataloged, registered = catalog.register(entry)
    if cataloged.entry_hash != summary["catalog_entry_hash"]:
        failures.append("catalog entry hash differs from the slice summary")

    rows = load_rows(database, start, end)
    replay = PythonPluginRuntime({"close": DataDomain.MARKET, "high": DataDomain.MARKET, "low": DataDomain.MARKET}).run(
        registered, plugin, rows
    )
    replay_payload = canonical_json_bytes([item.model_dump(mode="json") for item in replay.values])
    replay_artifact_id = sha256_bytes(replay_payload)
    if replay.input_hash != summary["input_hash"]:
        failures.append("isolated replay input hash differs from the frozen slice")
    if replay_artifact_id != summary["output_artifact_id"]:
        failures.append("isolated replay output differs from the frozen artifact")

    with duckdb.connect(str(database), read_only=True) as connection:
        plugin_row = connection.execute(
            """SELECT implementation_hash, entrypoint, sandbox_policy, source_artifact_id, source_manifest_id
            FROM metadata.python_plugin_registry WHERE plugin_id=? AND plugin_version=?""",
            [plugin.plugin_id, plugin.plugin_version],
        ).fetchone()
        factor_row = connection.execute(
            """SELECT spec_hash, implementation_hash, catalog_entry_hash, lifecycle, spec_json
            FROM metadata.factor_registry WHERE factor_id=? AND factor_version=?""",
            [entry.spec.factor_id, entry.spec.factor_version],
        ).fetchone()
    expected_plugin_row = (
        plugin.implementation_hash,
        plugin.entrypoint_ref,
        replay.policy_version,
        summary["plugin_artifact_id"],
        summary["plugin_manifest_id"],
    )
    if plugin_row != expected_plugin_row:
        failures.append("DuckDB Python plugin registry differs from immutable artifacts")
    if factor_row is None or factor_row[:4] != (
        cataloged.spec_hash,
        plugin.implementation_hash,
        cataloged.entry_hash,
        "CANDIDATE",
    ):
        failures.append("DuckDB factor registry differs from the governed plugin entry")
    elif {item.value for item in entry.spec.data_domains}.intersection({"label", "holdout"}):
        failures.append("registered plugin declares a privileged data domain")

    attacks = (
        "import os\ndef factor():\n    return 1\n",
        'def factor():\n    return open("secret.txt")\n',
        "def factor():\n    return ().__class__\n",
        'def factor():\n    return history("close", 1)[0]\n',
        "def factor():\n    return TUSHARE_TOKEN\n",
        "def factor():\n    while True:\n        pass\n",
        "def factor(value):\n    return value\n",
    )
    rejected_attacks = 0
    for source in attacks:
        try:
            validate_plugin_source(
                source,
                declared_fields=("close", "high", "low"),
                max_ast_nodes=512,
                max_lookback=1,
            )
        except PluginSourceError:
            rejected_attacks += 1
    if rejected_attacks != len(attacks):
        failures.append("one or more sandbox escape probes were accepted")

    return {
        "audited_at": datetime.now().astimezone().isoformat(),
        "catalog_entry_hash": cataloged.entry_hash,
        "factor_id": entry.spec.factor_id,
        "failures": failures,
        "input_rows": len(rows),
        "output_rows": len(replay.values),
        "plugin_artifact_id": summary["plugin_artifact_id"],
        "plugin_hash": plugin.implementation_hash,
        "policy_version": replay.policy_version,
        "rejected_escape_probes": rejected_attacks,
        "replay_artifact_id": replay_artifact_id,
        "status": "PASS" if not failures else "FAIL",
        "summary_hash": declared_summary_hash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("data/warehouse/alpha_research.duckdb"))
    parser.add_argument("--summary", type=Path, default=Path("artifacts/m3_3_plugin_slice/summary.json"))
    parser.add_argument("--start", type=date.fromisoformat, default=date(2024, 3, 25))
    parser.add_argument("--end", type=date.fromisoformat, default=date(2024, 3, 29))
    parser.add_argument("--output", type=Path, default=Path("reports/m3_3_plugin_sandbox_audit.json"))
    args = parser.parse_args()
    report = audit(args.database, args.summary, args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
