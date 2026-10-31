"""Explicit publication of synthetic persisted GPU evidence; no GPU execution."""

import copy

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client
from django.utils import translation
from test_showcase import enable, next_version, share, shared, url
from test_showcase import job as cpu_job

from core.models import ProblemVersion
from showcase import services
from showcase.forms import ShareForm
from showcase.models import PublicProfile, SharedResult
from tests.gpu_evidence_fixtures import job_fixture, rebind, version_fixture

pytestmark = pytest.mark.django_db


@pytest.fixture
def version():
    return version_fixture()


@pytest.fixture
def item(user, version):
    return job_fixture(user, version)


def measured(user, version, median):
    item = job_fixture(user, version)
    item.result["performance"].update(samples_ms=[median] * 5, median_batch_ms=median)
    item.save(update_fields=["result"])
    return item


def test_gpu_preview_requires_distinct_explicit_consent_and_never_copies_private_fields(
    logged_client, user, item
):
    assert not SharedResult.objects.exists()
    preview = logged_client.get(url("share", item.pk))
    assert preview.status_code == 200
    assert not PublicProfile.objects.exists()
    assert not list(Client().get(url("boards")).context["page"])
    enable(logged_client, user)
    preview = logged_client.get(url("share", item.pk))
    assert "这次正式 GPU 计时及评测环境指纹" in preview.content.decode()
    assert not preview.context["form"]["confirm_public"].value()
    assert share(logged_client, item, confirm_public="").status_code == 400
    entry = shared(logged_client, item)
    assert entry.metric_kind == SharedResult.Metric.GPU_BATCH
    guest = Client()
    for endpoint in (
        url("entry", entry.pk),
        url("profile", user.username),
        url("boards"),
        url("board", item.version_id, item.evaluation_fingerprint),
    ):
        response = guest.get(endpoint)
        assert response.status_code == 200 and "no-store" in response["Cache-Control"]
        body = response.content.decode()
        for secret in (
            "PRIVATE_LEARNER_SOURCE_SENTINEL",
            "PRIVATE_REFERENCE_SENTINEL",
            "PROTECTED_CASE_SENTINEL",
            item.environment["target"]["uuid"],
            item.environment["target"]["model"],
            str(item.pk),
            "samples_ms",
            "compile_initialization_ms",
        ):
            assert secret not in body
    card = guest.get(url("entry", entry.pk)).context["entry"]
    assert card["score_ms"] == "3.000" and card["score_us"] == 3000
    assert not {"source", "environment", "result", "performance", "assessment"}.intersection(card)
    assert (
        card["warmup_batches"] == 2
        and card["sample_batches"] == 5
        and card["batch_repetitions"] == 16
    )
    assert (
        services.export_user_data(user)["shared_results"][0]["metric_kind"]
        == SharedResult.Metric.GPU_BATCH
    )


def test_legacy_scope_does_not_automatically_disclose_gpu_timing(logged_client, user, item):
    enable(logged_client, user)
    entry = SharedResult.objects.create(submission=item, is_public=True)
    assert entry.metric_kind == SharedResult.Metric.SEQUENTIAL
    assert Client().get(url("entry", entry.pk)).status_code == 404
    assert not list(Client().get(url("boards")).context["page"])
    assert not logged_client.get(url("mine")).context["rows"][0]["public_visible"]
    response = share(logged_client, item)
    assert response.status_code == 302
    entry.refresh_from_db()
    assert entry.metric_kind == SharedResult.Metric.GPU_BATCH
    assert Client().get(url("entry", entry.pk)).status_code == 200
    assert logged_client.get(url("mine")).context["rows"][0]["public_visible"]


@pytest.mark.parametrize(
    "mode,verdict,batch_failed",
    [
        ("run", "accepted", False),
        ("submit", "wrong_answer", False),
        ("submit", "wrong_answer", True),
        ("submit", "runtime_error", False),
    ],
)
def test_only_fully_correct_formal_gpu_results_can_be_shared(
    logged_client, user, version, mode, verdict, batch_failed
):
    enable(logged_client, user)
    item = job_fixture(user, version, mode=mode, verdict=verdict, batch_failed=batch_failed)
    assert logged_client.get(url("share", item.pk)).status_code == 400
    assert share(logged_client, item).status_code == 400
    assert not SharedResult.objects.exists()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda j: j.result["performance"].update(median_batch_ms=0),
        lambda j: j.result["performance"].update(median_batch_ms=3.1),
        lambda j: j.result["performance"].update(samples_ms=[1, 2, 3, 4]),
        lambda j: j.result["performance"].update(samples_ms=[1, True, 3, 4, 5]),
        lambda j: j.result["performance"].update(measurement="guest-reported"),
        lambda j: j.result["performance"].update(batch_repetitions=1),
        lambda j: j.result.update(compile_initialization_ms=0),
        lambda j: j.result.update(passed=1),
        lambda j: j.result.update(private_field="not public"),
        lambda j: setattr(j, "evaluation_fingerprint", "b" * 64),
        lambda j: setattr(j, "status", "running"),
    ],
)
def test_later_invalid_evidence_disappears_from_every_public_surface(
    logged_client, user, item, mutation
):
    enable(logged_client, user)
    entry = shared(logged_client, item)
    fingerprint = item.evaluation_fingerprint
    mutation(item)
    item.save()
    guest = Client()
    assert guest.get(url("entry", entry.pk)).status_code == 404
    assert guest.get(url("board", item.version_id, fingerprint)).status_code == 404
    assert not list(guest.get(url("boards")).context["page"])
    assert not list(guest.get(url("profile", user.username)).context["page"])


