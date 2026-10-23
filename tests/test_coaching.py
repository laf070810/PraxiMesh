"""Private planning/model workflow checks; provider stubs prove boundaries, not AI quality."""

import copy
import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from coaching import configuration, evidence, hints, plans
from coaching.export import export_user_data
from coaching.models import DailyUsage, ExecutionLease, HintRequest, LearningPlan, SlotLock
from coaching.provider import HintResult, ProviderError
from core.content import digest
from core.models import Draft, KnowledgeNode, Problem, ProblemVersion, Submission
from tests.test_showcase import job

pytestmark = pytest.mark.django_db


def url(name, *args):
    return reverse("coaching:" + name, args=args)


@pytest.fixture
def configured(settings):
    settings.COACHING_ENABLED = True
    settings.COACHING_ENDPOINT = "https://operator.example/v1/chat/completions"
    settings.COACHING_MODEL = "synthetic-model"
    settings.COACHING_TOKEN = "PRIVATE-PROVIDER-CREDENTIAL"
    settings.COACHING_PROVIDER_DESCRIPTION = "Synthetic test provider; never contacts a real model."
    settings.COACHING_ALLOW_LOOPBACK_HTTP = False
    settings.COACHING_CA_FILE = ""
    settings.COACHING_DISABLE_THINKING = False
    settings.COACHING_MAX_TOKENS = 192
    settings.COACHING_DAILY_LIMIT = 5
    settings.COACHING_CONCURRENCY = 1
    SlotLock.objects.get_or_create(pk=1)
    return settings


@pytest.fixture
def version(catalog):
    return Problem.objects.get(pk="sum-positive").current


def preview(user, version, **overrides):
    return hints.preview(
        user, version.pk, question="How can I check the public boundary conditions?", **overrides
    )


def result(text="A synthetic hint, not an AI acceptance claim."):
    return HintResult(text, "synthetic-model", False, 100, 20, 50)


def test_plans_are_private_pinned_skippable_and_never_migrate_source(
    user, other_user, version, client
):
    client.force_login(user)
    response = client.post(url("goals"), {"targets": ["sequences"]})
    assert response.status_code == 302
    plan = LearningPlan.objects.get(user=user)
    snapshot = copy.deepcopy(plan.suggestions)
    assert plan.targets == [
        {"id": "sequences", "digest": digest(KnowledgeNode.objects.get(pk="sequences").data)}
    ]
    first = plan.suggestions[0]["version_id"]
    assert client.post(url("plan_action", plan.pk, "skip"), {"version": first}).status_code == 302
    plan.refresh_from_db()
    assert plan.skipped == [first]
    pinned = ProblemVersion.objects.get(pk=first)
    updated = copy.deepcopy(pinned.data)
    updated["revision"] += 10
    updated["title"] = "NEW CURRENT TITLE"
    replacement = ProblemVersion.objects.create(
        problem=pinned.problem, revision=updated["revision"], digest=digest(updated), data=updated
    )
    Problem.objects.filter(pk=pinned.problem_id).update(current=replacement)
    response = client.get(url("plan_task", plan.pk, first))
    assert response.status_code == 200 and b"NEW CURRENT TITLE" not in response.content
    plan.refresh_from_db()
    assert plan.suggestions == snapshot
    client.force_login(other_user)
    for name, args in [("plan", (plan.pk,)), ("plan_task", (plan.pk, first))]:
        assert client.get(url(name, *args)).status_code == 404
    assert client.post(url("plan_action", plan.pk, "delete")).status_code == 404
    client.force_login(user)
    Problem.objects.filter(pk=pinned.problem_id).update(published=False)
    KnowledgeNode.objects.filter(pk="sequences").update(published=False)
    response = client.get(url("plan", plan.pk))
    assert response.status_code == 200
    assert pinned.data["statement"].encode() not in response.content
    assert client.get(url("plan_task", plan.pk, first)).status_code == 404
    assert client.post(url("plan_action", plan.pk, "close")).status_code == 302
    assert not LearningPlan.objects.get(pk=plan.pk).active
    assert client.post(url("plan_action", plan.pk, "delete")).status_code == 302
    assert not LearningPlan.objects.exists()


