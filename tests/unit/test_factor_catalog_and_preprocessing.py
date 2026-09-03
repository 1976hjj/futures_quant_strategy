from __future__ import annotations

import pytest

from alpha_research_os.factors import (
    CrossSectionRow,
    FactorCatalog,
    FactorCatalogEntry,
    FactorLifecycle,
    FactorSource,
    FactorSourceKind,
    build_initial_catalog,
    initial_factor_entries,
    process_cross_section,
)
from alpha_research_os.kernel.errors import IntegrityViolation


def test_initial_catalog_keeps_external_reproduction_research_only() -> None:
    catalog = build_initial_catalog()
    entries = catalog.list()
    assert len(entries) == 13
    external = catalog.get("wq-alpha101-reproduction", "1.0.0")
    assert external.entry.lifecycle is FactorLifecycle.RESEARCH_ONLY
    assert external.entry.source_reference.formula_verified_against_primary_source


def test_unverified_external_formula_cannot_be_candidate() -> None:
    base = initial_factor_entries()[0]
    source = FactorSource(
        source_id="community-unverified",
        kind=FactorSourceKind.COMMUNITY_RESEARCH,
        title="Unverified community formula",
        uri="https://example.invalid/factor",
        license_note="Unknown reuse terms.",
        formula_verified_against_primary_source=False,
    )
    with pytest.raises(IntegrityViolation, match="EXTERNAL_FACTOR_SOURCE_UNVERIFIED"):
        FactorCatalog().register(
            FactorCatalogEntry(
                spec=base.spec.model_copy(update={"source": source.source_id}),
                family=base.family,
                source_reference=source,
                adaptation_notes="Pending verification.",
                lifecycle=FactorLifecycle.CANDIDATE,
            )
        )


def test_terminal_lifecycle_requires_evidence() -> None:
    base = initial_factor_entries()[0]
    with pytest.raises(IntegrityViolation, match="FACTOR_LIFECYCLE_WITHOUT_EVIDENCE"):
        FactorCatalogEntry(
            spec=base.spec,
            family=base.family,
            source_reference=base.source_reference,
            adaptation_notes=base.adaptation_notes,
            lifecycle=FactorLifecycle.RETIRED,
        )


def test_cross_section_is_winsorized_standardized_and_neutralized() -> None:
    rows = [
        CrossSectionRow("A", 1.0, "bank", 1.0),
        CrossSectionRow("B", 2.0, "bank", 2.0),
        CrossSectionRow("C", 3.0, "tech", 1.0),
        CrossSectionRow("D", 100.0, "tech", 2.0),
    ]
    result = process_cross_section(rows, mad_scale=2.0)
    assert result[-1].winsorized < 100
    neutralized = [row.neutralized for row in result]
    assert all(value is not None for value in neutralized)
    assert sum(value for value in neutralized if value is not None) == pytest.approx(0.0)


def test_missing_industry_is_not_silently_assigned_to_a_group() -> None:
    result = process_cross_section([CrossSectionRow("A", 1.0, "bank", 1.0), CrossSectionRow("B", 2.0, None, 2.0)])
    assert result[1].neutralized is None
