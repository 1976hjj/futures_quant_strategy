from __future__ import annotations

import pytest
from pydantic import ValidationError

from alpha_research_os.orchestration import M4PipelineConfig

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _base(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "batch_id": "M4-BATCH-1",
        "paths": {"report": "reports/m4.json"},
        "stages": ["walk_forward", "redundancy", "audit_redundancy"],
        "raw_factor_release_id": DIGEST_A,
        "walk_forward": {
            "family_id": "WF-FAMILY",
            "window_start": "2020-01-01",
            "window_end": "2022-12-31",
            "folds": [
                {
                    "fold_id": "F1",
                    "train_start": "2020-01-01",
                    "train_end": "2020-12-31",
                    "validation_start": "2021-01-01",
                    "validation_end": "2021-12-31",
                    "test_start": "2022-01-01",
                    "test_end": "2022-12-31",
                    "exposure_status": "RETROSPECTIVE_DIAGNOSTIC",
                }
            ],
        },
        "redundancy": {"family_id": "REDUNDANCY-FAMILY"},
    }
    payload.update(updates)
    return payload


def test_pipeline_config_is_content_addressed_and_order_independent() -> None:
    first = M4PipelineConfig.model_validate(_base())
    reordered = M4PipelineConfig.model_validate(dict(reversed(list(_base().items()))))

    assert first.config_id == reordered.config_id


def test_direction_override_changes_pipeline_identity() -> None:
    baseline = M4PipelineConfig.model_validate(_base())
    changed = M4PipelineConfig.model_validate(
        _base(
            redundancy={
                "family_id": "REDUNDANCY-FAMILY",
                "direction_overrides": {
                    "factor-a": {
                        "multiplier": -1,
                        "direction_source": "DECLARED_NEGATIVE",
                    }
                },
            }
        )
    )

    assert baseline.config_id != changed.config_id


def test_audit_redundancy_requires_redundancy_output_or_pinned_source() -> None:
    payload = _base(stages=["walk_forward", "audit_redundancy"])

    with pytest.raises(ValidationError, match="redundancy audit requires"):
        M4PipelineConfig.model_validate(payload)

    payload["redundancy"] = {
        "family_id": "REDUNDANCY-FAMILY",
        "source_redundancy_id": DIGEST_B,
    }
    assert M4PipelineConfig.model_validate(payload).redundancy is not None


def test_purge_must_cover_label_horizon() -> None:
    payload = _base()
    payload["walk_forward"]["folds"][0]["purge_sessions"] = 4  # type: ignore[index]

    with pytest.raises(ValidationError, match="purge must cover"):
        M4PipelineConfig.model_validate(payload)


def test_standalone_audit_sources_are_explicit() -> None:
    config = M4PipelineConfig.model_validate(
        _base(
            stages=["audit_basic_evidence", "audit_robustness"],
            robustness={
                "family_id": "ROBUSTNESS-FAMILY",
                "evidence_ids": [DIGEST_A],
                "source_robustness_id": DIGEST_B,
            },
            walk_forward=None,
            redundancy=None,
        )
    )

    assert config.robustness is not None
    assert config.robustness.source_robustness_id == DIGEST_B


def test_factor_explorer_requires_config_and_upstream_sources() -> None:
    with pytest.raises(ValidationError, match="factor Explorer stages require"):
        M4PipelineConfig.model_validate(_base(stages=["walk_forward", "redundancy", "factor_explorer"]))

    standalone = _base(
        stages=["factor_explorer"],
        factor_explorer={"report_name": "REPORT-1"},
    )
    with pytest.raises(ValidationError, match="source ID"):
        M4PipelineConfig.model_validate(standalone)

    standalone["walk_forward"]["source_walk_forward_id"] = DIGEST_A  # type: ignore[index]
    standalone["redundancy"]["source_redundancy_id"] = DIGEST_B  # type: ignore[index]
    assert M4PipelineConfig.model_validate(standalone).factor_explorer is not None


def test_factor_explorer_audit_requires_producing_stage() -> None:
    payload = _base(
        stages=["audit_factor_explorer"],
        factor_explorer={"report_name": "REPORT-1"},
    )
    payload["walk_forward"]["source_walk_forward_id"] = DIGEST_A  # type: ignore[index]
    payload["redundancy"]["source_redundancy_id"] = DIGEST_B  # type: ignore[index]

    with pytest.raises(ValidationError, match="audit requires its producing stage"):
        M4PipelineConfig.model_validate(payload)