def test_goal_limits_hidden_sources_and_replacement_history(user, version):
    with pytest.raises(plans.PlanError):
        plans.create(user, ["functions"] * 9)
    KnowledgeNode.objects.filter(pk="functions").update(published=False)
    with pytest.raises(plans.PlanError):
        plans.create(user, ["functions"])
    first = plans.create(user, ["sequences"])
    second = plans.create(user, ["vectors"])
    first.refresh_from_db()
    assert not first.active and second.active
    assert LearningPlan.objects.filter(user=user).count() == 2


def test_multidimensional_evidence_rejects_fake_run_and_does_not_inflate_diversity(
    user, other_user, version
):
    job(user, version=version)
    job(user, version=version)
    recent = job(user, version=version, passed=0)
    job(user, version=version, mode="run")
    job(user, version=version, result={})
    job(other_user, version=Problem.objects.get(pk="dot-product").current)
    plans.create(user, ["sequences"])
    data = evidence.build(user)
    assert data["summary"]["distinct_problems"] == 1
    assert data["summary"]["versions"] == 1
    assert data["summary"]["accepted_problems"] == 1
    assert data["summary"]["recent_failure_versions"] == 1
    assert data["excluded"] == 1
    card = data["cards"][0]
    assert card["formal_results"] == 3 and card["latest_id"] == recent.pk
    assert data["goals"]
    assert not any("mastery" in key for key in data)
    stale = evidence.build(user, now=timezone.now() + timedelta(days=31))
    assert stale["cards"][0]["stale"]
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    assert evidence.build(user)["cards"] == []


def test_diagnosis_uses_only_known_public_names_and_own_trusted_verdict(
    user, other_user, version, client
):
    record = job(user, version=version, passed=0)
    record.result["public_cases"] = [
        {"name": "PRIVATE-HIDDEN-CASE-SENTINEL", "verdict": "wrong_answer"}
    ]
    record.save(update_fields=["result"])
    client.force_login(user)
    response = client.get(url("diagnosis", record.pk))
    assert response.status_code == 200 and b"PRIVATE-HIDDEN" not in response.content
    assert b"PRIVATE-SOURCE" not in response.content
    client.force_login(other_user)
    assert client.get(url("diagnosis", record.pk)).status_code == 404
    client.force_login(user)
    record.result = {}
    record.save(update_fields=["result"])
    assert client.get(url("diagnosis", record.pk)).context["verified"] is False


def test_default_disabled_does_not_call_or_store_model_requests(user, version, settings, client):
    settings.COACHING_ENABLED = False
    client.force_login(user)
    with patch("coaching.hints.request_hint") as provider:
        assert client.get(url("hint_create", version.pk)).status_code == 200
        assert (
            client.post(
                url("hint_create", version.pk), {"question": "hello", "hint_level": 0}
            ).status_code
            == 400
        )
        provider.assert_not_called()
    assert not HintRequest.objects.exists()


def test_exact_preview_whitelists_public_content_excludes_secrets_and_default_source(
    user, version, configured, client
):
    Draft.objects.create(user=user, version=version, source="PRIVATE-DRAFT-SENTINEL")
    job(user, version=version)
    client.force_login(user)
    response = client.post(
        url("hint_create", version.pk),
        {
            "question": "Explain a public boundary",
            "hint_level": 0,
            "source": "UNSELECTED-PASTED-SOURCE",
        },
    )
    assert response.status_code == 302
    item = HintRequest.objects.get()
    assert item.payload["selected_author_hints"] == []
    assert "learner_source" not in item.payload
    assert item.payload["problem"]["statement"] == version.data["statement"]
    encoded = hints.encode(item.payload)
    for value in [
        "PRIVATE-DRAFT-SENTINEL",
        "PRIVATE-SOURCE-SENTINEL",
        "UNSELECTED-PASTED-SOURCE",
        configured.COACHING_TOKEN,
        version.data["reference"],
    ]:
        assert value not in encoded
    assert "tests" not in item.payload["problem"] and "reference" not in item.payload["problem"]
    page = client.get(url("hint", item.pk))
    assert page.status_code == 200
    assert configured.COACHING_TOKEN.encode() not in page.content
    assert hints.usage(user) == 0
    assert not ExecutionLease.objects.exists()


