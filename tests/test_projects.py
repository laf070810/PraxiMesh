"""Project import, version binding and private evidence contracts, not sandbox execution."""

import copy
import hashlib
import io
import json
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models.deletion import ProtectedError
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from core.content import ContentError, digest
from core.models import Problem, ProblemVersion, Submission
from projects.catalog import import_package, load_package, validate_package
from projects.models import Project, ProjectRevision, ProjectStage


def url(name, *args):
    return reverse(f"projects:{name}", args=args)


def current(slug):
    return Problem.objects.select_related("current").get(pk=slug).current


@pytest.fixture
def project_package(catalog):
    # Distinct existing tasks are metadata fixtures here. The shipped sensor
    # project uses a separate, genuinely integrated stdio task and sandbox suite.
    stages = []
    for index, slug in enumerate(("population-stats", "range-sums", "trapezoid-samples")):
        version = current(slug)
        stages.append(
            {
                "id": f"stage-{index}",
                "title": f"Stage {index}",
                "description": f"Component {index} of a reviewed integration plan.",
                "problem": slug,
                "problem_revision": version.revision,
                "problem_digest": version.digest,
                "recommended": ["stage-0", "stage-1"] if index == 2 else [],
                "final": index == 2,
            }
        )
    return {
        "schema_version": 1,
        "projects": [
            {
                "id": "fixture-project",
                "revision": 1,
                "title": "Reviewed test project",
                "summary": "Import and view contract fixture.",
                "integration_goal": "Combine reviewed components in the final task.",
                "stages": stages,
            }
        ],
    }


@pytest.fixture
def published(project_package):
    import_package(project_package)
    return Project.objects.select_related("current").get(pk="fixture-project")


def record(user, version, *, mode="submit", status="finished", verdict="accepted", **overrides):
    total = len(version.data["tests"])
    now = timezone.now()
    return Submission.objects.create(
        user=user,
        version=version,
        source="PRIVATE-PROJECT-SOURCE",
        source_digest="a" * 64,
        mode=mode,
        status=status,
        verdict=verdict,
        **{
            "evaluation_fingerprint": hashlib.sha256(
                f"wasi:{'b' * 64}:{'c' * 64}".encode()
            ).hexdigest(),
            "environment": {
                "policy": "wasi-python-function-v1",
                "bundle": "b" * 64,
                "policy_digest": "c" * 64,
            },
            "started_at": now - timedelta(seconds=1),
            "finished_at": now,
            "result": {"verdict": verdict, "passed": total, "total": total},
            **overrides,
        },
    )


def next_problem_revision(version):
    data = copy.deepcopy(version.data)
    data["revision"] += 1
    data["statement"] = "Current revision statement"
    item = ProblemVersion.objects.create(
        problem=version.problem, revision=data["revision"], digest=digest(data), data=data
    )
    Problem.objects.filter(pk=version.problem_id).update(current=item)
    return item


def test_owner_import_check_then_publish_is_idempotent_and_preserves_pins(
    project_package, tmp_path
):
    path = tmp_path / "project.json"
    path.write_text(json.dumps(project_package))
    call_command("project_import", str(path), check=True, stdout=io.StringIO())
    assert not Project.objects.exists()
    call_command("project_import", str(path), stdout=io.StringIO())
    call_command("project_import", str(path), stdout=io.StringIO())
    assert Project.objects.count() == ProjectRevision.objects.count() == 1
    stages = list(ProjectStage.objects.select_related("problem_version"))
    assert len(stages) == 3
    assert [stage.problem_version.digest for stage in stages] == [
        stage["problem_digest"] for stage in project_package["projects"][0]["stages"]
    ]
    with pytest.raises(ProtectedError):
        stages[0].problem_version.delete()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(schema_version=True),
        lambda p: p.update(unknown="no"),
        lambda p: p["projects"].append(copy.deepcopy(p["projects"][0])),
        lambda p: p["projects"][0].update(revision=True),
        lambda p: p["projects"][0].update(revision=2**31),
        lambda p: p["projects"][0].update(title="x" * 161),
        lambda p: p["projects"][0].update(summary=" " * 2000 + "x"),
        lambda p: p["projects"][0].update(integration_goal="\0"),
        lambda p: p["projects"][0]["stages"][0].update(final="false"),
        lambda p: p["projects"][0]["stages"][0].update(problem_revision=False),
        lambda p: p["projects"][0]["stages"][0].update(problem_digest="invalid"),
        lambda p: p["projects"][0]["stages"][1].update(id="stage-0"),
        lambda p: p["projects"][0]["stages"][1].update(problem="population-stats"),
        lambda p: p["projects"][0]["stages"][0].update(recommended=["stage-2"]),
        lambda p: p["projects"][0]["stages"][1].update(recommended=["stage-0", "stage-0"]),
        lambda p: p["projects"][0]["stages"][2].update(recommended=["stage-1"]),
        lambda p: p["projects"][0]["stages"][2].update(final=False),
        lambda p: p["projects"][0]["stages"][0].update(final=True),
    ],
)
def test_invalid_project_graph_and_metadata_fail_without_partial_rows(project_package, mutation):
    mutation(project_package)
    with pytest.raises(ContentError):
        import_package(project_package)
    assert not Project.objects.exists() and not ProjectStage.objects.exists()


