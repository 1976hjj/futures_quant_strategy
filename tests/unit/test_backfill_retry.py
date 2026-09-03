from __future__ import annotations

import json
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError

import pytest

from scripts.backfill_tushare_daily import _fetch_with_retry, _retry_delay


class _TransientProvider:
    def __init__(self, failures: int, error: Exception) -> None:
        self.failures = failures
        self.error = error
        self.calls = 0

    def fetch(self, request):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error
        return "recovered"


def _http_error(code: int) -> HTTPError:
    return HTTPError("https://provider.invalid", code, "test", {}, None)


def test_bad_gateway_uses_bounded_exponential_backoff(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr("scripts.backfill_tushare_daily.time.sleep", sleeps.append)
    provider = _TransientProvider(3, _http_error(502))

    result = _fetch_with_retry(provider, object())

    assert result == "recovered"
    assert provider.calls == 4
    assert sleeps == [5.0, 10.0, 20.0]


def test_unbounded_mode_survives_more_than_ten_transient_failures(monkeypatch) -> None:
    sleeps: list[float] = []
    events: list[tuple[str, int, Exception | None, float | None]] = []
    monkeypatch.setattr("scripts.backfill_tushare_daily.time.sleep", sleeps.append)
    provider = _TransientProvider(12, URLError("gateway offline"))

    result = _fetch_with_retry(provider, object(), attempts=None, observer=lambda *event: events.append(event))

    assert result == "recovered"
    assert provider.calls == 13
    assert sleeps[-1] == 60.0
    assert events[-1] == ("RUNNING", 13, None, None)
    assert [event[0] for event in events[:-1]] == ["RETRYING"] * 12


def test_rate_limit_waits_longer_and_is_capped() -> None:
    assert [_retry_delay(_http_error(429), attempt) for attempt in range(1, 6)] == [15.0, 30.0, 60.0, 120.0, 120.0]


def test_transport_error_is_retryable_but_programming_error_is_not() -> None:
    assert _retry_delay(URLError("temporary DNS failure"), 1) == 5.0
    assert _retry_delay(IncompleteRead(b"partial", 42), 2) == 10.0
    assert _retry_delay(json.JSONDecodeError("truncated", "{", 1), 1) == 5.0
    assert _retry_delay(ValueError("invalid payload"), 1) is None


def test_non_transient_http_error_fails_without_sleep(monkeypatch) -> None:
    monkeypatch.setattr(
        "scripts.backfill_tushare_daily.time.sleep",
        lambda _: pytest.fail("non-transient HTTP errors must fail immediately"),
    )
    provider = _TransientProvider(1, _http_error(403))

    with pytest.raises(HTTPError) as error:
        _fetch_with_retry(provider, object())

    assert error.value.code == 403
    assert provider.calls == 1