def test_selected_hints_and_source_require_opt_in_and_send_exact_preview(
    user, version, configured, client
):
    item = preview(
        user, version, hint_level=1, include_source=True, source="def synthetic(): return 1"
    )
    assert item.payload["selected_author_hints"] == version.data["hints"][:1]
    client.force_login(user)
    with patch(
        "coaching.hints.request_hint", return_value=result("<script>malicious()</script>")
    ) as provider:
        assert (
            client.post(url("hint_send", item.pk), {"digest": item.request_digest}).status_code
            == 400
        )
        assert (
            client.post(
                url("hint_send", item.pk), {"digest": item.request_digest, "confirm": "on"}
            ).status_code
            == 302
        )
        assert (
            client.post(
                url("hint_send", item.pk), {"digest": item.request_digest, "confirm": "on"}
            ).status_code
            == 302
        )
        assert provider.call_count == 1
        assert provider.call_args.kwargs["prompt"] == hints.encode(item.payload)
        assert provider.call_args.kwargs["instructions"] == item.instructions
    assert hints.usage(user) == 1
    assert not ExecutionLease.objects.exists()
    response = client.get(url("hint", item.pk))
    assert b"<script>malicious()" not in response.content
    assert b"&lt;script&gt;malicious()&lt;/script&gt;" in response.content
    assert not Submission.objects.filter(user=user).exists()


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("timeout"),
        ProviderError("invalid_response"),
        RuntimeError("PRIVATE-EXCEPTION-CREDENTIAL"),
    ],
)
def test_failure_is_charged_and_sanitized_without_invented_answer(user, version, configured, error):
    item = preview(user, version)
    with patch("coaching.hints.request_hint", side_effect=error):
        returned = hints.send(user, item.pk, item.request_digest)
    assert returned.state == HintRequest.State.FAILED
    assert returned.response == "" and "PRIVATE" not in returned.error_code
    assert hints.usage(user) == 1
    assert not ExecutionLease.objects.exists()


def test_configuration_change_hidden_problem_and_tampered_preview_never_send(
    user, version, configured
):
    item = preview(user, version)
    configured.COACHING_MODEL = "another-operator-model"
    with patch("coaching.hints.request_hint") as provider, pytest.raises(hints.HintError):
        hints.send(user, item.pk, item.request_digest)
    provider.assert_not_called()
    assert hints.usage(user) == 0
    configured.COACHING_MODEL = "synthetic-model"
    item.payload["question"] = "tampered"
    HintRequest.objects.filter(pk=item.pk).update(payload=item.payload)
    with pytest.raises(hints.HintError):
        hints.send(user, item.pk, item.request_digest)
    second = preview(user, version)
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    from django.http import Http404

    with pytest.raises(Http404):
        hints.send(user, second.pk, second.request_digest)
    assert hints.usage(user) == 0


