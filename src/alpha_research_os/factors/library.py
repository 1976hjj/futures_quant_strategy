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

WORLDQUANT_101_SOURCE = FactorSource(
    source_id="kakushadze-2016-alpha101",
    kind=FactorSourceKind.ACADEMIC_PAPER,
    title="101 Formulaic Alphas",
    uri="https://arxiv.org/abs/1601.00991",
    original_identifier="Alpha#101",
    license_note="Formula reproduced for research verification; no claim of present-day efficacy.",
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
        factor_version="1.0.0",
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
        generation_process="M3-A preregistered seed library; formula fixed before real-data evaluation.",
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
            factor_id="price-momentum-20",
            name="20-session price momentum",
            family="momentum",
            formula="close / Ref(close, 20) - 1",
            field_domains={"close": market},
            direction=FactorDirection.POSITIVE,
            hypothesis="Medium-horizon trends may persist because information is incorporated gradually.",
            mechanism="Ranks securities by the cumulative unadjusted close-to-close move over 20 sessions.",
        ),
        _entry(
            factor_id="short-reversal-5",
            name="5-session short reversal",
            family="reversal",
            formula="-(close / Ref(close, 5) - 1)",
            field_domains={"close": market},
            direction=FactorDirection.POSITIVE,
            hypothesis="Short-lived liquidity shocks can partially reverse.",
            mechanism="Assigns larger values to recent five-session losers.",
        ),
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
            factor_id="intraday-strength",
            name="Intraday return strength",
            family="price-behavior",
            formula="close / open - 1",
            field_domains={"close": market, "open": market},
            direction=FactorDirection.TRAIN_FIT,
            hypothesis="Persistent intraday order imbalance may contain short-horizon information.",
            mechanism="Measures the open-to-close return without mixing in the overnight gap.",
        ),
        _entry(
            factor_id="volume-shock-20",
            name="Volume shock versus 20-session mean",
            family="liquidity",
            formula="volume_shares / Mean(volume_shares, 20) - 1",
            field_domains={"volume_shares": market},
            direction=FactorDirection.TRAIN_FIT,
            hypothesis="Abnormal participation may identify information arrival or temporary crowding.",
            mechanism="Compares current share volume with its trailing 20-session mean.",
        ),
        _entry(
            factor_id="return-volatility-20",
            name="20-session return volatility",
            family="risk",
            formula="Std(return_1d, 20)",
            field_domains={"return_1d": market},
            direction=FactorDirection.NEGATIVE,
            hypothesis="High idiosyncratic volatility may be penalized in constrained long-only markets.",
            mechanism="Measures population standard deviation of daily close returns over 20 sessions.",
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
            factor_id="book-to-price",
            name="Book-to-price proxy",
            family="value",
            formula="1 / pb",
            field_domains={"pb": market},
            direction=FactorDirection.POSITIVE,
            hypothesis="Cheaper firms relative to book equity may earn a valuation premium.",
            mechanism="Uses the reciprocal of positive point-in-time price-to-book.",
        ),
        _entry(
            factor_id="earnings-yield",
            name="Earnings yield proxy",
            family="value",
            formula="1 / pe_ttm",
            field_domains={"pe_ttm": market},
            direction=FactorDirection.POSITIVE,
            hypothesis="A higher positive earnings yield may indicate cheaper expected cash flows.",
            mechanism="Uses the reciprocal of positive TTM price-to-earnings.",
        ),
        _entry(
            factor_id="log-size",
            name="Log market capitalization",
            family="size",
            formula="Log(total_mv)",
            field_domains={"total_mv": market},
            direction=FactorDirection.NEGATIVE,
            hypothesis="Smaller firms may carry a risk or mispricing premium after liquidity controls.",
            mechanism="Uses natural log of total market capitalization in the source unit.",
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
        _entry(
            factor_id="wq-alpha101-reproduction",
            name="WorldQuant Alpha 101 reproduction",
            family="external-reproduction",
            formula="(close - open) / (high - low + 0.001)",
            field_domains={"close": market, "open": market, "high": market, "low": market},
            direction=FactorDirection.TRAIN_FIT,
            hypothesis="The close location within the daily range may summarize intraday pressure.",
            mechanism="Reproduces Alpha#101 before A-share execution and cross-sectional adaptation.",
            source=WORLDQUANT_101_SOURCE,
            lifecycle=FactorLifecycle.RESEARCH_ONLY,
            adaptation_notes="Exact formula reproduction; not yet adjusted for A-share price units or limit regimes.",
        ),
    )


def build_initial_catalog() -> FactorCatalog:
    catalog = FactorCatalog()
    for entry in initial_factor_entries():
        catalog.register(entry)
    return catalog
