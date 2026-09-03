from __future__ import annotations

from datetime import UTC, datetime

import pytest

from alpha_research_os.kernel.canonical import FrozenManifest, canonical_json_bytes, content_hash
from alpha_research_os.kernel.errors import CanonicalizationError


def test_canonical_json_and_hash_ignore_mapping_insertion_order() -> None:
    first = {"b": [2, 1], "a": {"x": True, "when": datetime(2024, 1, 1, tzinfo=UTC)}}
    second = {"a": {"when": datetime(2024, 1, 1, tzinfo=UTC), "x": True}, "b": [2, 1]}

    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert content_hash(first) == content_hash(second)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_non_finite_numbers(value: float) -> None:
    with pytest.raises(CanonicalizationError, match="NON_FINITE_NUMBER"):
        canonical_json_bytes({"value": value})


def test_canonical_json_rejects_naive_datetimes() -> None:
    with pytest.raises(CanonicalizationError, match="NAIVE_DATETIME"):
        canonical_json_bytes({"when": datetime(2024, 1, 1)})


def test_manifest_hash_and_payload_hash_are_independently_recomputable() -> None:
    manifest = FrozenManifest.build("experiment_spec", {"z": 1, "a": "A股"})

    rebuilt = FrozenManifest.build("experiment_spec", {"a": "A股", "z": 1})

    assert manifest.verify()
    assert manifest.payload_hash == rebuilt.payload_hash
    assert manifest.manifest_hash == rebuilt.manifest_hash
    assert manifest.to_bytes() == rebuilt.to_bytes()
