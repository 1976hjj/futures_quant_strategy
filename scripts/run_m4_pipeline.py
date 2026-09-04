"""Run a configuration-driven, resumable M4 evidence pipeline and persist one report."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
for import_root in (PROJECT_ROOT, SRC_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from audit_factor_explorer import audit as audit_factor_explorer  # noqa: E402
from audit_m4_1_evidence import audit as audit_basic_evidence  # noqa: E402
from audit_m4_3_robustness import audit as audit_robustness  # noqa: E402
from audit_m4_4_walk_forward import audit as audit_walk_forward  # noqa: E402
from audit_m4_5_redundancy import audit as audit_redundancy  # noqa: E402
from publish_processed_factor_release import publish as publish_processed  # noqa: E402
from run_m4_1_evidence import run as run_basic_evidence  # noqa: E402
from run_m4_3_robustness import publish as publish_robustness  # noqa: E402
from run_m4_4_walk_forward import publish as publish_walk_forward  # noqa: E402
from run_m4_5_redundancy import publish as publish_redundancy  # noqa: E402

from alpha_research_os.evaluation import WalkForwardFoldSpec  # noqa: E402
from alpha_research_os.kernel.canonical import canonical_json_bytes  # noqa: E402
from alpha_research_os.kernel.specs import DateRange  # noqa: E402
from alpha_research_os.orchestration import M4PipelineConfig  # noqa: E402
from alpha_research_os.reporting import FactorExplorerConfig, build_factor_explorer  # noqa: E402

STAGE_ORDER = (
    "processed",
    "basic_evidence",
    "audit_basic_evidence",
    "robustness",
    "audit_robustness",
    "walk_forward",
    "redundancy",
    "audit_walk_forward",
    "audit_redundancy",
    "factor_explorer",
    "audit_factor_explorer",
)


def _resolve(path: str) -> Path:
    candidate = Path(path)
    return candidate if candidate.is_absolute() else PROJECT_ROOT / candidate


def _atomic_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_bytes(canonical_json_bytes(payload) + b"\n")
    os.replace(temporary, path)


def _load_config(path: Path) -> M4PipelineConfig:
    return M4PipelineConfig.model_validate_json(path.read_bytes())


def _factor_manifest_path(factor_store: Path, release_id: str) -> Path:
    digest = release_id.removeprefix("sha256:")
    raw = factor_store / "releases" / digest / "manifest.json"
    processed = factor_store / "processed_releases" / digest / "manifest.json"
    if raw.exists() == processed.exists():
        raise ValueError(f"factor release must resolve exactly once: {release_id}")
    return raw if raw.exists() else processed


def preflight(config: M4PipelineConfig) -> dict[str, Any]:
    database = _resolve(config.paths.database)
    factor_store = _resolve(config.paths.factor_store)
    evidence_store = _resolve(config.paths.evidence_store)
    if not database.exists():
        raise FileNotFoundError(database)
    raw_manifest_path = _factor_manifest_path(factor_store, config.raw_factor_release_id)
    raw_manifest = json.loads(raw_manifest_path.read_text(encoding="utf-8"))
    if raw_manifest["request"]["variant"] != "RAW":
        raise ValueError("raw_factor_release_id does not point to a RAW release")
    for release_id in config.processed_factor_release_ids:
        manifest = json.loads(_factor_manifest_path(factor_store, release_id).read_text(encoding="utf-8"))
        if manifest["request"]["variant"] == "RAW":
            raise ValueError(f"processed release points to RAW: {release_id}")
    variant_count = 1 + max(len(config.processed_factor_release_ids), len(config.processed_variants))
    estimated_entities = int(raw_manifest["factor_count"]) * variant_count
    warnings = []
    if estimated_entities > 300:
        warnings.append(
            "M4.5 pair correlation is O(entity_count^2); use CLUSTER_REPRESENTATIVES_AND_FOCUS "
            "for conditional evidence and plan compute/storage capacity."
        )
    return {
        "database": str(database),
        "factor_store": str(factor_store),
        "evidence_store": str(evidence_store),
        "raw_factor_count": int(raw_manifest["factor_count"]),
        "estimated_factor_variant_entities": estimated_entities,
        "estimated_pair_correlations": estimated_entities * (estimated_entities - 1) // 2,
        "warnings": warnings,
    }


def _folds(config: M4PipelineConfig) -> tuple[WalkForwardFoldSpec, ...]:
    if config.walk_forward is None:
        raise ValueError("walk_forward configuration is required")
    return tuple(
        WalkForwardFoldSpec(
            fold_id=item.fold_id,
            train=DateRange(start=item.train_start, end=item.train_end),
            validation=DateRange(start=item.validation_start, end=item.validation_end),
            test=DateRange(start=item.test_start, end=item.test_end),
            label_horizon_sessions=item.label_horizon_sessions,
            purge_sessions=item.purge_sessions,
            embargo_sessions=item.embargo_sessions,
            exposure_status=item.exposure_status,
        )
        for item in config.walk_forward.folds
    )


def execute(config: M4PipelineConfig, *, validate_only: bool = False) -> dict[str, Any]:
    started_at = datetime.now().astimezone()
    report_path = _resolve(config.paths.report)
    report: dict[str, Any] = {
        "schema_version": "1",
        "batch_id": config.batch_id,
        "config_id": config.config_id,
        "started_at": started_at.isoformat(),
        "config": config.model_dump(mode="json"),
        "preflight": None,
        "stages": {},
        "status": "RUNNING",
    }
    try:
        report["preflight"] = preflight(config)
        if validate_only:
            report["status"] = "VALIDATED"
            report["completed_at"] = datetime.now().astimezone().isoformat()
            _atomic_report(report_path, report)
            return report

        database = _resolve(config.paths.database)
        factor_store = _resolve(config.paths.factor_store)
        evidence_store = _resolve(config.paths.evidence_store)
        selected = set(config.stages)
        processed_ids = list(config.processed_factor_release_ids)
        evidence_ids: list[str] = []
        robustness_id = config.robustness.source_robustness_id if config.robustness else None
        walk_forward_id = config.walk_forward.source_walk_forward_id if config.walk_forward else None
        redundancy_id = config.redundancy.source_redundancy_id if config.redundancy else None
        factor_explorer_directory: Path | None = None

        for stage in STAGE_ORDER:
            if stage not in selected:
                continue
            stage_started = datetime.now().astimezone()
            if stage == "processed":
                outputs = []
                processed_ids = []
                for variant in config.processed_variants:
                    result = publish_processed(database, factor_store, config.raw_factor_release_id, variant)
                    outputs.append(result)
                    processed_ids.append(result["release_id"])
                stage_result: Any = outputs
            elif stage == "basic_evidence":
                outputs = []
                for release_id in (config.raw_factor_release_id, *processed_ids):
                    result = run_basic_evidence(database, factor_store, evidence_store, release_id)
                    outputs.append(result)
                    evidence_ids.append(result["evidence_id"])
                stage_result = outputs
            elif stage == "robustness":
                assert config.robustness is not None
                selected_evidence = tuple(config.robustness.evidence_ids) or tuple(evidence_ids)
                if not selected_evidence:
                    raise ValueError("robustness requires configured evidence_ids or basic_evidence outputs")
                stage_result = publish_robustness(
                    database,
                    evidence_store,
                    selected_evidence,
                    family_id=config.robustness.family_id,
                )
                robustness_id = stage_result["robustness_id"]
            elif stage == "audit_basic_evidence":
                selected_evidence = tuple(evidence_ids)
                if not selected_evidence and config.robustness is not None:
                    selected_evidence = tuple(config.robustness.evidence_ids)
                if not selected_evidence:
                    raise ValueError("basic evidence audit source IDs are unavailable")
                stage_result = [
                    audit_basic_evidence(database, evidence_store, evidence_id, factor_store)
                    for evidence_id in selected_evidence
                ]
                if any(result["status"] == "FAIL" for result in stage_result):
                    raise ValueError("basic evidence audit failed")
            elif stage == "audit_robustness":
                if robustness_id is None:
                    raise ValueError("robustness audit source ID is unavailable")
                stage_result = audit_robustness(database, evidence_store, robustness_id)
                if stage_result["status"] == "FAIL":
                    raise ValueError("robustness audit failed")
            elif stage == "walk_forward":
                assert config.walk_forward is not None
                stage_result = publish_walk_forward(
                    database,
                    factor_store,
                    evidence_store,
                    raw_release_id=config.raw_factor_release_id,
                    processed_release_ids=tuple(processed_ids),
                    family_id=config.walk_forward.family_id,
                    folds=_folds(config),
                    window=DateRange(
                        start=config.walk_forward.window_start,
                        end=config.walk_forward.window_end,
                    ),
                    engine_version=config.walk_forward.engine_version,
                )
                walk_forward_id = stage_result["walk_forward_id"]
            elif stage == "redundancy":
                assert config.redundancy is not None
                source_id = walk_forward_id or config.redundancy.source_walk_forward_id
                if source_id is None:
                    raise ValueError("redundancy source walk-forward ID is unavailable")
                overrides = {
                    key: value.model_dump(mode="json") for key, value in config.redundancy.direction_overrides.items()
                }
                stage_result = publish_redundancy(
                    database,
                    factor_store,
                    evidence_store,
                    source_walk_forward_id=source_id,
                    family_id=config.redundancy.family_id,
                    direction_overrides=overrides,
                    candidate_policy=config.redundancy.candidate_policy,
                    bind_configuration_to_asset_identity=config.redundancy.bind_configuration_to_asset_identity,
                )
                redundancy_id = stage_result["redundancy_id"]
            elif stage == "audit_walk_forward":
                if walk_forward_id is None:
                    raise ValueError("walk-forward audit source ID is unavailable")
                stage_result = audit_walk_forward(database, factor_store, evidence_store, walk_forward_id)
                if stage_result["status"] == "FAIL":
                    raise ValueError("walk-forward audit failed")
            elif stage == "audit_redundancy":
                if redundancy_id is None:
                    raise ValueError("redundancy audit source ID is unavailable")
                stage_result = audit_redundancy(
                    database,
                    factor_store,
                    evidence_store,
                    redundancy_id,
                )
                if stage_result["status"] == "FAIL":
                    raise ValueError("redundancy audit failed")
            elif stage == "factor_explorer":
                if walk_forward_id is None or redundancy_id is None or config.factor_explorer is None:
                    raise ValueError("factor Explorer source IDs or configuration are unavailable")
                explorer = FactorExplorerConfig(
                    report_name=config.factor_explorer.report_name,
                    title=config.factor_explorer.title,
                    database=config.paths.database,
                    evidence_store=config.paths.evidence_store,
                    output_root=config.factor_explorer.output_root,
                    walk_forward_id=walk_forward_id,
                    redundancy_id=redundancy_id,
                    robustness_id=config.factor_explorer.robustness_id or robustness_id,
                    basic_evidence_ids=config.factor_explorer.basic_evidence_ids,
                    maximum_compare_entities=config.factor_explorer.maximum_compare_entities,
                )
                stage_result = build_factor_explorer(explorer, PROJECT_ROOT)
                factor_explorer_directory = Path(stage_result["index"]).parent
            elif stage == "audit_factor_explorer":
                if factor_explorer_directory is None:
                    raise ValueError("factor Explorer audit source directory is unavailable")
                stage_result = audit_factor_explorer(
                    factor_explorer_directory,
                    database,
                    evidence_store,
                )
                if stage_result["status"] == "FAIL":
                    raise ValueError("factor Explorer audit failed")
            else:
                raise AssertionError(stage)
            report["stages"][stage] = {
                "started_at": stage_started.isoformat(),
                "completed_at": datetime.now().astimezone().isoformat(),
                "result": stage_result,
            }
            _atomic_report(report_path, report)
        report["status"] = "PASS"
    except Exception as error:
        report["status"] = "FAIL"
        report["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        raise
    finally:
        report["completed_at"] = datetime.now().astimezone().isoformat()
        _atomic_report(report_path, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    config_path = args.config if args.config.is_absolute() else PROJECT_ROOT / args.config
    config = _load_config(config_path)
    try:
        report = execute(config, validate_only=args.validate_only)
    except Exception:
        report_path = _resolve(config.paths.report)
        print(report_path.read_text(encoding="utf-8"), file=sys.stderr)
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
