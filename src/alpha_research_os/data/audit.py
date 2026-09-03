"""Data-domain blockers required before PIT dataset publication."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from math import isclose

from alpha_research_os.kernel.audit import AuditFinding, FindingSeverity
from alpha_research_os.kernel.specs import DataDomain

from .contracts import NormalizedRecord
from .pit import fact_content_hash, record_content_hash


def _blocker(code: str, rule_id: str, message: str, record: NormalizedRecord, **evidence) -> AuditFinding:
    return AuditFinding(
        audit_id="AUDIT-DATA-PIT",
        code=code,
        rule_id=rule_id,
        severity=FindingSeverity.BLOCKER,
        message=message,
        location=record.source_record_id,
        evidence=evidence,
    )


def audit_normalized_records(
    records: Iterable[NormalizedRecord],
    *,
    known_raw_snapshots: set[str],
) -> tuple[AuditFinding, ...]:
    records_tuple = tuple(records)
    findings: list[AuditFinding] = []
    identities: dict[tuple[str, str], str] = {}
    for record in records_tuple:
        if record.available_at < record.published_at:
            findings.append(
                _blocker(
                    "AVAILABILITY_PRECEDES_PUBLICATION",
                    "RULE-004",
                    "record is available before publication",
                    record,
                    published_at=record.published_at.isoformat(),
                    available_at=record.available_at.isoformat(),
                )
            )
        if record.raw_snapshot_id not in known_raw_snapshots:
            findings.append(
                _blocker(
                    "RAW_SNAPSHOT_LINEAGE_MISSING",
                    "RULE-009",
                    "normalized record has no captured raw snapshot",
                    record,
                    raw_snapshot_id=record.raw_snapshot_id,
                )
            )
        expected_hash = record_content_hash(record)
        if record.record_hash != expected_hash:
            findings.append(
                _blocker(
                    "RECORD_HASH_MISMATCH",
                    "RULE-027",
                    "record hash is absent or cannot be recomputed",
                    record,
                    declared=record.record_hash,
                    expected=expected_hash,
                )
            )
        identity = (record.logical_key, record.revision_id)
        fact_hash = fact_content_hash(record)
        prior_hash = identities.get(identity)
        if prior_hash is not None and prior_hash != fact_hash:
            findings.append(
                _blocker(
                    "DUPLICATE_REVISION_CONFLICT",
                    "RULE-010",
                    "same logical key and revision contain conflicting facts",
                    record,
                    logical_key=record.logical_key,
                    revision_id=record.revision_id,
                )
            )
        identities[identity] = fact_hash
        if record.record_type is DataDomain.MARKET:
            findings.extend(_audit_daily_bar(record))
        elif record.record_type is DataDomain.CORPORATE_ACTION:
            findings.extend(_audit_corporate_action(record))
    return tuple(findings)


def _audit_daily_bar(record: NormalizedRecord) -> list[AuditFinding]:
    values = record.value_map()
    required = {"open", "high", "low", "close", "volume", "amount"}
    if not required.issubset(values):
        return []
    open_, high, low, close = (float(values[name]) for name in ("open", "high", "low", "close"))
    findings: list[AuditFinding] = []
    if low > min(open_, close) or high < max(open_, close) or high < low:
        findings.append(
            _blocker(
                "OHLC_INVARIANT_FAILED",
                "RULE-010",
                "daily bar violates high/low price invariants",
                record,
                open=open_,
                high=high,
                low=low,
                close=close,
            )
        )
    if float(values["volume"]) < 0 or float(values["amount"]) < 0:
        findings.append(
            _blocker(
                "NEGATIVE_TRADING_ACTIVITY",
                "RULE-010",
                "volume and amount must be non-negative",
                record,
            )
        )
    return findings


def _audit_corporate_action(record: NormalizedRecord) -> list[AuditFinding]:
    values = record.value_map()
    required = {
        "previous_close",
        "cash_dividend",
        "bonus_ratio",
        "rights_ratio",
        "rights_price",
        "exchange_reference_price",
        "price_tick",
    }
    if not required.issubset(values):
        return []
    previous = Decimal(str(values["previous_close"]))
    dividend = Decimal(str(values["cash_dividend"]))
    bonus = Decimal(str(values["bonus_ratio"]))
    rights = Decimal(str(values["rights_ratio"]))
    rights_price = Decimal(str(values["rights_price"]))
    tick = Decimal(str(values["price_tick"]))
    exchange = Decimal(str(values["exchange_reference_price"]))
    expected = (previous - dividend + rights_price * rights) / (Decimal(1) + bonus + rights)
    expected = (expected / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * tick
    if expected != exchange:
        return [
            _blocker(
                "CORPORATE_ACTION_RECONCILIATION_FAILED",
                "RULE-009",
                "exchange ex-right reference price does not reconcile",
                record,
                expected=str(expected),
                observed=str(exchange),
            )
        ]
    return []


def audit_survivorship(
    records: Iterable[NormalizedRecord], expected_historical_members: set[str]
) -> tuple[AuditFinding, ...]:
    by_instrument: defaultdict[str, list[NormalizedRecord]] = defaultdict(list)
    for record in records:
        if record.record_type is DataDomain.UNIVERSE and record.instrument_id:
            by_instrument[record.instrument_id].append(record)
    missing = sorted(expected_historical_members - set(by_instrument))
    return tuple(
        AuditFinding(
            audit_id="AUDIT-UNIVERSE-SURVIVORSHIP",
            code="HISTORICAL_MEMBER_MISSING",
            rule_id="RULE-007",
            severity=FindingSeverity.BLOCKER,
            message="an expected historical member disappeared from the universe history",
            location=instrument_id,
            evidence={"instrument_id": instrument_id},
        )
        for instrument_id in missing
    )


def audit_cross_source_market(
    records: Iterable[NormalizedRecord],
    *,
    price_abs_tolerance: float = 0.005,
    volume_abs_tolerance: float = 0.0,
    amount_abs_tolerance: float = 1.0,
    amount_relative_tolerance: float = 1e-6,
) -> tuple[AuditFinding, ...]:
    """Compare independently acquired bars after unit normalization.

    A mismatch is MAJOR rather than automatically assigning blame to either
    provider. The affected partition must be investigated before promotion.
    """

    grouped: defaultdict[str, dict[str, NormalizedRecord]] = defaultdict(dict)
    for record in records:
        if record.record_type is not DataDomain.MARKET:
            continue
        current = grouped[record.logical_key].get(record.source)
        if current is None or (record.ingested_at, record.revision_id) > (current.ingested_at, current.revision_id):
            grouped[record.logical_key][record.source] = record

    findings = []
    for logical_key, by_source in sorted(grouped.items()):
        if len(by_source) < 2:
            continue
        sources = sorted(by_source)
        baseline_source = sources[0]
        baseline = by_source[baseline_source].value_map()
        for source in sources[1:]:
            candidate = by_source[source].value_map()
            for field in ("open", "high", "low", "close", "volume", "amount"):
                if field not in baseline or field not in candidate:
                    continue
                absolute_tolerance = {
                    "volume": volume_abs_tolerance,
                    "amount": amount_abs_tolerance,
                }.get(field, price_abs_tolerance)
                relative_tolerance = amount_relative_tolerance if field == "amount" else 0.0
                if isclose(
                    float(baseline[field]),
                    float(candidate[field]),
                    rel_tol=relative_tolerance,
                    abs_tol=absolute_tolerance,
                ):
                    continue
                findings.append(
                    AuditFinding(
                        audit_id="AUDIT-DATA-CROSS-SOURCE",
                        code="CROSS_SOURCE_MARKET_MISMATCH",
                        rule_id="RULE-010",
                        severity=FindingSeverity.MAJOR,
                        message="independent providers disagree after canonical unit normalization",
                        location=logical_key,
                        evidence={
                            "baseline_source": baseline_source,
                            "baseline_value": baseline[field],
                            "candidate_source": source,
                            "candidate_value": candidate[field],
                            "field": field,
                        },
                    )
                )
    return tuple(findings)
