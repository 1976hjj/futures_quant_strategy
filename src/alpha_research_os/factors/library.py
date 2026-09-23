"""Initial mechanism-led M3 factor library and explicitly isolated reproductions."""

from __future__ import annotations

from alpha_research_os.kernel.specs import (
    DataDomain,
    FactorDirection,
    FactorSpec,
    FeatureExpression,
    ImplementationType,
    SignalCutoff,
    TemporalDependency,
)

from .catalog import (
    FactorCatalog,
    FactorCatalogEntry,
    FactorLifecycle,
    FactorSource,
    FactorSourceKind,
)
from .expression import compile_feature_expression

INTERNAL_SOURCE = FactorSource(
    source_id="internal-mechanism-v1",
    kind=FactorSourceKind.INTERNAL_HYPOTHESIS,
    title="Alpha Research OS mechanism-led seed library",
    license_note="Project-owned research specification.",
    formula_verified_against_primary_source=True,
)

def _entry(
    *,
    factor_id: str,
    name: str,
    family: str,
    formula: str,
    field_domains: dict[str, DataDomain],
    direction: FactorDirection,
    hypothesis: str,
    mechanism: str,
    source: FactorSource = INTERNAL_SOURCE,
    lifecycle: FactorLifecycle = FactorLifecycle.CANDIDATE,
    adaptation_notes: str = "Native A-share definition; no external formula adaptation.",
    factor_version: str = "1.0.0",
    generation_process: str = "M3-A preregistered seed library; formula fixed before real-data evaluation.",
) -> FactorCatalogEntry:
    compiled = compile_feature_expression(formula)
    dependencies = tuple(
        TemporalDependency(
            field=item.field,
            data_domain=field_domains[item.field],
            relative_session=item.relative_session,
        )
        for item in compiled.dependencies
    )
    spec = FactorSpec(
        factor_id=factor_id,
        factor_version=factor_version,
        name=name,
        author="alpha-research-os",
        source=source.source_id,
        economic_hypothesis=hypothesis,
        expected_mechanism=mechanism,
        implementation_type=ImplementationType.EXPRESSION,
        expression=FeatureExpression(formula=formula, dependencies=dependencies),
        required_fields=tuple(sorted(compiled.fields)),
        data_domains=tuple(sorted(set(field_domains.values()), key=lambda item: item.value)),
        lookback_sessions=compiled.required_history + 1,
        warmup_sessions=compiled.required_history,
        signal_cutoff=SignalCutoff.POST_CLOSE,
        missing_value_policy="propagate",
        infinite_value_policy="to_missing",
        outlier_policy="raw_then_cross_section_pipeline",
        allowed_universe_ids=("ALL-A-PIT",),
        direction=direction,
        implementation_hash=compiled.implementation_hash,
        generation_process=generation_process,
        test_references=(f"m3a-{factor_id}-golden",),
    )
    return FactorCatalogEntry(
        spec=spec,
        family=family,
        source_reference=source,
        adaptation_notes=adaptation_notes,
        lifecycle=lifecycle,
    )


def initial_factor_entries() -> tuple[FactorCatalogEntry, ...]:
    market = DataDomain.MARKET
    fundamental = DataDomain.FUNDAMENTAL
    return (
        _entry(
            factor_id="overnight-gap-1",
            name="Overnight gap",
            family="price-behavior",
            formula="open / Ref(close, 1) - 1",
            field_domains={"open": market, "close": market},
            direction=FactorDirection.TRAIN_FIT,
            hypothesis="Overnight information and auction pressure differ from continuous-session price discovery.",
            mechanism="Separates previous-close-to-open movement from the intraday return.",
        ),
        _entry(
            factor_id="amihud-illiquidity-20",
            name="20-session Amihud-style illiquidity",
            family="liquidity",
            formula="Mean(illiquidity_1d, 20)",
            field_domains={"illiquidity_1d": market},
            direction=FactorDirection.POSITIVE,
            hypothesis="Investors may require compensation for bearing price impact and illiquidity.",
            mechanism="Averages absolute daily return divided by CNY trading amount.",
        ),
        _entry(
            factor_id="roe-pit",
            name="Point-in-time ROE",
            family="quality",
            formula="roe",
            field_domains={"roe": fundamental},
            direction=FactorDirection.POSITIVE,
            hypothesis="Persistently profitable firms may compound capital more efficiently.",
            mechanism="Uses the latest disclosed financial-indicator ROE available at the signal cutoff.",
        ),
        _entry(
            factor_id="debt-to-assets-pit",
            name="Point-in-time leverage",
            family="quality",
            formula="debt_to_assets",
            field_domains={"debt_to_assets": fundamental},
            direction=FactorDirection.NEGATIVE,
            hypothesis="High balance-sheet leverage can amplify distress and refinancing risk.",
            mechanism="Uses the latest disclosed debt-to-assets value available at the signal cutoff.",
        ),
    )


def build_initial_catalog() -> FactorCatalog:
    catalog = FactorCatalog()
    for entry in initial_factor_entries():
        catalog.register(entry)
    return catalog


_M42_REPLACED_FACTOR_IDS = frozenset({"overnight-gap-1"})


def m4_2_factor_entries() -> tuple[FactorCatalogEntry, ...]:
    """Return the M4.2 catalog with corporate-action-sensitive price factors corrected.

    The v1 entry remains immutable in the initial catalog and its published release. The
    overnight gap is replaced because its lookback crosses a corporate-action boundary.
    """

    retained = tuple(
        entry for entry in initial_factor_entries() if entry.spec.factor_id not in _M42_REPLACED_FACTOR_IDS
    )
    adjusted = DataDomain.CORPORATE_ACTION
    replacements = (
        _entry(
            factor_id="overnight-gap-1",
            factor_version="2.0.0",
            name="Total-return-consistent overnight gap",
            family="price-behavior",
            formula="adjusted_open / Ref(adjusted_close, 1) - 1",
            field_domains={"adjusted_open": adjusted, "adjusted_close": adjusted},
            direction=FactorDirection.TRAIN_FIT,
            hypothesis="Overnight information and auction pressure differ from continuous-session price discovery.",
            mechanism=(
                "Measures prior adjusted close to current adjusted open without treating ex-right moves as alpha."
            ),
            adaptation_notes="M4.2 replaces the raw-price v1 gap after the corporate-action audit.",
            generation_process="M4.2 preregistered corporate-action correction; no label statistics used.",
        ),
    )
    return retained + replacements


def build_m4_2_catalog() -> FactorCatalog:
    catalog = FactorCatalog()
    for entry in m4_2_factor_entries():
        catalog.register(entry)
    return catalog
