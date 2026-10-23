"""HTTP scheduling boundaries only, with no fabricated migration/runtime success."""

import json

import pytest

from scripts import deploy_b2_acceptance as acceptance


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(acceptance.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(acceptance.time, "sleep", clock.sleep)
    return clock


def response(state="running", *, code=200, retry=None):
    return (
        code,
        {} if retry is None else {"Retry-After": retry},
        json.dumps({"status": state, "verdict": "accepted", "formal_result": True}).encode(),
    )


class Client:
    def __init__(self, clock, responses):
        self.clock, self.responses = clock, iter(responses)
        self.requests = []

    def request(self, path, *, headers):
        self.requests.append(self.clock.now)
        assert path == "/result" and headers == {"Authorization": "synthetic"}
        return next(self.responses)


def test_long_pending_result_keeps_ordinary_user_bearer_quota(clock):
    client = Client(clock, [response()] * 35 + [response("finished")])
    result = acceptance.wait_for_api_result(client, "/result", {"Authorization": "synthetic"})
    assert result["status"] == "finished"
    assert all(b - a >= 2 for a, b in zip(client.requests, client.requests[1:], strict=False))
    assert sum(instant < 60 for instant in client.requests) == 30
    assert clock.now == 70


@pytest.mark.parametrize(
    "retry,delay",
    [("5", 5), (None, 2), ("NaN", 2), ("-7", 2), ("invalid", 2), ("Infinity", 2), ("3600", 60)],
)
def test_rate_limit_retry_is_bounded_and_can_recover(clock, retry, delay):
    client = Client(clock, [response(code=429, retry=retry), response("finished")])
    acceptance.wait_for_api_result(client, "/result", {"Authorization": "synthetic"})
    assert client.requests == [0, delay]


def test_repeated_rate_limit_does_not_extend_deadline(clock):
    client = Client(clock, [response(code=429, retry="4")] * 3)
    with pytest.raises(AssertionError, match="before its deadline"):
        acceptance.wait_for_api_result(client, "/result", {"Authorization": "synthetic"}, timeout=9)
    assert client.requests == [0, 4, 8]
    assert clock.sleeps == [4, 4, 1] and clock.now == 9


def test_authentication_failure_is_not_treated_as_retryable_rate_limit(clock):
    client = Client(clock, [response(code=401)])
    with pytest.raises(AssertionError, match="HTTPS result status"):
        acceptance.wait_for_api_result(client, "/result", {"Authorization": "synthetic"})
    assert clock.sleeps == []


def test_b2_read_only_contract_does_not_expand_with_later_schema():
    assert set(acceptance.READ_ONLY) == {
        "projects_project",
        "projects_projectrevision",
        "projects_projectstage",
        "localization_knowledgesnapshot",
        "localization_knowledgetranslation",
        "localization_problemtranslation",
    }
    assert all(
        not table.startswith(("organizations_", "coaching_")) for table in acceptance.READ_ONLY
    )
