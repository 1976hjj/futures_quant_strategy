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
    build_m4_2_catalog,
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


def test_m4_2_catalog_versions_only_corporate_action_sensitive_price_factors() -> None:
    catalog = build_m4_2_catalog()
    assert len(catalog.list()) == 13
    versions = {item.entry.spec.factor_id: item.entry.spec.factor_version for item in catalog.list()}
    assert versions["price-momentum-20"] == "2.0.0"
    assert versions["short-reversal-5"] == "2.0.0"
    assert versions["overnight-gap-1"] == "2.0.0"
    assert versions["intraday-strength"] == "1.0.0"


def test_adjusted_price_factor_is_invariant_to_a_mechanical_split() -> None:
    old = build_initial_catalog().registry.get("short-reversal-5", "1.0.0").compiled_expression
    corrected = build_m4_2_catalog().registry.get("short-reversal-5", "2.0.0").compiled_expression
    assert old is not None and corrected is not None
    raw_history = {"close": (10.0, 10.0, 10.0, 10.0, 10.0, 5.0)}
    adjusted_history = {"adjusted_close": (10.0, 10.0, 10.0, 10.0, 10.0, 10.0)}
    assert old.evaluate(raw_history) == pytest.approx(0.5)
    assert corrected.evaluate(adjusted_history) == pytest.approx(0.0)


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


def test_zero_mad_preserves_nonmedian_information() -> None:
    result = process_cross_section(
        [CrossSectionRow("A", 1.0), CrossSectionRow("B", 1.0), CrossSectionRow("C", 2.0)],
        neutralize_industry=False,
        neutralize_log_size=False,
    )
    assert [row.winsorized for row in result] == [1.0, 1.0, 2.0]
