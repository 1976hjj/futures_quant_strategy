"""Collect a bounded AKShare/BaoStock audit sample into immutable local artifacts."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from alpha_research_os.data.audit import audit_cross_source_market, audit_normalized_records
from alpha_research_os.data.contracts import FetchRequest
from alpha_research_os.data.providers import (
    AKShareProvider,
    BaoStockProvider,
    normalize_akshare_market,
    normalize_baostock_market,
    normalize_baostock_status,
)
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.canonical import canonical_json_bytes
from alpha_research_os.kernel.specs import DataDomain


def _fields(provider, domain: DataDomain) -> tuple[str, ...]:
    return next(item.fields for item in provider.spec.capabilities if item.data_domain is domain)


def _finding_dict(finding) -> dict[str, object]:
    return {
        "audit_id": finding.audit_id,
        "code": finding.code,
        "evidence": finding.evidence,
        "location": finding.location,
        "message": finding.message,
        "rule_id": finding.rule_id,
        "severity": finding.severity.value,
    }


def _fetch_with_retry(provider, request: FetchRequest, *, attempts: int = 3):
    for attempt in range(1, attempts + 1):
        try:
            return provider.fetch(request)
        except Exception:
            if attempt == attempts:
                raise
            time.sleep(0.5 * (2 ** (attempt - 1)))
    raise AssertionError("retry loop exhausted without returning or raising")


def collect(*, start: str, end: str, instruments: tuple[str, ...], output: Path) -> dict[str, object]:
    artifacts = ArtifactStore(output / "artifacts")
    raw_store = RawSnapshotStore(artifacts)
    akshare = AKShareProvider()
    baostock = BaoStockProvider()
    snapshots = []
    market_records = []
    status_records = []
    market_endpoints = {}

    for provider, normalizer in (
        (akshare, normalize_akshare_market),
        (baostock, normalize_baostock_market),
    ):
        request = FetchRequest(
            request_id=f"AUDIT-{provider.spec.provider_id}-market-{start}-{end}",
            data_domain=DataDomain.MARKET,
            start=start,
            end=end,
            fields=_fields(provider, DataDomain.MARKET),
            instrument_ids=instruments,
        )
        response = _fetch_with_retry(provider, request)
        captured = raw_store.capture(provider.spec, response, storage_encoding="gzip")
        snapshots.append(captured.reference)
        market_records.extend(normalizer(response, captured.reference))
        market_endpoints[provider.spec.provider_id] = json.loads(response.payload)["endpoint"]

    status_request = FetchRequest(
        request_id=f"AUDIT-baostock-status-{start}-{end}",
        data_domain=DataDomain.SECURITY_STATUS,
        start=start,
        end=end,
        fields=_fields(baostock, DataDomain.SECURITY_STATUS),
        instrument_ids=instruments,
    )
    status_response = _fetch_with_retry(baostock, status_request)
    status_snapshot = raw_store.capture(baostock.spec, status_response, storage_encoding="gzip")
    snapshots.append(status_snapshot.reference)
    status_records.extend(normalize_baostock_status(status_response, status_snapshot.reference))

    all_records = (*market_records, *status_records)
    findings = (
        *audit_normalized_records(
            all_records,
            known_raw_snapshots={snapshot.snapshot_id for snapshot in snapshots},
        ),
        *audit_cross_source_market(market_records),
    )
    summary = {
        "audit_schema": "free-provider-audit-v1",
        "coverage": {"end": end, "start": start},
        "findings": [_finding_dict(finding) for finding in findings],
        "instruments": list(instruments),
        "market_endpoints": market_endpoints,
        "providers": {
            akshare.spec.provider_id: akshare.spec.provider_version,
            baostock.spec.provider_id: baostock.spec.provider_version,
        },
        "record_counts": {
            "market": len(market_records),
            "security_status": len(status_records),
        },
        "snapshot_ids": sorted(snapshot.snapshot_id for snapshot in snapshots),
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_bytes(canonical_json_bytes(summary))
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--instrument", action="append", required=True, dest="instruments")
    parser.add_argument("--output", type=Path, default=Path("data/free_audit"))
    args = parser.parse_args()
    summary = collect(
        start=args.start,
        end=args.end,
        instruments=tuple(args.instruments),
        output=args.output,
    )
    sys.stdout.buffer.write(canonical_json_bytes(summary) + b"\n")
    return 1 if summary["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