def test_transitive_recommended_components_are_allowed(project_package):
    stages = project_package["projects"][0]["stages"]
    stages[1]["recommended"] = ["stage-0"]
    stages[2]["recommended"] = ["stage-1"]
    validate_package(project_package)


@pytest.mark.parametrize(
    "raw",
    [
        '{"schema_version":1,"schema_version":1,"projects":[]}',
        '{"schema_version":NaN,"projects":[]}',
        "[]",
        "not JSON",
        " " * 1_000_001,
    ],
)
def test_json_load_rejects_duplicates_nonfinite_and_oversize(tmp_path, raw):
    path = tmp_path / "invalid.json"
    path.write_text(raw)
    with pytest.raises(ContentError):
        load_package(path)


@pytest.mark.parametrize("change", ["missing", "digest", "unpublished", "no-current"])
def test_project_pins_must_resolve_to_available_exact_content(project_package, change):
    stage = project_package["projects"][0]["stages"][0]
    if change == "missing":
        stage["problem_revision"] = 100
    elif change == "digest":
        stage["problem_digest"] = "f" * 64
    elif change == "unpublished":
        Problem.objects.filter(pk=stage["problem"]).update(published=False)
    else:
        Problem.objects.filter(pk=stage["problem"]).update(current=None)
    with pytest.raises(ContentError):
        import_package(project_package)
    assert not Project.objects.exists()


def test_revision_cannot_be_rewritten_rolled_back_or_silently_withdraw_others(
    project_package, published
):
    changed = copy.deepcopy(project_package)
    changed["projects"][0]["summary"] = "Modified immutable text"
    with pytest.raises(ContentError):
        import_package(changed)
    changed["projects"][0]["revision"] = 2
    import_package(changed)
    with pytest.raises(ContentError):
        import_package(project_package)
    different = copy.deepcopy(project_package)
    different["projects"][0]["id"] = "second-project"
    import_package(different)
    published.refresh_from_db()
    assert published.published and published.current.revision == 2
    assert ProjectRevision.objects.filter(project=published).count() == 2
    assert Project.objects.get(pk="second-project").published


def test_package_import_is_atomic_when_a_later_plan_has_bad_pin(project_package):
    bad = copy.deepcopy(project_package["projects"][0])
    bad["id"] = "second-project"
    bad["stages"][0]["problem_digest"] = "f" * 64
    project_package["projects"].append(bad)
    with pytest.raises(ContentError):
        import_package(project_package)
    assert not Project.objects.exists() and not ProjectStage.objects.exists()


def test_stored_stage_corruption_does_not_pass_idempotent_import(project_package, published):
    ProjectStage.objects.filter(revision=published.current, key="stage-0").update(data={})
    with pytest.raises(ContentError, match="bindings"):
        import_package(project_package)


def test_public_stage_is_freely_accessible_and_uses_only_public_problem_fields(published, client):
    endpoint = url("stage", published.pk, 1, "stage-2")
    response = client.get(endpoint)
    assert response.status_code == 200 and "no-store" in response["Cache-Control"]
    text = response.content.decode()
    assert "进入练习与提交" in text
    assert "不会阻止直接完成当前阶段" in text
    version = current("trapezoid-samples")
    assert version.data["reference"] not in text
    for case in version.data["tests"]:
        if not case["public"]:
            assert case["name"] not in text
    assert not {"reference", "tests", "data"}.intersection(response.context["problem"])
    assert client.post(endpoint, {"completed": True}).status_code == 405
    assert client.post(url("detail", published.pk), {"published": True}).status_code == 405
    assert client.post(url("index"), {}).status_code == 405


