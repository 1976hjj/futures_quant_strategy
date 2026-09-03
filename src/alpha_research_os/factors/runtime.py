"""Feature-only runtime for registered expression factors."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from datetime import date, datetime

from pydantic import Field, field_validator, model_validator

from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import DataDomain, FrozenSpec, Identifier, ImplementationType

from .registry import RegisteredFactor


class FeatureValue(FrozenSpec):
    name: Identifier
    value: int | float | None

    @field_validator("value")
    @classmethod
    def finite_value(cls, value: int | float | None) -> int | float | None:
        if value is not None and not math.isfinite(float(value)):
            raise ValueError("feature input values must be finite or missing")
        return value


class FeatureInputRow(FrozenSpec):
    session: date
    instrument_id: Identifier
    available_at: datetime
    values: tuple[FeatureValue, ...] = Field(min_length=1)

    @field_validator("available_at")
    @classmethod
    def available_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("available_at must include a timezone")
        return value

    @model_validator(mode="after")
    def fields_are_unique(self) -> FeatureInputRow:
        names = [item.name for item in self.values]
        if len(names) != len(set(names)):
            raise ValueError("feature row fields must be unique")
        return self

    def value_map(self) -> dict[str, int | float | None]:
        return {item.name: item.value for item in self.values}


class RawFactorValue(FrozenSpec):
    session: date
    instrument_id: Identifier
    factor_id: Identifier
    factor_version: str
    variant: str = "RAW"
    value: float | None
    available_at: datetime
    implementation_hash: str


class FeatureRuntime:
    """Evaluate expressions against a view that exposes feature domains only."""

    def __init__(self, field_domains: Mapping[str, DataDomain]) -> None:
        self._field_domains = dict(field_domains)
        forbidden = {
            name: domain.value
            for name, domain in self._field_domains.items()
            if domain in {DataDomain.LABEL, DataDomain.HOLDOUT}
        }
        if forbidden:
            raise IntegrityViolation(
                "FEATURE_VIEW_PRIVILEGED_DOMAIN",
                "FeatureRuntime cannot be constructed with Label or Holdout fields",
                rule_id="RULE-005",
                context={"fields": forbidden},
            )

    def run(
        self,
        factor: RegisteredFactor,
        rows: Iterable[FeatureInputRow],
    ) -> tuple[RawFactorValue, ...]:
        if factor.spec.implementation_type is ImplementationType.PYTHON:
            raise IntegrityViolation(
                "PYTHON_PLUGIN_SANDBOX_REQUIRED",
                "Python factors cannot execute until the isolated plugin runtime is available",
                rule_id="RULE-030",
            )
        compiled = factor.compiled_expression
        assert compiled is not None
        declared_domain_by_field: dict[str, DataDomain] = {}
        assert factor.spec.expression is not None
        for dependency in factor.spec.expression.dependencies:
            previous = declared_domain_by_field.setdefault(dependency.field, dependency.data_domain)
            if previous is not dependency.data_domain:
                raise IntegrityViolation(
                    "FACTOR_FIELD_DOMAIN_AMBIGUOUS",
                    "a field cannot be declared in more than one data domain",
                    rule_id="RULE-005",
                    context={"field": dependency.field},
                )
        missing = sorted(compiled.fields - self._field_domains.keys())
        mismatched = {
            field: {"declared": declared_domain_by_field[field].value, "runtime": self._field_domains[field].value}
            for field in compiled.fields.intersection(self._field_domains)
            if declared_domain_by_field[field] is not self._field_domains[field]
        }
        if missing or mismatched:
            raise IntegrityViolation(
                "FEATURE_VIEW_CONTRACT_MISMATCH",
                "runtime field domains do not satisfy the registered FactorSpec",
                rule_id="RULE-005",
                context={"missing": missing, "mismatched": mismatched},
            )

        validated_rows = tuple(FeatureInputRow.model_validate(row) for row in rows)
        row_by_key: dict[tuple[str, date], FeatureInputRow] = {}
        for row in validated_rows:
            key = (row.instrument_id, row.session)
            if key in row_by_key:
                raise ValueError(f"duplicate feature row: {row.instrument_id}@{row.session.isoformat()}")
            row_by_key[key] = row
        sessions = tuple(sorted({row.session for row in validated_rows}))
        instruments = tuple(sorted({row.instrument_id for row in validated_rows}))
        results: list[RawFactorValue] = []
        for instrument_id in instruments:
            history: dict[str, list[int | float | None]] = {field: [] for field in compiled.fields}
            availability: list[datetime | None] = []
            for session in sessions:
                row = row_by_key.get((instrument_id, session))
                values = row.value_map() if row is not None else {}
                for field in compiled.fields:
                    history[field].append(values.get(field))
                availability.append(row.available_at if row is not None else None)
                if row is None:
                    continue
                dependency_times = []
                for dependency in compiled.dependencies:
                    index = len(availability) - 1 + dependency.relative_session
                    if 0 <= index < len(availability) and availability[index] is not None:
                        dependency_times.append(availability[index])
                result = compiled.evaluate({name: tuple(series) for name, series in history.items()})
                results.append(
                    RawFactorValue(
                        session=session,
                        instrument_id=instrument_id,
                        factor_id=factor.spec.factor_id,
                        factor_version=factor.spec.factor_version,
                        value=None if result is None else float(result),
                        available_at=max(dependency_times, default=row.available_at),
                        implementation_hash=factor.spec.implementation_hash,
                    )
                )
        return tuple(sorted(results, key=lambda item: (item.session, item.instrument_id)))
