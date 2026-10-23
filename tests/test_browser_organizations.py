"""Fixture/control checks only; actual Chromium evidence is a separate receipt."""

import copy
import json
import uuid

import pytest

from organizations.models import Package
from organizations.packages import validate_payload
from scripts.browser_organizations import (
    bootstrap_reviewer,
    checked_owner_action,
    synthetic_payload,
    verify_retention,
)


def test_browser_package_is_bounded_valid_data_without_mutating_catalog(package):
    before = copy.deepcopy(package)
    payload = synthetic_payload(package, "PRIVATE-BROWSER-CANARY")
    assert package == before
    assert len(json.dumps(payload, ensure_ascii=False).encode()) < 75000
    owned = validate_payload(Package(organization_id="org-browser-test", slug="fixture"), payload)
    assert owned["problems"][0]["id"] == "org-browser-test--fixture--positive-sum"
    assert "PRIVATE-BROWSER-CANARY" not in owned["problems"][0]["statement"]
    assert "PRIVATE-BROWSER-CANARY-reference" in owned["problems"][0]["reference"]
    assert any(
        case["name"] == "PRIVATE-BROWSER-CANARY-hidden-case" and not case["public"]
        for case in owned["problems"][0]["tests"]
    )


def test_bootstrap_uses_one_atomic_owner_command_and_private_prefixed_environment():
    calls = []
    credentials = bootstrap_reviewer(lambda *args, **kwargs: calls.append((args, kwargs)), {})
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:3] == ("manage.py", "shell", "-c")
    assert "with transaction.atomic():" in args[3]
    assert "organization_reviewer" in args[3] and "review_packages" in args[3]
    assert set(credentials) == {"BROWSER_ORG_REVIEWER_USERNAME", "BROWSER_ORG_REVIEWER_PASSWORD"}
    assert credentials["BROWSER_ORG_REVIEWER_PASSWORD"] not in repr(args)
    assert kwargs["env"] == credentials


@pytest.mark.parametrize(
    "action,identity,digest",
    [
        ("shell", "1", "a" * 64),
        ("publish", "../1", "a" * 64),
        ("publish", "0", "a" * 64),
        ("publish", "1", "a" * 63),
        ("withdraw", "1", "a" * 64),
        ("withdraw", 123, "a" * 64),
    ],
)
def test_owner_action_rejects_unrecognized_command_identity_or_digest(action, identity, digest):
    calls = []
    with pytest.raises(ValueError):
        checked_owner_action(
            lambda *args, **kwargs: calls.append(args), {}, action, identity, digest
        )
    assert calls == []


def test_owner_actions_are_fixed_management_commands_with_exact_snapshot():
    calls = []
    runner = lambda *args, **kwargs: calls.append((args, kwargs))  # noqa: E731
    package_id = str(uuid.uuid4())
    checked_owner_action(runner, {"BROWSER_URL": "http://127.0.0.1"}, "publish", "17", "a" * 64)
    checked_owner_action(runner, {}, "withdraw", package_id, "b" * 64)
    assert calls[0][0] == ("manage.py", "org_package_publish", "17", "--digest", "a" * 64)
    assert calls[1][0] == ("manage.py", "org_package_withdraw", package_id, "--digest", "b" * 64)


def test_retention_check_refuses_nonfixture_scope_before_any_owner_access():
    calls = []
    with pytest.raises(ValueError):
        verify_retention(
            lambda *args, **kwargs: calls.append(args),
            {},
            "real-institution",
            [],
            "a" * 64,
            "b" * 64,
        )
    assert calls == []


def test_retention_requires_and_passes_exact_snapshot_hashes_without_mutation():
    calls = []
    runner = lambda *args, **kwargs: calls.append((args, kwargs))  # noqa: E731
    names = [prefix + "a" * 12 for prefix in ("org-owner-", "org-learner-", "org-reviewer-")]
    with pytest.raises(ValueError):
        verify_retention(runner, {}, "org-browser-" + "a" * 10, names, "a" * 63, "b" * 64)
    assert calls == []
    verify_retention(runner, {}, "org-browser-" + "a" * 10, names, "a" * 64, "b" * 64)
    args, kwargs = calls[0]
    assert args[:3] == ("manage.py", "shell", "-c")
    assert "course.digest==digest(course.data)" in args[3]
    assert (
        "revision.digest==revision.approved_digest==package_digest(package,revision.payload)"
        in args[3]
    )
    assert kwargs["env"]["BROWSER_ORG_COURSE_DIGEST"] == "a" * 64
    assert kwargs["env"]["BROWSER_ORG_PACKAGE_DIGEST"] == "b" * 64