def test_quota_survives_conversation_delete_and_rolls_over_utc_day(user, version, configured):
    configured.COACHING_DAILY_LIMIT = 1
    first = preview(user, version)
    with patch("coaching.hints.request_hint", return_value=result()):
        hints.send(user, first.pk, first.request_digest)
    hints.remove(user, first.pk)
    second = preview(user, version)
    with pytest.raises(hints.HintError, match="quota"):
        hints.send(user, second.pk, second.request_digest)
    assert hints.usage(user) == 1
    tomorrow = timezone.now() + timedelta(days=1)
    with (
        patch("coaching.hints.timezone.now", return_value=tomorrow),
        patch("coaching.hints.request_hint", return_value=result()),
    ):
        assert (
            hints.send(user, second.pk, second.request_digest).state == HintRequest.State.COMPLETE
        )
    assert DailyUsage.objects.filter(user=user).count() == 2


def test_deleted_inflight_conversation_does_not_free_capacity_or_resurrect(
    user, other_user, version, configured
):
    item = preview(user, version)
    other = preview(other_user, version)

    def in_flight(*args, **kwargs):
        assert ExecutionLease.objects.filter(pk=item.pk).exists()
        hints.remove(user, item.pk)
        assert ExecutionLease.objects.filter(pk=item.pk).exists()
        with pytest.raises(hints.HintError, match="busy"):
            hints.send(other_user, other.pk, other.request_digest)
        return result()

    with patch("coaching.hints.request_hint", side_effect=in_flight):
        assert hints.send(user, item.pk, item.request_digest) is None
    assert not HintRequest.objects.filter(pk=item.pk).exists()
    assert hints.usage(user) == 1 and hints.usage(other_user) == 0
    assert not ExecutionLease.objects.exists()


def test_account_deleted_during_model_call_never_recreates_personal_records(
    user, version, configured
):
    item = preview(user, version)
    user_id = user.pk

    def in_flight(*args, **kwargs):
        user.delete()
        assert ExecutionLease.objects.filter(pk=item.pk).exists()
        return result()

    with patch("coaching.hints.request_hint", side_effect=in_flight):
        assert hints.send(user, item.pk, item.request_digest) is None
    assert not HintRequest.objects.filter(user_id=user_id).exists()
    assert not DailyUsage.objects.filter(user_id=user_id).exists()
    assert not ExecutionLease.objects.exists()


def test_expired_lease_released_but_prior_usage_stays_charged(
    user, other_user, version, configured
):
    first, second = preview(user, version), preview(other_user, version)
    earlier = timezone.now() - timedelta(seconds=40)
    hints._reserve(user, first.pk, first.request_digest, configuration.current(), earlier)
    assert hints.usage(user) == 1
    with patch("coaching.hints.request_hint", return_value=result()):
        hints.send(other_user, second.pk, second.request_digest)
    first.refresh_from_db()
    assert first.state == HintRequest.State.FAILED and first.error_code == "interrupted"
    assert hints.usage(user) == 1 and hints.usage(other_user) == 1


def test_private_export_delete_and_withdrawn_context(user, other_user, version, configured, client):
    plan = plans.create(user, ["sequences"])
    item = preview(user, version, include_source=True, source="MY-OPTED-IN-SOURCE")
    with patch("coaching.hints.request_hint", return_value=result()):
        hints.send(user, item.pk, item.request_digest)
    assert export_user_data(other_user) == {"plans": [], "hints": [], "usage": []}
    client.force_login(other_user)
    assert client.get(url("hint", item.pk)).status_code == 404
    assert (
        client.post(
            url("hint_send", item.pk), {"digest": item.request_digest, "confirm": "on"}
        ).status_code
        == 404
    )
    assert client.post(url("hint_delete", item.pk)).status_code == 404
    client.force_login(user)
    exported = client.post(reverse("export_account")).json()["coaching"]
    assert exported["hints"][0]["payload"]["learner_source"] == "MY-OPTED-IN-SOURCE"
    assert exported["plans"][0]["id"] == str(plan.pk)
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    exported = export_user_data(user)
    assert exported["hints"][0]["payload"] is None
    assert exported["hints"][0]["response"] is None
    response = client.get(url("hint", item.pk))
    assert version.data["statement"].encode() not in response.content
    assert b"MY-OPTED-IN-SOURCE" not in response.content
    assert (
        client.post(
            reverse("delete_account"), {"password": "Test-passphrase-9621", "confirm": "DELETE"}
        ).status_code
        == 200
    )
    assert (
        not LearningPlan.objects.exists()
        and not HintRequest.objects.exists()
        and not DailyUsage.objects.exists()
    )


