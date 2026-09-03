from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from alpha_research_os.data.contracts import FetchRequest
from alpha_research_os.data.pit import select_as_of
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.data.synthetic import SyntheticProvider, normalize_synthetic_response
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.specs import DataDomain

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "m2_provider_rows.json"


def test_financial_restatement_is_not_visible_before_its_release(tmp_path: Path) -> None:
    rows = tuple(json.loads(FIXTURE.read_text(encoding="utf-8")))
    provider = SyntheticProvider(rows, retrieved_at=datetime.fromisoformat("2024-09-01T00:00:00+08:00"))
    request = FetchRequest(
        request_id="FINANCIAL-ALL",
        data_domain=DataDomain.FUNDAMENTAL,
        start="2023-01-01",
        end="2024-12-31",
        fields=next(item.fields for item in provider.spec.capabilities if item.data_domain is DataDomain.FUNDAMENTAL),
    )
    response = provider.fetch(request)
    snapshot = RawSnapshotStore(ArtifactStore(tmp_path / "artifacts")).capture(provider.spec, response)
    records = normalize_synthetic_response(response, snapshot.reference)

    before_restatement = select_as_of(
        records,
        signal_cutoff=datetime.fromisoformat("2024-06-01T15:00:00+08:00"),
    )
    after_restatement = select_as_of(
        records,
        signal_cutoff=datetime.fromisoformat("2024-09-01T15:00:00+08:00"),
    )

    assert len(before_restatement) == len(after_restatement) == 1
    assert before_restatement[0].revision_id == "R1"
    assert before_restatement[0].value_map()["value"] == 1_000_000
    assert after_restatement[0].revision_id == "R2"
    assert after_restatement[0].value_map()["value"] == 900_000
