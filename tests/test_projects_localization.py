"""Versioned project text projections; no fabricated judging evidence is required."""

import copy

import pytest
from django.utils import translation

from core.content import digest
from core.models import Problem, ProblemVersion
from localization.models import ProblemTranslation
from localization.packages import import_translations, load_translations
from projects.catalog import import_package
from projects.models import Project


@pytest.fixture
def localized_project(catalog, settings, client):
    import_translations(load_translations(settings.CONTENT_ROOT / "en.json"))
    stages = []
    for position, slug in enumerate(("sum-positive", "argmax-first")):
        version = Problem.objects.get(pk=slug).current
        stages.append(
            {
                "id": f"stage-{position}",
                "title": f"中文阶段 {position}",
                "description": "中文说明 <script>privateStageText=true</script>",
                "problem": slug,
                "problem_revision": version.revision,
                "problem_digest": version.digest,
                "recommended": ["stage-0"] if position else [],
                "final": bool(position),
            }
        )
    import_package(
        {
            "schema_version": 1,
            "projects": [
                {
                    "id": "localized-project",
                    "revision": 1,
                    "title": "中文项目计划",
                    "summary": "中文项目摘要",
                    "integration_goal": "中文集成要求",
                    "stages": stages,
                }
            ],
        }
    )
    client.defaults["HTTP_ACCEPT_LANGUAGE"] = "en-US"
    yield Project.objects.select_related("current").get(pk="localized-project")
    translation.deactivate()


def stage_url(project, position=0):
    return f"/projects/{project.pk}/revisions/1/stages/stage-{position}/"


@pytest.mark.parametrize("locale", ["en-US", "zh-CN"])
def test_project_metadata_is_unchanged_and_source_language_is_explicit(
    localized_project, client, locale
):
    revision = localized_project.current
    before = copy.deepcopy(revision.data), revision.digest
    for endpoint in (
        "/projects/",
        f"/projects/{localized_project.pk}/",
        stage_url(localized_project),
    ):
        response = client.get(endpoint, HTTP_ACCEPT_LANGUAGE=locale)
        assert response.status_code == 200
        body = response.content.decode()
        assert "中文项目计划" in body
        assert "project-source-language" in body
        assert "<script>privateStageText=true</script>" not in body
        if locale == "en-US":
            assert "Engineering projects" in body
            assert (
                "Project plans and stage descriptions are currently available in the Chinese original."
                in body
            )
        else:
            assert "项目计划和阶段说明目前提供中文原文" in body
    revision.refresh_from_db()
    assert (revision.data, revision.digest) == before


def test_exact_version_translation_and_public_example_projection(localized_project, client):
    revision = localized_project.current
    version = revision.stages.get(key="stage-0").problem_version
    translated = ProblemTranslation.objects.get(version=version)
    response = client.get(stage_url(localized_project))
    body = response.content.decode()
    assert response.status_code == 200 and response["Content-Language"] == "en"
    assert "Sum only positive values" in body and f"Pinned problem v{version.revision}" in body
    assert translated.fields["statement"] in response.context["problem"]["statement"]
    assert [case["name"] for case in response.context["problem"]["examples"]] == translated.fields[
        "examples"
    ]
    assert (
        not {"reference", "tests", "source_search", "data", "starter"}
        & response.context["problem"].keys()
    )
    assert version.data["reference"] not in body
    for case in version.data["tests"]:
        if not case["public"]:
            assert case["name"] not in body
    detail = client.get(f"/projects/{localized_project.pk}/")
    assert detail.context["stages"][0]["problem_title"] == translated.fields["title"]
    assert detail.context["stages"][0]["problem_language"] == "en"
    assert not detail.context["stages"][0]["problem_translation_fallback"]


def test_missing_translation_marks_chinese_problem_fallback(localized_project, client):
    response = client.get(stage_url(localized_project, 1))
    assert response.status_code == 200
    body = response.content.decode()
    assert "No English translation is available for this version." in body
    assert response.context["problem"]["content_language"] == "zh-hans"
    assert response.context["problem"]["translation_fallback"]
    detail = client.get(f"/projects/{localized_project.pk}/")
    assert detail.context["stages"][1]["problem_translation_fallback"]
    assert "Chinese original" in detail.content.decode()


def test_new_current_problem_does_not_replace_pinned_translation(localized_project, client):
    old = localized_project.current.stages.get(key="stage-0").problem_version
    new_data = copy.deepcopy(old.data)
    new_data.update(
        revision=old.revision + 1, title="NEW-CURRENT-TITLE", statement="NEW-CURRENT-STATEMENT"
    )
    new = ProblemVersion.objects.create(
        problem=old.problem, revision=old.revision + 1, data=new_data, digest=digest(new_data)
    )
    Problem.objects.filter(pk=old.problem_id).update(current=new)
    response = client.get(stage_url(localized_project))
    body = response.content.decode()
    assert "Sum only positive values" in body
    assert "NEW-CURRENT-TITLE" not in body and "NEW-CURRENT-STATEMENT" not in body
    assert "archived versions no longer accept submissions" in body
    assert "Practice and submit" not in body
    ProblemTranslation.objects.filter(version=old).update(published=False)
    response = client.get(stage_url(localized_project))
    assert response.context["problem"]["title"] == old.data["title"]
    assert "No English translation is available for this version." in response.content.decode()
    assert "NEW-CURRENT-TITLE" not in response.content.decode()


def test_withdrawal_removes_translated_problem_access(localized_project, client):
    version = localized_project.current.stages.get(key="stage-0").problem_version
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    assert client.get(stage_url(localized_project)).status_code == 404
    detail = client.get(f"/projects/{localized_project.pk}/")
    body = detail.content.decode()
    assert "Sum only positive values" not in body and version.data["title"] not in body
    assert "problem_language" not in detail.context["stages"][0]
    assert detail.context["stages"][0]["problem_title"] is None
    Project.objects.filter(pk=localized_project.pk).update(published=False)
    assert client.get(f"/projects/{localized_project.pk}/").status_code == 404
    assert client.get(stage_url(localized_project, 1)).status_code == 404
