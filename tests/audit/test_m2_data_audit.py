from __future__ import annotations

from datetime import timedelta

from alpha_research_os.data.audit import audit_normalized_records, audit_survivorship
from alpha_research_os.data.contracts import FieldValue
from alpha_research_os.data.pit import seal_record
from alpha_research_os.kernel.specs import DataDomain


def _with(record, **updates):
    return seal_record(record.model_copy(update={**updates, "record_hash": None}))


def test_pit_chronology_hash_lineage_and_domain_poison_are_blockers(m2_records) -> None:
    safe_bar = next(record for record in m2_records if record.record_type is DataDomain.MARKET)
    safe_action = next(record for record in m2_records if record.record_type is DataDomain.CORPORATE_ACTION)

    premature = _with(
        safe_bar,
        source_record_id="POISON-PREMATURE",
        available_at=safe_bar.published_at - timedelta(days=1),
    )
    bad_bar_values = tuple(
        FieldValue(name=item.name, value=9.0 if item.name == "high" else item.value) for item in safe_bar.values
    )
    bad_bar = _with(safe_bar, source_record_id="POISON-OHLC", values=bad_bar_values)
    bad_action_values = tuple(
        FieldValue(name=item.name, value=8.50 if item.name == "exchange_reference_price" else item.value)
        for item in safe_action.values
    )
    bad_action = _with(safe_action, source_record_id="POISON-ACTION", values=bad_action_values)

    findings = audit_normalized_records(
        (premature, bad_bar, bad_action),
        known_raw_snapshots={record.raw_snapshot_id for record in m2_records},
    )
    observed = {(finding.code, finding.location) for finding in findings}

    assert ("AVAILABILITY_PRECEDES_PUBLICATION", "POISON-PREMATURE") in observed
    assert ("OHLC_INVARIANT_FAILED", "POISON-OHLC") in observed
    assert ("CORPORATE_ACTION_RECONCILIATION_FAILED", "POISON-ACTION") in observed

    missing_lineage = audit_normalized_records((safe_bar,), known_raw_snapshots=set())
    assert missing_lineage[0].code == "RAW_SNAPSHOT_LINEAGE_MISSING"


def test_survivorship_audit_names_the_missing_delisted_security(m2_records) -> None:
    universe_without_delisted = tuple(
        record
        for record in m2_records
        if not (record.record_type is DataDomain.UNIVERSE and record.instrument_id == "CN-EQ-DELISTED-001")
    )

    findings = audit_survivorship(
        universe_without_delisted,
        {"CN-EQ-000001", "CN-EQ-DELISTED-001"},
    )

    assert len(findings) == 1
    assert findings[0].code == "HISTORICAL_MEMBER_MISSING"
    assert findings[0].location == "CN-EQ-DELISTED-001"