def test_progress_counts_only_requesting_user_exact_formal_finished_accepted(
    published, logged_client, user, other_user
):
    first, second, third = [
        stage.problem_version
        for stage in published.current.stages.select_related("problem_version")
    ]
    accepted = record(user, first)
    record(other_user, second)
    record(user, second, mode="run")
    record(user, second, status="queued")
    record(user, second, status="running")
    record(user, second, verdict="internal_error")
    record(user, second, verdict="wrong_answer")
    record(user, third, started_at=None)
    record(user, third, finished_at=None)
    record(user, third, result={"verdict": "accepted", "passed": True, "total": True})
    record(user, third, environment={})
    record(user, third, evaluation_fingerprint="0" * 64)
    response = logged_client.get(url("detail", published.pk))
    assert response.context["completed"] == 1 and not response.context["all_complete"]
    assert response.context["stages"][0]["evidence"]["id"] == accepted.pk
    assert "PRIVATE-PROJECT-SOURCE" not in response.content.decode()
    visitor = Client().get(url("detail", published.pk))
    assert not any(stage["evidence"] for stage in visitor.context["stages"])
    assert str(accepted.pk) not in visitor.content.decode()
    logged_client.force_login(other_user)
    response = logged_client.get(url("detail", published.pk))
    assert response.context["completed"] == 1
    assert response.context["stages"][1]["evidence"]
    assert str(accepted.pk) not in response.content.decode()


def test_final_can_be_completed_before_components_without_unlock_gate(
    published, logged_client, user
):
    final = published.current.stages.get(key="stage-2")
    record(user, final.problem_version)
    response = logged_client.get(url("stage", published.pk, 1, "stage-2"))
    assert response.status_code == 200 and response.context["stage"]["evidence"]
    assert "进入练习与提交" in response.content.decode()
    progress = logged_client.get(url("detail", published.pk))
    assert progress.context["completed"] == 1 and not progress.context["all_complete"]
    for stage in published.current.stages.exclude(key="stage-2"):
        record(user, stage.problem_version)
    progress = logged_client.get(url("detail", published.pk))
    assert progress.context["all_complete"] and progress.context["completed"] == 3


def test_fixed_version_history_survives_new_current_problem_and_project_revision(
    project_package, published, logged_client, user
):
    original = current("population-stats")
    accepted = record(user, original)
    replacement = next_problem_revision(original)
    response = logged_client.get(url("detail", published.pk))
    assert response.context["completed"] == 1
    assert response.context["stages"][0]["archived"]
    stage = logged_client.get(url("stage", published.pk, 1, "stage-0"))
    assert "归档版本不再接受提交" in stage.content.decode()
    assert "其结果不计入本阶段" in stage.content.decode()
    changed = copy.deepcopy(project_package)
    changed["projects"][0]["revision"] = 2
    changed["projects"][0]["stages"][0].update(
        problem_revision=2, problem_digest=replacement.digest
    )
    import_package(changed)
    response = logged_client.get(url("detail", published.pk))
    assert response.context["project"]["revision"] == 2
    assert response.context["completed"] == 0
    history = logged_client.get(url("revision", published.pk, 1))
    assert history.context["completed"] == 1 and history.context["project"]["historical"]
    assert str(accepted.pk) in history.content.decode()
    record(user, replacement)
    assert logged_client.get(url("detail", published.pk)).context["completed"] == 1
    assert logged_client.get(url("revision", published.pk, 1)).context["completed"] == 1


def test_newer_problem_result_does_not_count_for_older_pinned_stage(published, logged_client, user):
    newer = next_problem_revision(current("population-stats"))
    record(user, newer)
    assert logged_client.get(url("detail", published.pk)).context["completed"] == 0