def test_login_post_csrf_and_unconfigured_metadata_safety(user, version, configured, client):
    item = preview(user, version)
    assert client.get(url("index")).status_code == 302
    client.force_login(user)
    assert client.get(url("hint_send", item.pk)).status_code == 405
    assert client.get(url("hint_delete", item.pk)).status_code == 405
    csrf = Client(enforce_csrf_checks=True)
    csrf.force_login(user)
    assert (
        csrf.post(
            url("hint_send", item.pk), {"confirm": "on", "digest": item.request_digest}
        ).status_code
        == 403
    )
    assert configured.COACHING_TOKEN not in json.dumps(export_user_data(user))
    assert configured.COACHING_TOKEN not in repr(configuration.current())


def test_expired_same_request_does_not_dispatch_twice(user, version, configured):
    item = preview(user, version)
    hints._reserve(
        user,
        item.pk,
        item.request_digest,
        configuration.current(),
        timezone.now() - timedelta(seconds=40),
    )
    with patch("coaching.hints.request_hint") as provider:
        returned = hints.send(user, item.pk, item.request_digest)
        provider.assert_not_called()
    assert returned.state == HintRequest.State.FAILED
    assert hints.usage(user) == 1


def test_complete_english_pages_and_actions(user, version, configured, client):
    client.force_login(user)
    client.cookies["django_language"] = "en"
    for name, text in [
        ("index", "Planning and tutoring"),
        ("goals", "Set private goals"),
        ("analysis", "Learning evidence by dimension"),
    ]:
        page = client.get(url(name))
        assert page.status_code == 200 and text.encode() in page.content
    plan = plans.create(user, ["sequences"])
    assert b"My plan with exact problem versions" in client.get(url("plan", plan.pk)).content
    page = client.get(url("hint_create", version.pk))
    assert b"Optional AI hint: preview first" in page.content
    item = preview(user, version)
    assert b"My private tutoring conversation" in client.get(url("hint", item.pk)).content
    record = job(user, version=version)
    assert b"Evidence-based result review" in client.get(url("diagnosis", record.pk)).content
    record.environment.update(python="3.14.9", wasmtime="50.0.1")
    record.save(update_fields=["environment"])
    assert client.get(reverse("submission", args=[record.pk])).status_code == 200


def test_goal_title_uses_recorded_source_snapshot_after_current_content_update(user, version):
    plan = plans.create(user, ["sequences"])
    original = KnowledgeNode.objects.get(pk="sequences")
    title = original.title
    updated = copy.deepcopy(original.data)
    updated["title"] = "CHANGED-GOAL-TITLE"
    KnowledgeNode.objects.filter(pk=original.pk).update(data=updated, title=updated["title"])
    shown = plans.present(plan)["targets"][0]
    assert shown["node"]["title"] == title and shown["changed"]
    KnowledgeNode.objects.filter(pk=original.pk).update(published=False)
    assert plans.present(plan)["targets"][0]["node"] is None


def test_embedded_trust_configuration_is_pinned_without_token(configured):
    configured.COACHING_CA_PEM = "SYNTHETIC-TRUST-TEXT"
    first = configuration.current()
    assert first.provider.ca_pem == "SYNTHETIC-TRUST-TEXT"
    configured.COACHING_CA_PEM = "DIFFERENT-SYNTHETIC-TRUST-TEXT"
    assert configuration.current().identity != first.identity
    second_identity = configuration.current().identity
    configured.COACHING_TOKEN = "ROTATED-SECRET-TOKEN"
    assert configuration.current().identity == second_identity
