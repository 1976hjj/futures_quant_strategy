from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from alpha_research_os.kernel.canonical import canonical_json_bytes, content_hash

json_scalar = st.none() | st.booleans() | st.integers() | st.text()


@given(st.dictionaries(st.text(min_size=1), json_scalar, max_size=20))
def test_canonical_mapping_is_invariant_to_insertion_order(mapping: dict) -> None:
    reversed_mapping = dict(reversed(tuple(mapping.items())))

    assert canonical_json_bytes(mapping) == canonical_json_bytes(reversed_mapping)
    assert content_hash(mapping) == content_hash(reversed_mapping)


@given(st.binary(max_size=2048))
def test_manifest_payload_hash_is_repeatable(payload: bytes) -> None:
    from alpha_research_os.kernel.canonical import sha256_bytes

    assert sha256_bytes(payload) == sha256_bytes(bytes(payload))
