import pytest
import requests

from steam_analyzer.http import request_with_retry


class FakeResponse(requests.Response):
    def __init__(self, status: int, headers: dict | None = None):
        super().__init__()
        self.status_code = status
        self.headers.update(headers or {})


class FakeSession:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_retries_rate_limit_then_succeeds_using_retry_after():
    delays = []
    session = FakeSession([FakeResponse(429, {"Retry-After": "7"}), FakeResponse(200)])
    resp = request_with_retry(session, "GET", "u", sleep=delays.append)
    assert resp.status_code == 200
    assert delays == [7.0]


def test_backoff_doubles_on_server_errors_and_network_failures():
    delays = []
    session = FakeSession([FakeResponse(503), requests.ConnectionError("x"), FakeResponse(200)])
    request_with_retry(session, "GET", "u", backoff_seconds=1, sleep=delays.append)
    assert delays == [1, 2]


def test_gives_up_after_max_attempts():
    session = FakeSession([FakeResponse(500)] * 3)
    with pytest.raises(requests.HTTPError):
        request_with_retry(session, "GET", "u", max_attempts=3, sleep=lambda _: None)
    assert session.calls == 3


def test_client_errors_are_not_retried():
    session = FakeSession([FakeResponse(404)])
    with pytest.raises(requests.HTTPError):
        request_with_retry(session, "GET", "u", sleep=lambda _: None)
    assert session.calls == 1
