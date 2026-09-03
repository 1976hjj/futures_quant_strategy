from __future__ import annotations

from datetime import date

import pytest

from alpha_research_os.evaluation import (
    BasicEvidenceRequest,
    EvidenceInputRef,
    ExecutionConstraintLevel,
    LabelAssetRequest,
    RobustnessEvidenceRequest,
    StatisticalInferenceSpec,
    WalkForwardEvaluationSpec,
    WalkForwardFoldSpec,
)
from alpha_research_os.factors.assets import DatasetLineage
from alpha_research_os.kernel.specs import DateRange

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


def test_m4_3_identity_binds_complete_test_family_and_frozen_parameters() -> None:
    inputs = tuple(
        EvidenceInputRef(
            evidence_id=digest,
            evidence_manifest_hash=DIGEST_A,
            factor_release_id=DIGEST_B,
            factor_variant=variant,
        )
        for variant, digest in (("RAW", DIGEST_A), ("SIZE_NEUTRALIZED", DIGEST_B))
    )
    request = RobustnessEvidenceRequest(
        engine_version="m4-3-v1",
        multiple_testing_family_id="M4-3-Q1-ALL-VARIANTS",
        label_release_id=DIGEST_A,
        evidence_inputs=inputs,
        inference=StatisticalInferenceSpec(),
    )
    changed = request.model_copy(
        update={"inference": request.inference.model_copy(update={"bootstrap_block_length": 10})}
    )

    assert request.robustness_id != changed.robustness_id


def _walk_forward_fold(fold_id: str, train_end: date, validation_year: int, test_year: int) -> WalkForwardFoldSpec:
    return WalkForwardFoldSpec(
        fold_id=fold_id,
        train=DateRange(start=date(2020, 1, 2), end=train_end),
        validation=DateRange(start=date(validation_year, 1, 10), end=date(validation_year, 12, 20)),
        test=DateRange(start=date(test_year, 1, 10), end=date(test_year, 12, 20)),
        exposure_status="RETROSPECTIVE_DIAGNOSTIC",
    )


def test_walk_forward_contract_requires_expanding_ordered_folds() -> None:
    first = _walk_forward_fold("WF-2023", date(2021, 12, 20), 2022, 2023)
    second = _walk_forward_fold("WF-2024", date(2022, 12, 20), 2023, 2024)

    spec = WalkForwardEvaluationSpec(folds=(first, second))

    assert spec.folds[1].train.end > spec.folds[0].train.end


def test_walk_forward_purge_must_cover_label_horizon() -> None:
    with pytest.raises(ValueError, match="purge must cover"):
        WalkForwardFoldSpec(
            fold_id="WF-BAD",
            train=DateRange(start=date(2020, 1, 2), end=date(2021, 12, 20)),
            validation=DateRange(start=date(2022, 1, 10), end=date(2022, 12, 20)),
            test=DateRange(start=date(2023, 1, 10), end=date(2023, 12, 20)),
            label_horizon_sessions=5,
            purge_sessions=4,
            exposure_status="RETROSPECTIVE_DIAGNOSTIC",
        )