def test_changed_immutable_content_digest_cannot_keep_a_public_gpu_score(logged_client, user, item):
    enable(logged_client, user)
    entry = shared(logged_client, item)
    changed = copy.deepcopy(item.version.data)
    changed["statement"] += " Changed after publication"
    ProblemVersion.objects.filter(pk=item.version_id).update(data=changed)
    assert Client().get(url("entry", entry.pk)).status_code == 404
    assert share(logged_client, item).status_code == 400


@pytest.mark.parametrize(
    "field,value", [("budget_state", "reserved"), ("execution_started_at", None)]
)
def test_invalid_gpu_reservation_is_rejected_by_consumer_and_database(item, field, value):
    setattr(item, field, value)
    with pytest.raises(services.SharingError):
        services.result_metrics(item)
    with pytest.raises(IntegrityError), transaction.atomic():
        item.save()


@pytest.mark.parametrize(
    "median,display", [(2.3454, "2.345"), (2.3455, "2.346"), (0.0001, "0.000"), (0.0005, "0.001")]
)
def test_display_and_ties_use_declared_decimal_rounding(user, version, median, display):
    metrics = services.result_metrics(measured(user, version, median))
    assert metrics["score_ms"] == display


def test_best_public_timing_only_equal_ranks_and_withdrawal(
    logged_client, user, other_user, version
):
    second_client = Client()
    second_client.force_login(other_user)
    third = get_user_model().objects.create_user(username="third-gpu")
    third_client = Client()
    third_client.force_login(third)
    for client, owner in (
        (logged_client, user),
        (second_client, other_user),
        (third_client, third),
    ):
        enable(client, owner)
    best = measured(user, version, 2.3455)
    first = shared(logged_client, best)
    shared(logged_client, measured(user, version, 4))
    measured(user, version, 0.1)  # Private best must not change the public board.
    shared(second_client, measured(other_user, version, 2.3456))
    shared(third_client, measured(third, version, 3))
    endpoint = url("board", version.pk, best.evaluation_fingerprint)
    rows = list(Client().get(endpoint).context["page"])
    assert [row["rank"] for row in rows] == [1, 1, 3]
    assert [row["score_ms"] for row in rows] == ["2.346", "2.346", "3.000"]
    assert (
        logged_client.post(url("withdraw", first.pk), {"revision": first.revision}).status_code
        == 302
    )
    rows = list(Client().get(endpoint).context["page"])
    assert [(row["username"], row["score_ms"], row["rank"]) for row in rows] == [
        (other_user.username, "2.346", 1),
        (third.username, "3.000", 2),
        (user.username, "4.000", 3),
    ]


def test_gpu_version_device_and_cpu_groups_stay_separate(catalog, logged_client, user, version):
    enable(logged_client, user)
    first = job_fixture(user, version)
    shared(logged_client, first)
    second = job_fixture(user, version)
    second.environment["target"]["uuid"] = "GPU-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    rebind(second)
    second.save(update_fields=["environment", "evaluation_fingerprint"])
    shared(logged_client, second)
    newer = next_version(version)
    shared(logged_client, job_fixture(user, newer))
    shared(logged_client, cpu_job(user))
    groups = list(Client().get(url("boards")).context["page"])
    assert len(groups) == 4
    assert sum(group["metric_kind"] == SharedResult.Metric.GPU_BATCH for group in groups) == 3
    assert sum(group["historical"] for group in groups) == 2
    with pytest.raises(services.SharingError, match="不能混排"):
        services.ranked_results(services.public_entries())


def test_cross_owner_cannot_confirm_new_scope_or_withdraw_and_close_hides_all(
    logged_client, user, other_user, item
):
    profile = enable(logged_client, user)
    entry = shared(logged_client, item)
    outsider = Client()
    outsider.force_login(other_user)
    assert outsider.get(url("share", item.pk)).status_code == 404
    assert share(outsider, item).status_code == 404
    assert outsider.post(url("withdraw", entry.pk), {"revision": entry.revision}).status_code == 404
    assert logged_client.post(url("close"), {"revision": profile.revision}).status_code == 302
    assert Client().get(url("entry", entry.pk)).status_code == 404
    assert not list(Client().get(url("boards")).context["page"])


def test_gpu_share_preview_and_rules_are_translated(logged_client, user, item):
    enable(logged_client, user)
    with translation.override("en"):
        form = ShareForm(metric_kind=SharedResult.Metric.GPU_BATCH)
        assert "official GPU timing" in str(form.fields["confirm_public"].label)
    logged_client.cookies["django_language"] = "en"
    response = logged_client.get(url("share", item.pk))
    body = response.content.decode()
    assert "Median batch round trip" in body
    assert "rounded half up to 0.001 ms" in body
    assert "kernel time alone" in body
