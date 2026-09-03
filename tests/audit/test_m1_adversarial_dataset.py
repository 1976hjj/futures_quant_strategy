from __future__ import annotations

import json
from pathlib import Path

import pytest

from alpha_research_os.kernel.audit import (
    FindingSeverity,
    audit_as_of_records,
    audit_label_boundary_overlap,
    audit_signal_execution,
    purge_overlapping_samples,
    require_no_blockers,
)
from alpha_research_os.kernel.errors import IntegrityViolation

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "m1_adversarial_cases.json"


def _cases() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_as_of_poison_records_trigger_locatable_blockers() -> None:
    cases = _cases()
    findings = audit_as_of_records(
        cases["as_of_records"],
        signal_cutoff=cases["signal_cutoff"],
        require_ingested=True,
    )
    observed = {(finding.code, finding.location) for finding in findings}

    assert ("AVAILABILITY_PRECEDES_PUBLICATION", "POISON-FINANCIAL-EARLY-001") in observed
    assert ("PIT_NOT_AVAILABLE", "POISON-FUTURE-AVAILABLE-001") in observed
    assert ("PIT_NOT_INGESTED", "POISON-LATE-INGEST-001") in observed
    assert all(finding.severity is FindingSeverity.BLOCKER for finding in findings)
    assert not any(finding.location == "SAFE-PRICE-001" for finding in findings)


def test_signal_execution_poison_is_rejected() -> None:
    findings = audit_signal_execution(_cases()["signal_execution_cases"])
    observed = {(finding.code, finding.location) for finding in findings}

    assert ("SAME_CLOSE_EXECUTION", "POISON-SAME-CLOSE") in observed
    assert ("EXECUTION_BEFORE_INFORMATION", "POISON-BEFORE-INFORMATION") in observed
    assert not any(finding.location == "SAFE-NEXT-OPEN" for finding in findings)


def test_overlapping_labels_are_located_and_purged(split_spec) -> None:
    samples = _cases()["label_samples"]

    findings = audit_label_boundary_overlap(samples, split_spec)
    kept, purged = purge_overlapping_samples(samples, split_spec)

    assert [(finding.location, finding.evidence["target_split"]) for finding in findings] == [
        ("TRAIN-OVERLAP-001", "validation"),
        ("VALIDATION-OVERLAP-001", "test"),
    ]
    assert {sample["sample_id"] for sample in kept} == {"TRAIN-SAFE-001", "TEST-NO-PURGE-001"}
    assert purged == findings

    with pytest.raises(IntegrityViolation, match="TRAIN-OVERLAP-001") as error:
        require_no_blockers(findings)
    assert error.value.context["boundary_date"] == "2024-07-01"


def test_fixture_reserves_named_m2_adversaries() -> None:
    reserved = _cases()["future_m2_cases"]

    assert set(reserved) == {
        "delisted_instrument",
        "historical_st_change",
        "limit_up_buy_blocked",
        "limit_down_sell_blocked",
    }
