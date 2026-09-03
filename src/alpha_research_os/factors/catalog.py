"""Governed factor catalog layered on top of the immutable executable registry."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import HttpUrl, model_validator

from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.errors import IntegrityViolation
from alpha_research_os.kernel.specs import FactorSpec, FrozenSpec, Identifier

from .registry import FactorRegistry, RegisteredFactor


class FactorSourceKind(StrEnum):
    INTERNAL_HYPOTHESIS = "INTERNAL_HYPOTHESIS"
    ACADEMIC_PAPER = "ACADEMIC_PAPER"
    BROKER_REPORT = "BROKER_REPORT"
    COMMUNITY_RESEARCH = "COMMUNITY_RESEARCH"
    OPEN_SOURCE_IMPLEMENTATION = "OPEN_SOURCE_IMPLEMENTATION"


class FactorLifecycle(StrEnum):
    CANDIDATE = "CANDIDATE"
    RESEARCH_ONLY = "RESEARCH_ONLY"
    REJECTED = "REJECTED"
    WATCH = "WATCH"
    DECAYED = "DECAYED"
    RETIRED = "RETIRED"


class FactorSource(FrozenSpec):
    source_id: Identifier
    kind: FactorSourceKind
    title: str
    uri: HttpUrl | None = None
    original_identifier: str | None = None
    license_note: str
    formula_verified_against_primary_source: bool

    @model_validator(mode="after")
    def external_sources_require_a_uri(self) -> FactorSource:
        if self.kind is not FactorSourceKind.INTERNAL_HYPOTHESIS and self.uri is None:
            raise ValueError("external factor sources require a stable URI")
        return self


class FactorCatalogEntry(FrozenSpec):
    spec: FactorSpec
    family: Identifier
    source_reference: FactorSource
    adaptation_notes: str
    lifecycle: FactorLifecycle = FactorLifecycle.CANDIDATE
    availability_start: date | None = None
    availability_end: date | None = None
    lifecycle_evidence_ids: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_lifecycle_and_coverage(self) -> FactorCatalogEntry:
        if self.spec.source != self.source_reference.source_id:
            raise IntegrityViolation(
                "FACTOR_SOURCE_MISMATCH",
                "FactorSpec source must match its catalog source reference",
                rule_id="RULE-027",
            )
        if self.availability_start and self.availability_end and self.availability_end < self.availability_start:
            raise ValueError("factor availability_end must not precede availability_start")
        if self.lifecycle in {FactorLifecycle.REJECTED, FactorLifecycle.DECAYED, FactorLifecycle.RETIRED}:
            if not self.lifecycle_evidence_ids:
                raise IntegrityViolation(
                    "FACTOR_LIFECYCLE_WITHOUT_EVIDENCE",
                    "a terminal factor status requires evidence identifiers",
                    rule_id="RULE-028",
                )
        if self.source_reference.kind is not FactorSourceKind.INTERNAL_HYPOTHESIS:
            if not self.source_reference.formula_verified_against_primary_source:
                if self.lifecycle is not FactorLifecycle.RESEARCH_ONLY:
                    raise IntegrityViolation(
                        "EXTERNAL_FACTOR_SOURCE_UNVERIFIED",
                        "an unverified external formula must remain RESEARCH_ONLY",
                        rule_id="RULE-032",
                    )
        return self


class CatalogedFactor(FrozenSpec):
    entry: FactorCatalogEntry
    entry_hash: str
    spec_hash: str


class FactorCatalog:
    """Immutable source-aware catalog; it cannot promote candidates to CORE."""

    def __init__(self, registry: FactorRegistry | None = None) -> None:
        self.registry = registry or FactorRegistry()
        self._entries: dict[tuple[str, str], CatalogedFactor] = {}

    def register(self, entry: FactorCatalogEntry) -> tuple[CatalogedFactor, RegisteredFactor]:
        validated = FactorCatalogEntry.model_validate(entry)
        registered = self.registry.register(validated.spec)
        cataloged = CatalogedFactor(
            entry=validated,
            entry_hash=content_hash(validated.model_dump(mode="json")),
            spec_hash=registered.spec_hash,
        )
        key = (validated.spec.factor_id, validated.spec.factor_version)
        existing = self._entries.get(key)
        if existing is not None and existing.entry_hash != cataloged.entry_hash:
            raise IntegrityViolation(
                "FACTOR_CATALOG_VERSION_CONFLICT",
                "a catalog identity and version cannot be rebound to different provenance",
                rule_id="RULE-027",
                context={"factor_id": key[0], "factor_version": key[1]},
            )
        self._entries[key] = existing or cataloged
        return self._entries[key], registered

    def get(self, factor_id: str, factor_version: str) -> CatalogedFactor:
        try:
            return self._entries[(factor_id, factor_version)]
        except KeyError:
            raise KeyError(f"factor is not cataloged: {factor_id}@{factor_version}") from None

    def list(self) -> tuple[CatalogedFactor, ...]:
        return tuple(self._entries[key] for key in sorted(self._entries))
