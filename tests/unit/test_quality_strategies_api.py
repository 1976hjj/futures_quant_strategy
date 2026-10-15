from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from alpha_research_os.portfolio.quality_strategies import QualityStrategyBook, atomic_json
from scripts.serve_strategy_backtest_api import make_handler


@pytest.fixture
def api(tmp_path):
    original = {
        "status": "PASS",
        "config": {"name": "fixture", "start": "2020-01-02", "end": "2025-12-31",
                   "score_rules": [{"factor_id": "quality", "release_id": "sha256:" + "a" * 64, "weight": 1}]},
        "summary": {"total_return": .2},
        "daily": [{"session": "2020-01-02", "nav": 100}, {"session": "2025-12-31", "nav": 120}],
        "annual": [{"year": 2025, "return": .2}],
    }
    atomic_json(tmp_path / "reports/strategy_backtests/example.result.json", original)
    manager = SimpleNamespace(running=lambda: False, has_pending=lambda: False)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(tmp_path, {"http://127.0.0.1:8872"}, manager))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path="", payload=None):
        request = Request(f"http://127.0.0.1:{server.server_port}/api/v1/quality-strategies{path}",
                          data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:8872"})
        try:
            response = urlopen(request)
        except HTTPError as error:
            response = error
        with response:
            assert response.headers["Access-Control-Allow-Origin"] == "http://127.0.0.1:8872"
            return response.status, json.load(response)

    try:
        yield call, manager, tmp_path
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_category_record_detail_and_independent_evidence(api):
    call, _, root = api
    status, listing = call()
    assert status == 200
    assert {c["id"] for c in listing["categories"]} == {"SMALL_CAP", "BANK", "FACTOR_WEIGHTED"}
    for category in ("SMALL_CAP", "BANK", "FACTOR_WEIGHTED"):
        status, saved = call(payload={"job_id": "example", "category": category, "name": category})
        assert status == 201
        status, detail = call("/" + saved["strategy_id"])
        assert status == 200
        assert len(detail["daily"]) == 2
        assert detail["summary"]["total_return"] == .2
        assert call("?category=" + category)[1]["items"][0]["strategy_id"] == saved["strategy_id"]
    (root / "reports/strategy_backtests/example.result.json").unlink()
    assert call("/" + saved["strategy_id"])[0] == 200
    assert call("?category=INVALID")[0] == 400
    assert call("/missing")[0] == 404
    assert call("/jobs/missing")[0] == 404


def test_calculation_validation_and_existing_task_conflict(api, monkeypatch):
    call, manager, _ = api
    sid = call(payload={"job_id": "example", "category": "SMALL_CAP", "name": "candidate"})[1]["strategy_id"]
    for payload in ({"capital_cny": -1}, {"capital_cny": 0}, {"extra": 1},
                    {"current_holdings": [{"ts_code": "830001.BJ", "quantity": 100}]}):
        assert call("/" + sid + "/calculate", payload)[0] == 422
    duplicate = {"current_holdings": [{"ts_code": "600036.SH", "quantity": 100}] * 2}
    assert call("/" + sid + "/calculate", duplicate)[0] == 400
    manager.running = lambda: True
    assert call("/" + sid + "/calculate", {})[0] == 409
    manager.running = lambda: False
    monkeypatch.setattr(QualityStrategyBook, "running", lambda self: True)
    assert call("/" + sid + "/calculate", {})[0] == 409


def test_http_dispatches_validated_strategy_and_funds_to_persistent_job(api, monkeypatch):
    call, _, _ = api
    sid = call(payload={"job_id": "example", "category": "BANK", "name": "candidate"})[1]["strategy_id"]
    dispatched = []

    def capture(self, strategy_id, parameters):
        dispatched.append((strategy_id, parameters))
        return {"job_id": "fixture-job", "status": "RUNNING"}

    monkeypatch.setattr(QualityStrategyBook, "calculate", capture)
    status, job = call("/" + sid + "/calculate", {"capital_cny": 2000000, "current_holdings": []})
    assert status == 202 and job["job_id"] == "fixture-job"
    assert dispatched == [(sid, {"capital_cny": 2000000, "current_holdings": []})]