def test_problem_withdrawal_blocks_stage_content_but_keeps_private_project_history(
    published, logged_client, user
):
    version = current("population-stats")
    record(user, version)
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    response = logged_client.get(url("detail", published.pk))
    assert response.context["completed"] == 1
    assert not response.context["stages"][0]["available"]
    assert version.data["title"] not in response.content.decode()
    assert url("stage", published.pk, 1, "stage-0") not in response.content.decode()
    final = logged_client.get(url("stage", published.pk, 1, "stage-2"))
    assert url("stage", published.pk, 1, "stage-0") not in final.content.decode()
    assert Client().get(url("stage", published.pk, 1, "stage-0")).status_code == 404
    assert logged_client.get(url("stage", published.pk, 1, "stage-0")).status_code == 404


def test_owner_project_withdrawal_preserves_versions_and_submissions(published, user):
    record(user, current("population-stats"))
    call_command("project_withdraw", published.pk, stdout=io.StringIO())
    assert ProjectRevision.objects.count() == 1 and Submission.objects.count() == 1
    client = Client()
    assert not list(client.get(url("index")).context["page"])
    assert client.get(url("detail", published.pk)).status_code == 404
    assert client.get(url("revision", published.pk, 1)).status_code == 404
    assert client.get(url("stage", published.pk, 1, "stage-2")).status_code == 404
    with pytest.raises(CommandError):
        call_command("project_withdraw", "missing-project", stdout=io.StringIO())


def test_project_text_is_escaped_and_bad_or_cross_project_versions_do_not_leak(
    project_package, client
):
    plan = project_package["projects"][0]
    plan["title"] = "<script>plan</script>"
    plan["integration_goal"] = "<img src=x>\nExplain"
    plan["stages"][0]["description"] = "<script>stage</script>"
    import_package(project_package)
    response = client.get(url("detail", plan["id"]))
    assert "<script>plan</script>" not in response.content.decode()
    assert "&lt;script&gt;plan&lt;/script&gt;" in response.content.decode()
    assert "&lt;img src=x&gt;<br>Explain" in response.content.decode()
    assert client.get(url("revision", plan["id"], 2**40)).status_code == 404
    assert client.get(url("revision", plan["id"], 2)).status_code == 404
    assert client.get(url("stage", plan["id"], 1, "missing")).status_code == 404


def test_stdio_stage_displays_public_stdin_and_expected_without_reference(project_package, client):
    original = current("trapezoid-samples")
    data = copy.deepcopy(original.data)
    data.pop("function")
    data.update(
        evaluator="python-stdio-v1", output_format="json", reference="PRIVATE-STDIO-REFERENCE"
    )
    data["tests"] = [
        {"name": "Example", "public": True, "stdin": '{"values":[1]}\n', "expected": {"mean": 1}},
        {"name": "HIDDEN-CASE-NAME", "public": False, "stdin": "HIDDEN-INPUT", "expected": 2},
    ]
    # Trusted fixture setup for the future evaluator; no program is executed here.
    ProblemVersion.objects.filter(pk=original.pk).update(data=data, digest=digest(data))
    project_package["projects"][0]["stages"][-1]["problem_digest"] = digest(data)
    import_package(project_package)
    response = client.get(url("stage", "fixture-project", 1, "stage-2"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "python-stdio-v1" in body and "values" in body and "mean" in body
    assert (
        "PRIVATE-STDIO-REFERENCE" not in body
        and "HIDDEN-CASE-NAME" not in body
        and "HIDDEN-INPUT" not in body
    )


def test_directory_and_revision_history_are_reachably_paginated(project_package, client):
    for index in range(21):
        plan = copy.deepcopy(project_package)
        plan["projects"][0]["id"] = f"fixture-{index:02}"
        import_package(plan)
    first, second = client.get(url("index")), client.get(url("index"), {"page": 2})
    assert len(first.context["page"]) == 20 and len(second.context["page"]) == 1
    assert "?page=2" in first.content.decode()
    plan = copy.deepcopy(project_package)
    for revision in range(1, 22):
        plan["projects"][0]["revision"] = revision
        import_package(plan)
    first = client.get(url("detail", "fixture-project"))
    second = client.get(url("detail", "fixture-project"), {"page": 2})
    assert len(first.context["history"]) == 20 and len(second.context["history"]) == 1
    assert "?page=2" in first.content.decode()
    assert second.context["history"][0].revision == 1
