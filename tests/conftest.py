from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from alpha_research_os.data.contracts import FetchRequest
from alpha_research_os.data.raw import RawSnapshotStore
from alpha_research_os.data.synthetic import SyntheticProvider, normalize_synthetic_response
from alpha_research_os.kernel.artifacts import ArtifactStore
from alpha_research_os.kernel.canonical import content_hash
from alpha_research_os.kernel.specs import (
    DataDomain,
    DateRange,
    FeatureExpression,
    GitStateSpec,
    SplitSpec,
    TemporalDependency,
    VersionRef,
)

HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64


@pytest.fixture
def feature_expression() -> FeatureExpression:
    return FeatureExpression(
        formula="close / Ref(close, 5) - 1",
        dependencies=(
            TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=0),
            TemporalDependency(field="close", data_domain=DataDomain.MARKET, relative_session=-5),
        ),
    )


@pytest.fixture
def split_spec() -> SplitSpec:
    return SplitSpec(
        train=DateRange(start="2024-01-01", end="2024-06-30"),
        validation=DateRange(start="2024-07-01", end="2024-07-31"),
        test=DateRange(start="2024-08-01", end="2024-08-31"),
        labels_overlap=True,
        label_horizon_sessions=5,
        purge_sessions=4,
        embargo_sessions=1,
    )


@pytest.fixture
def version_ref():
    def factory(object_id: str, version: str = "1.0.0", digest: str = HASH_A) -> VersionRef:
        return VersionRef(object_id=object_id, version=version, manifest_hash=digest)

    return factory


@pytest.fixture
def clean_git_state() -> GitStateSpec:
    return GitStateSpec(
        commit="1" * 40,
        is_dirty=False,
        status_entries=(),
        worktree_fingerprint=content_hash({"clean": True}),
    )


def aware_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.fixture
def m2_provider() -> SyntheticProvider:
    path = Path(__file__).resolve().parent / "fixtures" / "m2_provider_rows.json"
    rows = tuple(json.loads(path.read_text(encoding="utf-8")))
    return SyntheticProvider(rows, retrieved_at=datetime.fromisoformat("2024-09-01T00:00:00+08:00"))


@pytest.fixture
def m2_records(m2_provider: SyntheticProvider, tmp_path: Path):
    artifacts = ArtifactStore(tmp_path / "m2-fixture-artifacts")
    raw_store = RawSnapshotStore(artifacts)
    records = []
    for capability in m2_provider.spec.capabilities:
        request = FetchRequest(
            request_id=f"FIXTURE-{capability.data_domain.value}",
            data_domain=capability.data_domain,
            start="2010-01-01",
            end="2026-01-01",
            fields=capability.fields,
        )
        response = m2_provider.fetch(request)
        snapshot = raw_store.capture(m2_provider.spec, response)
        records.extend(normalize_synthetic_response(response, snapshot.reference))
    return tuple(records)
