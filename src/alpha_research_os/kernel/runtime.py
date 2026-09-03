"""Minimal typed data views proving Feature/Label runtime isolation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .capabilities import Capability, CapabilityAuthority, CapabilityScope
from .errors import IntegrityViolation
from .specs import DataDomain, FeatureExpression, LabelExpression


def _aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")


@dataclass(frozen=True, slots=True)
class PITRecord:
    instrument_id: str
    field: str
    value: Any
    event_time: datetime
    published_at: datetime
    available_at: datetime
    ingested_at: datetime
    data_domain: DataDomain
    source_record_id: str

    def __post_init__(self) -> None:
        for name in ("event_time", "published_at", "available_at", "ingested_at"):
            _aware(getattr(self, name), name)


class FeatureRuntime:
    """Expose only PIT-safe, non-label records to Feature expressions."""

    def records_as_of(
        self,
        expression: FeatureExpression,
        records: tuple[PITRecord, ...],
        *,
        signal_cutoff: datetime,
        require_ingested: bool = True,
    ) -> tuple[PITRecord, ...]:
        expression = FeatureExpression.model_validate(expression)
        _aware(signal_cutoff, "signal_cutoff")
        allowed = {(item.field, item.data_domain) for item in expression.dependencies}
        selected: list[PITRecord] = []
        for record in records:
            if record.available_at < record.published_at:
                raise IntegrityViolation(
                    "AVAILABILITY_PRECEDES_PUBLICATION",
                    "Feature runtime received a record available before publication",
                    rule_id="RULE-004",
                    context={"source_record_id": record.source_record_id},
                )
            if record.data_domain in {DataDomain.LABEL, DataDomain.HOLDOUT}:
                raise IntegrityViolation(
                    "FEATURE_DOMAIN_ACCESS",
                    "Feature runtime received a privileged record",
                    rule_id="RULE-005",
                    context={"source_record_id": record.source_record_id},
                )
            if (record.field, record.data_domain) not in allowed:
                continue
            if record.event_time > signal_cutoff:
                raise IntegrityViolation(
                    "FEATURE_FUTURE_ACCESS",
                    "Feature runtime received a future economic event",
                    rule_id="RULE-001",
                    context={"source_record_id": record.source_record_id},
                )
            if record.available_at > signal_cutoff:
                continue
            if require_ingested and record.ingested_at > signal_cutoff:
                continue
            selected.append(record)
        return tuple(selected)


class LabelRuntime:
    """Label access exists only behind an evaluator-bound capability."""

    def __init__(self, authority: CapabilityAuthority) -> None:
        self._authority = authority

    def authorize(
        self,
        expression: LabelExpression,
        *,
        actor: str,
        experiment_id: str,
        capability: Capability | None,
    ) -> LabelExpression:
        expression = LabelExpression.model_validate(expression)
        self._authority.require(
            capability,
            actor=actor,
            scope=CapabilityScope.READ_LABEL,
            resource=experiment_id,
        )
        return expression
