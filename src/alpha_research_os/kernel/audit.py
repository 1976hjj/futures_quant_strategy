"""P0 adversarial audits for time, execution, and split integrity."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from .errors import IntegrityViolation
from .specs import SplitSpec


class FindingSeverity(StrEnum):
    BLOCKER = "BLOCKER"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    NOTE = "NOTE"


@dataclass(frozen=True, slots=True)
class AuditFinding:
    audit_id: str
    code: str
    rule_id: str
    severity: FindingSeverity
    message: str
    location: str
    evidence: dict[str, Any]


def _parse_time(value: str | datetime) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("audit timestamps must include a timezone")
    return parsed


def audit_as_of_records(
    records: Iterable[dict[str, Any]],
    *,
    signal_cutoff: str | datetime,
    require_ingested: bool = True,
) -> tuple[AuditFinding, ...]:
    cutoff = _parse_time(signal_cutoff)
    findings: list[AuditFinding] = []
    for record in records:
        record_id = str(record["source_record_id"])
        published = _parse_time(record["published_at"])
        available = _parse_time(record["available_at"])
        ingested = _parse_time(record["ingested_at"])
        if available < published:
            findings.append(
                AuditFinding(
                    audit_id="AUDIT-DATA-PIT",
                    code="AVAILABILITY_PRECEDES_PUBLICATION",
                    rule_id="RULE-004",
                    severity=FindingSeverity.BLOCKER,
                    message="record is declared available before its public release",
                    location=record_id,
                    evidence={"published_at": published.isoformat(), "available_at": available.isoformat()},
                )
            )
        if record.get("included", True) and available > cutoff:
            findings.append(
                AuditFinding(
                    audit_id="AUDIT-DATA-PIT",
                    code="PIT_NOT_AVAILABLE",
                    rule_id="RULE-002",
                    severity=FindingSeverity.BLOCKER,
                    message="record included in a signal before available_at",
                    location=record_id,
                    evidence={"signal_cutoff": cutoff.isoformat(), "available_at": available.isoformat()},
                )
            )
        if require_ingested and record.get("included", True) and ingested > cutoff:
            findings.append(
                AuditFinding(
                    audit_id="AUDIT-DATA-PIT",
                    code="PIT_NOT_INGESTED",
                    rule_id="RULE-002",
                    severity=FindingSeverity.BLOCKER,
                    message="record included before the simulated system ingested it",
                    location=record_id,
                    evidence={"signal_cutoff": cutoff.isoformat(), "ingested_at": ingested.isoformat()},
                )
            )
    return tuple(findings)


def audit_signal_execution(cases: Iterable[dict[str, Any]]) -> tuple[AuditFinding, ...]:
    findings: list[AuditFinding] = []
    for case in cases:
        case_id = str(case["case_id"])
        information_at = _parse_time(case["information_available_at"])
        execution_at = _parse_time(case["execution_at"])
        if execution_at < information_at:
            findings.append(
                AuditFinding(
                    audit_id="AUDIT-SIGNAL-EXECUTION",
                    code="EXECUTION_BEFORE_INFORMATION",
                    rule_id="RULE-003",
                    severity=FindingSeverity.BLOCKER,
                    message="execution occurs before signal information is available",
                    location=case_id,
                    evidence={
                        "information_available_at": information_at.isoformat(),
                        "execution_at": execution_at.isoformat(),
                    },
                )
            )
        if (
            case.get("uses_session_close", False)
            and case["signal_session"] == case["execution_session"]
            and case["execution_event"] == "CLOSE"
        ):
            findings.append(
                AuditFinding(
                    audit_id="AUDIT-SIGNAL-EXECUTION",
                    code="SAME_CLOSE_EXECUTION",
                    rule_id="RULE-003",
                    severity=FindingSeverity.BLOCKER,
                    message="close-derived signal is executed at the same close",
                    location=case_id,
                    evidence={"session": case["signal_session"], "event": case["execution_event"]},
                )
            )
    return tuple(findings)


def audit_label_boundary_overlap(samples: Iterable[dict[str, Any]], split: SplitSpec) -> tuple[AuditFinding, ...]:
    boundaries = {
        "train": ("validation", split.validation.start),
        "validation": ("test", split.test.start),
    }
    findings: list[AuditFinding] = []
    for sample in samples:
        source_split = sample["split"]
        if source_split not in boundaries:
            continue
        target_split, boundary = boundaries[source_split]
        label_end = _parse_time(sample["label_end_at"])
        # Compare dates: split boundaries are session dates, independent of local timezone.
        if label_end.date() >= boundary:
            findings.append(
                AuditFinding(
                    audit_id="AUDIT-WALK-FORWARD",
                    code="LABEL_BOUNDARY_OVERLAP",
                    rule_id="RULE-013",
                    severity=FindingSeverity.BLOCKER,
                    message=f"{source_split} label reaches the {target_split} boundary",
                    location=str(sample["sample_id"]),
                    evidence={
                        "source_split": source_split,
                        "target_split": target_split,
                        "boundary_date": boundary.isoformat(),
                        "label_end_at": label_end.isoformat(),
                    },
                )
            )
    return tuple(findings)


def purge_overlapping_samples(
    samples: Iterable[dict[str, Any]], split: SplitSpec
) -> tuple[tuple[dict[str, Any], ...], tuple[AuditFinding, ...]]:
    samples_tuple = tuple(samples)
    findings = audit_label_boundary_overlap(samples_tuple, split)
    blocked_ids = {finding.location for finding in findings}
    kept = tuple(sample for sample in samples_tuple if str(sample["sample_id"]) not in blocked_ids)
    return kept, findings


def require_no_blockers(findings: Iterable[AuditFinding]) -> None:
    blockers = [finding for finding in findings if finding.severity is FindingSeverity.BLOCKER]
    if blockers:
        first = blockers[0]
        raise IntegrityViolation(
            first.code,
            f"{first.message}; location={first.location}",
            rule_id=first.rule_id,
            context={"location": first.location, **first.evidence},
        )
