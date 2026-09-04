"""Build a content-addressed static Factor Evidence Explorer."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

import duckdb
from pydantic import Field

from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash
from alpha_research_os.kernel.specs import Digest, FrozenSpec, Identifier

GENERATOR_VERSION = "factor-evidence-explorer-1.0.0"
WEB_ROOT = Path(__file__).resolve().parent / "web"


class FactorExplorerConfig(FrozenSpec):
    """Pinned inputs for one reproducible Explorer snapshot."""

    schema_version: Literal["1"] = "1"
    report_name: Identifier
    title: str = "Alpha Research OS · Factor Evidence Explorer"
    database: str = "data/warehouse/alpha_research.duckdb"
    evidence_store: str = "data/evidence_store"
    output_root: str = "reports/factor_explorer"
    walk_forward_id: Digest
    redundancy_id: Digest
    robustness_id: Digest | None = None
    basic_evidence_ids: tuple[Digest, ...] = ()
    maximum_compare_entities: int = Field(default=6, ge=2, le=12)


def _sql_path(path: Path) -> str:
    return path.resolve().as_posix().replace("'", "''")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


def _rows(
    connection: duckdb.DuckDBPyConnection, query: str, parameters: list[Any] | None = None
) -> list[dict[str, Any]]:
    cursor = connection.execute(query, parameters or [])
    columns = [item[0] for item in cursor.description]
    return [
        {column: _json_value(value) for column, value in zip(columns, row, strict=True)} for row in cursor.fetchall()
    ]


def _one(
    connection: duckdb.DuckDBPyConnection,
    query: str,
    parameters: list[Any] | None = None,
) -> dict[str, Any]:
    rows = _rows(connection, query, parameters)
    if len(rows) != 1:
        raise ValueError(f"expected exactly one metadata row, received {len(rows)}")
    return rows[0]


def _asset_manifest(evidence_store: Path, category: str, asset_id: str) -> tuple[dict[str, Any], Path]:
    path = evidence_store / category / asset_id.removeprefix("sha256:") / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    identity_key = "walk_forward_id" if category == "walk_forward" else "redundancy_id"
    if payload.get(identity_key) != asset_id:
        raise ValueError(f"{category} manifest identity mismatch")
    return payload, path


def _artifact_path(evidence_store: Path, manifest: dict[str, Any], name: str) -> Path:
    match = [item for item in manifest["files"] if item["name"] == name]
    if len(match) != 1:
        raise ValueError(f"artifact must resolve exactly once: {name}")
    path = evidence_store / match[0]["relative_path"]
    if _sha256_file(path) != match[0]["artifact_hash"]:
        raise ValueError(f"artifact hash mismatch: {name}")
    return path


def _entity_id(variant: str, factor_id: str, factor_version: str) -> str:
    return f"{variant}|{factor_id}|{factor_version}"


def derive_routes(
    *,
    is_canonical: bool,
    fold_outcomes: list[str],
    sample_classification: str,
    integrity_blocked: bool = False,
    execution_available: bool = False,
) -> list[str]:
    """Derive non-exclusive display routes without making a promotion decision."""

    if integrity_blocked:
        return ["QUARANTINED_INTEGRITY_FAILURE"]
    routes = ["MODEL_FEATURE_ELIGIBLE"]
    if not is_canonical:
        routes.append("CANONICALIZED_REDUNDANT")
    if any(outcome == "DIRECTION_CONTRADICTED" for outcome in fold_outcomes):
        routes.append("DIRECTION_CONTRADICTED")
    if "EXPOSED" in sample_classification or "PSEUDO" in sample_classification:
        routes.append("REQUIRES_NEW_OOS")
    if not execution_available:
        routes.append("DIAGNOSTIC_ONLY")
    return routes


def _quality_rows(
    connection: duckdb.DuckDBPyConnection,
    factor_inputs: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in factor_inputs:
        release_id = item["release_id"]
        variant = item["variant"]
        if variant == "RAW":
            rows = _rows(
                connection,
                """SELECT factor_id,factor_version,coverage,present_count,row_count,minimum,maximum
                FROM metadata.factor_quality_summary WHERE release_id=?""",
                [release_id],
            )
        else:
            rows = _rows(
                connection,
                """SELECT factor_id,factor_version,coverage,present_count,row_count,
                cross_section_mean_abs_max,cross_section_std_error_max
                FROM metadata.processed_factor_quality_summary WHERE release_id=?""",
                [release_id],
            )
        for row in rows:
            row["release_id"] = release_id
            result[_entity_id(variant, row["factor_id"], row["factor_version"])] = row
    return result


def _basic_rows(
    connection: duckdb.DuckDBPyConnection,
    evidence_ids: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for evidence_id in evidence_ids:
        metadata = _one(
            connection,
            "SELECT request_json,label_release_id FROM metadata.evidence_bundle_manifest WHERE evidence_id=?",
            [evidence_id],
        )
        request = json.loads(metadata["request_json"])
        label_metadata = _one(
            connection,
            "SELECT request_json FROM metadata.label_release_manifest WHERE release_id=?",
            [metadata["label_release_id"]],
        )
        label_request = json.loads(label_metadata["request_json"])
        variant = request["factor_variant"]
        rows = _rows(
            connection,
            "SELECT * FROM research.factor_evidence_summary WHERE evidence_id=?",
            [evidence_id],
        )
        for row in rows:
            row["variant"] = variant
            row["window"] = {"start": label_request["start"], "end": label_request["end"]}
            row["constraint_level"] = label_request["constraint_level"]
            result[_entity_id(variant, row["factor_id"], row["factor_version"])] = row
    return result


def _robustness_inputs(metadata: dict[str, Any]) -> tuple[str, ...]:
    request = json.loads(metadata["request_json"])
    return tuple(item["evidence_id"] for item in request["evidence_inputs"])


def _group_by_entity(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[_entity_id(row["variant"], row["factor_id"], row["factor_version"])].append(row)
    return dict(grouped)


def _report_payload(
    connection: duckdb.DuckDBPyConnection,
    config: FactorExplorerConfig,
    evidence_store: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    walk_metadata = _one(
        connection,
        "SELECT * FROM metadata.walk_forward_evidence_manifest WHERE walk_forward_id=?",
        [config.walk_forward_id],
    )
    redundancy_metadata = _one(
        connection,
        "SELECT * FROM metadata.factor_redundancy_evidence_manifest WHERE redundancy_id=?",
        [config.redundancy_id],
    )
    if redundancy_metadata["source_walk_forward_id"] != config.walk_forward_id:
        raise ValueError("redundancy evidence is not derived from the configured walk-forward asset")
    walk_request = json.loads(walk_metadata["request_json"])
    redundancy_request = json.loads(redundancy_metadata["request_json"])
    label_metadata = _one(
        connection,
        "SELECT manifest_hash,request_json FROM metadata.label_release_manifest WHERE release_id=?",
        [walk_request["label_release_id"]],
    )
    label_request = json.loads(label_metadata["request_json"])
    walk_manifest, _ = _asset_manifest(evidence_store, "walk_forward", config.walk_forward_id)
    redundancy_manifest, _ = _asset_manifest(evidence_store, "redundancy", config.redundancy_id)

    source_manifests = [
        {
            "kind": "LABEL",
            "asset_id": walk_request["label_release_id"],
            "manifest_hash": label_metadata["manifest_hash"],
        },
        {
            "kind": "WALK_FORWARD",
            "asset_id": config.walk_forward_id,
            "manifest_hash": walk_metadata["manifest_hash"],
        },
        {
            "kind": "REDUNDANCY",
            "asset_id": config.redundancy_id,
            "manifest_hash": redundancy_metadata["manifest_hash"],
        },
    ]
    robustness_metadata: dict[str, Any] | None = None
    evidence_ids = config.basic_evidence_ids
    if config.robustness_id is not None:
        robustness_metadata = _one(
            connection,
            "SELECT * FROM metadata.robustness_evidence_manifest WHERE robustness_id=?",
            [config.robustness_id],
        )
        source_manifests.append(
            {
                "kind": "ROBUSTNESS",
                "asset_id": config.robustness_id,
                "manifest_hash": robustness_metadata["manifest_hash"],
            }
        )
        if not evidence_ids:
            evidence_ids = _robustness_inputs(robustness_metadata)
    for evidence_id in evidence_ids:
        metadata = _one(
            connection,
            "SELECT manifest_hash FROM metadata.evidence_bundle_manifest WHERE evidence_id=?",
            [evidence_id],
        )
        source_manifests.append(
            {"kind": "BASIC_EVIDENCE", "asset_id": evidence_id, "manifest_hash": metadata["manifest_hash"]}
        )
    source_manifests.sort(key=lambda item: (item["kind"], item["asset_id"]))
    semantic_config = config.model_dump(mode="json")
    for path_field in ("database", "evidence_store", "output_root"):
        semantic_config.pop(path_field)
    request = {
        "schema_version": "1",
        "generator_version": GENERATOR_VERSION,
        "config": semantic_config,
        "source_manifests": source_manifests,
    }
    report_id = content_hash(request)

    registry = {}
    for row in _rows(connection, "SELECT * FROM metadata.factor_registry"):
        specification = json.loads(row.pop("spec_json"))
        registry[(row["factor_id"], row["factor_version"])] = {**row, "spec": specification}
    quality = _quality_rows(connection, walk_request["factor_inputs"])
    basic = _basic_rows(connection, evidence_ids)
    robustness = (
        {
            _entity_id(row["variant"], row["factor_id"], row["factor_version"]): row
            for row in _rows(
                connection,
                "SELECT * FROM research.factor_robustness_summary WHERE robustness_id=?",
                [config.robustness_id],
            )
        }
        if config.robustness_id is not None
        else {}
    )
    folds = _group_by_entity(
        _rows(
            connection,
            """SELECT * FROM research.factor_walk_forward_decisions
            WHERE walk_forward_id=? ORDER BY variant,factor_id,factor_version,fold_id""",
            [config.walk_forward_id],
        )
    )
    regimes = _group_by_entity(
        _rows(
            connection,
            """SELECT * FROM raw.factor_regime_statistics
            WHERE walk_forward_id=? ORDER BY variant,factor_id,factor_version,fold_id,regime_dimension,regime""",
            [config.walk_forward_id],
        )
    )

    artifact_paths = {
        name: _artifact_path(evidence_store, redundancy_manifest, name)
        for name in ("variant_deduplication", "factor_clusters", "incremental_value_summary", "pair_correlations")
    }
    dedup = {
        row["entity_id"]: row
        for row in _rows(
            connection,
            f"SELECT * FROM read_parquet('{_sql_path(artifact_paths['variant_deduplication'])}')",
        )
    }
    clusters = {
        row["entity_id"]: row
        for row in _rows(
            connection,
            f"SELECT * FROM read_parquet('{_sql_path(artifact_paths['factor_clusters'])}')",
        )
    }
    incremental = {
        row["candidate_entity_id"]: row
        for row in _rows(
            connection,
            f"SELECT * FROM read_parquet('{_sql_path(artifact_paths['incremental_value_summary'])}')",
        )
    }
    correlations = _rows(
        connection,
        f"""SELECT left_entity_id,right_entity_id,mean_daily_spearman_value_correlation,
        daily_rank_ic_correlation,value_sample_session_count,daily_rank_ic_pair_count
        FROM read_parquet('{_sql_path(artifact_paths["pair_correlations"])}')
        ORDER BY left_entity_id,right_entity_id""",
    )

    factors = []
    for entity_id in sorted(folds):
        entity_folds = folds[entity_id]
        first = entity_folds[0]
        key = (first["factor_id"], first["factor_version"])
        registry_row = registry.get(key, {})
        specification = registry_row.get("spec", {})
        dedup_row = dedup.get(entity_id, {})
        cluster_row = clusters.get(dedup_row.get("canonical_entity_id", entity_id), clusters.get(entity_id, {}))
        outcomes = sorted(
            {
                outcome
                for row in entity_folds
                for outcome in (row.get("hac_direction_outcome"), row.get("bootstrap_direction_outcome"))
                if outcome
            }
        )
        routes = derive_routes(
            is_canonical=bool(dedup_row.get("is_canonical", True)),
            fold_outcomes=outcomes,
            sample_classification=redundancy_metadata["sample_classification"],
        )
        factors.append(
            {
                "entity_id": entity_id,
                "factor_id": first["factor_id"],
                "factor_version": first["factor_version"],
                "variant": first["variant"],
                "name": specification.get("name", first["factor_id"]),
                "family": registry_row.get("family", "UNKNOWN"),
                "source_id": registry_row.get("source_id"),
                "lifecycle": registry_row.get("lifecycle"),
                "direction": specification.get("direction"),
                "economic_hypothesis": specification.get("economic_hypothesis"),
                "expected_mechanism": specification.get("expected_mechanism"),
                "formula": (specification.get("expression") or {}).get("formula"),
                "implementation_type": specification.get("implementation_type"),
                "implementation_hash": specification.get("implementation_hash"),
                "quality": quality.get(entity_id),
                "basic_evidence": basic.get(entity_id),
                "robustness": robustness.get(entity_id),
                "folds": entity_folds,
                "regimes": regimes.get(entity_id, []),
                "deduplication": dedup_row,
                "cluster": cluster_row,
                "incremental": incremental.get(entity_id),
                "canonical_incremental": incremental.get(dedup_row.get("canonical_entity_id", entity_id)),
                "routes": routes,
                "execution": {"status": "NOT_AVAILABLE", "reason": "M4.6_NOT_PUBLISHED"},
                "model_contribution": {"status": "NOT_AVAILABLE", "reason": "M6_NOT_PUBLISHED"},
            }
        )

    cluster_groups: dict[str, dict[str, Any]] = {}
    for row in clusters.values():
        item = cluster_groups.setdefault(
            row["cluster_id"],
            {
                "cluster_id": row["cluster_id"],
                "representative_entity_id": row["representative_entity_id"],
                "members": [],
            },
        )
        item["members"].append(
            {
                "entity_id": row["entity_id"],
                "mean_distance": row["mean_distance_to_cluster_members"],
                "mean_coverage": row["mean_daily_coverage"],
                "is_representative": row["is_representative"],
            }
        )
    for item in cluster_groups.values():
        item["members"].sort(key=lambda member: (not member["is_representative"], member["entity_id"]))

    summary = {
        "entity_count": len(factors),
        "factor_count": len({item["factor_id"] for item in factors}),
        "canonical_count": sum(bool(item["deduplication"].get("is_canonical", True)) for item in factors),
        "cluster_count": len(cluster_groups),
        "integrity_blocker_count": sum("QUARANTINED_INTEGRITY_FAILURE" in item["routes"] for item in factors),
        "execution_available_count": sum(item["execution"]["status"] == "AVAILABLE" for item in factors),
    }
    label_horizons = sorted({item["label_horizon_sessions"] for item in walk_request["evaluation"]["folds"]})
    if len(label_horizons) != 1:
        raise ValueError("Explorer requires one label horizon across all walk-forward folds")
    payload = {
        "schema_version": "1",
        "report": {
            "report_id": report_id,
            "report_name": config.report_name,
            "title": config.title,
            "generator_version": GENERATOR_VERSION,
            "generated_at": datetime.now().astimezone().isoformat(),
            "maximum_compare_entities": config.maximum_compare_entities,
            "sample_classification": redundancy_metadata["sample_classification"],
            "window": redundancy_request["window"],
            "universe_id": label_request["universe_id"],
            "universe_version": label_request["universe_version"],
            "constraint_level": label_request["constraint_level"],
            "label_id": label_request["label_id"],
            "label_version": label_request["label_version"],
            "label_horizon_sessions": label_horizons[0],
            "label_release_id": walk_request["label_release_id"],
            "walk_forward_id": config.walk_forward_id,
            "redundancy_id": config.redundancy_id,
            "robustness_id": config.robustness_id,
            "limitations": sorted(set(walk_manifest["limitations"] + redundancy_manifest["limitations"])),
        },
        "summary": summary,
        "factors": factors,
        "clusters": sorted(cluster_groups.values(), key=lambda item: item["cluster_id"]),
        "correlations": correlations,
    }
    return request, payload


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _verify_cached(directory: Path, request: dict[str, Any]) -> dict[str, Any] | None:
    manifest_path = directory / "manifest.json"
    if not manifest_path.exists():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("request") != request:
        raise ValueError("cached Explorer request differs from its content-addressed directory")
    for item in manifest["files"]:
        if _sha256_file(directory / item["path"]) != item["sha256"]:
            raise ValueError(f"cached Explorer file hash mismatch: {item['path']}")
    return manifest


def build_factor_explorer(config: FactorExplorerConfig, project_root: Path) -> dict[str, Any]:
    """Build or verify one immutable static report and update a convenience pointer."""

    database = project_root / config.database if not Path(config.database).is_absolute() else Path(config.database)
    evidence_store = (
        project_root / config.evidence_store
        if not Path(config.evidence_store).is_absolute()
        else Path(config.evidence_store)
    )
    output_root = (
        project_root / config.output_root if not Path(config.output_root).is_absolute() else Path(config.output_root)
    )
    with duckdb.connect(str(database), read_only=True) as connection:
        request, payload = _report_payload(connection, config, evidence_store)
    report_id = payload["report"]["report_id"]
    directory = output_root / report_id.removeprefix("sha256:")
    cached = _verify_cached(directory, request)
    if cached is not None:
        result = {
            "cache_hit": True,
            "report_id": report_id,
            "index": str((directory / "index.html").resolve()),
            "manifest": str((directory / "manifest.json").resolve()),
            "summary": payload["summary"],
        }
        latest = {
            "report_id": report_id,
            "index": result["index"],
            "manifest": result["manifest"],
            "updated_at": datetime.now().astimezone().isoformat(),
        }
        _atomic_write(output_root / "latest.json", canonical_json_bytes(latest) + b"\n")
        return result

    temporary = output_root / f".{report_id.removeprefix('sha256:')}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(parents=True, exist_ok=False)
    try:
        shutil.copy2(WEB_ROOT / "index.html", temporary / "index.html")
        shutil.copy2(WEB_ROOT / "app.css", temporary / "app.css")
        shutil.copy2(WEB_ROOT / "app.js", temporary / "app.js")
        data_bytes = canonical_json_bytes(payload)
        (temporary / "evidence-summary.json").write_bytes(data_bytes + b"\n")
        escaped = data_bytes.decode("utf-8").replace("</", "<\\/")
        (temporary / "data.js").write_text(f"window.__EXPLORER_DATA__={escaped};\n", encoding="utf-8")
        files = []
        for path in sorted(temporary.iterdir(), key=lambda item: item.name):
            if path.name == "manifest.json":
                continue
            files.append({"path": path.name, "sha256": _sha256_file(path), "size_bytes": path.stat().st_size})
        manifest = {
            "schema_version": "1",
            "report_id": report_id,
            "created_at": payload["report"]["generated_at"],
            "request": request,
            "summary": payload["summary"],
            "files": files,
            "quality_status": "PASS",
            "decision_status": "READ_ONLY_DERIVED_REPORT_NOT_EVIDENCE",
        }
        (temporary / "manifest.json").write_bytes(canonical_json_bytes(manifest) + b"\n")
        output_root.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, directory)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    latest = {
        "report_id": report_id,
        "index": str((directory / "index.html").resolve()),
        "manifest": str((directory / "manifest.json").resolve()),
        "updated_at": datetime.now().astimezone().isoformat(),
    }
    _atomic_write(output_root / "latest.json", canonical_json_bytes(latest) + b"\n")
    return {
        "cache_hit": False,
        "report_id": report_id,
        "index": latest["index"],
        "manifest": latest["manifest"],
        "summary": payload["summary"],
    }
