from __future__ import annotations

from alpha_research_os.reporting import FactorExplorerConfig, derive_routes

DIGEST = "sha256:" + "a" * 64


def test_routes_preserve_model_eligibility_for_weak_or_contradicted_factor() -> None:
    routes = derive_routes(
        is_canonical=True,
        fold_outcomes=["NOT_REJECTED", "DIRECTION_CONTRADICTED"],
        sample_classification="EXPOSED_RESEARCH_SAMPLE_NOT_OOS",
    )

    assert "MODEL_FEATURE_ELIGIBLE" in routes
    assert "DIRECTION_CONTRADICTED" in routes
    assert "REQUIRES_NEW_OOS" in routes
    assert "DIAGNOSTIC_ONLY" in routes


def test_noncanonical_route_does_not_quarantine_feature() -> None:
    routes = derive_routes(
        is_canonical=False,
        fold_outcomes=[],
        sample_classification="TRUE_OOS",
        execution_available=True,
    )

    assert routes == ["MODEL_FEATURE_ELIGIBLE", "CANONICALIZED_REDUNDANT"]


def test_integrity_blocker_is_the_only_global_quarantine_route() -> None:
    routes = derive_routes(
        is_canonical=True,
        fold_outcomes=["DIRECTION_SUPPORTED"],
        sample_classification="TRUE_OOS",
        integrity_blocked=True,
        execution_available=True,
    )

    assert routes == ["QUARANTINED_INTEGRITY_FAILURE"]


def test_explorer_config_is_strict_and_content_ready() -> None:
    config = FactorExplorerConfig(
        report_name="REPORT-1",
        walk_forward_id=DIGEST,
        redundancy_id=DIGEST,
    )

    assert config.maximum_compare_entities == 6
    assert config.output_root == "reports/factor_explorer"
