from __future__ import annotations

from datetime import date

from alpha_research_os.evaluation import BasicEvidenceRequest, ExecutionConstraintLevel, LabelAssetRequest
from alpha_research_os.factors.assets import DatasetLineage

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _label_request(*, constraint: ExecutionConstraintLevel) -> LabelAssetRequest:
    return LabelAssetRequest(
        engine_version="label-engine-v1",
        label_id="forward-5d",
        label_version="1.0.0",
        label_spec_hash=DIGEST_A,
        source_factor_release_id=DIGEST_B,
        dataset_lineage=(DatasetLineage(manifest_table="metadata.archive_manifest", checkpoint_hashes=(DIGEST_A,)),),
        universe_id="ALL-A-PIT",
        universe_version="m2b-v1",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        constraint_level=constraint,
    )


def test_label_identity_changes_when_execution_constraints_change() -> None:
    provisional = _label_request(constraint=ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY)
    limit_aware = _label_request(constraint=ExecutionConstraintLevel.LIMIT_AWARE)

    assert provisional.computation_key != limit_aware.computation_key
    assert (
        provisional.computation_key
        == _label_request(constraint=ExecutionConstraintLevel.BAR_AND_SUSPENSION_ONLY).computation_key
    )


def test_evidence_identity_binds_factor_label_and_evaluator() -> None:
    request = BasicEvidenceRequest(
        evaluator_version="basic-v1",
        factor_release_id=DIGEST_A,
        label_release_id=DIGEST_B,
    )
    changed_label = request.model_copy(update={"label_release_id": DIGEST_A})
    changed_quantiles = request.model_copy(update={"quantile_count": 10})

    assert request.evidence_id != changed_label.evidence_id
    assert request.evidence_id != changed_quantiles.evidence_id
