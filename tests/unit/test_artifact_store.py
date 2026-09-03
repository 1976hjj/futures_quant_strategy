from __future__ import annotations

from pathlib import Path

import pytest

from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.errors import ArtifactConflictError, IntegrityViolation
from alpha_research_os.kernel.specs import (
    DateRange,
    EvaluatorSpec,
    ExperimentSpec,
    SearchDimension,
    SplitSpec,
)


def test_artifact_store_is_content_addressed_and_idempotent(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    first = store.put_bytes(b"frozen evidence", media_type="application/octet-stream", metadata={"b": 2, "a": 1})
    second = store.put_bytes(b"frozen evidence", media_type="application/octet-stream", metadata={"a": 1, "b": 2})

    assert first == second
    assert store.read_bytes(first) == b"frozen evidence"
    manifest = store.read_manifest(first)
    assert manifest["payload"]["artifact_id"] == first.artifact_id
    assert manifest["payload_hash"].startswith("sha256:")


def test_artifact_read_detects_external_tampering(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    reference = store.put_bytes(b"original", media_type="text/plain")
    digest = reference.artifact_id.split(":", 1)[1]
    object_path = tmp_path / "artifacts" / "objects" / "sha256" / digest[:2] / digest
    object_path.write_bytes(b"tampered")

    with pytest.raises(IntegrityViolation, match="ARTIFACT_HASH_MISMATCH"):
        store.read_bytes(reference)


def test_experiment_id_cannot_be_rebound_to_modified_spec(
    tmp_path: Path,
    clean_git_state,
    version_ref,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    split = SplitSpec(
        train=DateRange(start="2024-01-01", end="2024-06-30"),
        validation=DateRange(start="2024-07-01", end="2024-07-31"),
        test=DateRange(start="2024-08-01", end="2024-08-31"),
        labels_overlap=False,
        label_horizon_sessions=1,
        purge_sessions=0,
        embargo_sessions=0,
    )
    spec = ExperimentSpec(
        experiment_id="EXP-20240901-A234",
        hypothesis="Frozen before execution.",
        constitution_version="1.0.0-draft",
        git_state=clean_git_state,
        dataset=version_ref("dataset"),
        universe=version_ref("universe"),
        factors=(version_ref("factor"),),
        label=version_ref("label"),
        preprocessing_versions=(),
        split=split,
        evaluator=EvaluatorSpec(name="rank-ic", version="1"),
        multiple_testing_family_id="MTF-001",
        execution_model_version="diagnostic-1",
        cost_model_version="not-applicable-1",
        capacity_model_version="not-applicable-1",
        search_space=(SearchDimension(name="window", values=(5,)),),
        search_budget=1,
        random_seed=1,
        promotion_gates=("no-lookahead",),
    )
    original = store.put_experiment_spec(spec)
    assert store.put_experiment_spec(spec) == original

    modified_same_id = spec.model_copy(update={"hypothesis": "Changed after registration."})
    with pytest.raises(ArtifactConflictError, match="ARTIFACT_IMMUTABILITY"):
        store.put_experiment_spec(modified_same_id)
