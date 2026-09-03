import re
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
RULES_PATH = REPOSITORY_ROOT / "constitution" / "research_rules.yaml"


def load_constitution() -> dict:
    return yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))


def test_constitution_is_owned_and_not_automatically_writable() -> None:
    constitution = load_constitution()["constitution"]
    governance = constitution["governance"]

    assert constitution["precedence"] == "highest"
    assert governance["automated_write_allowed"] is False
    assert governance["owner_approval_required"] is True
    assert governance["changes_require_new_version"] is True
    assert {
        "RESEARCH_CONSTITUTION.md",
        "constitution/research_rules.yaml",
    }.issubset(set(governance["protected_paths"]))


def test_rule_ids_are_unique_contiguous_and_enforceable() -> None:
    rules = load_constitution()["rules"]
    ids = [rule["id"] for rule in rules]

    assert len(ids) == len(set(ids))
    assert ids == [f"RULE-{number:03d}" for number in range(1, len(ids) + 1)]

    for rule in rules:
        assert re.fullmatch(r"RULE-\d{3}", rule["id"])
        assert rule["severity"] in {"blocker", "major", "minor"}
        assert rule["statement"].strip()
        assert rule["enforcement"]
