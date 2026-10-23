import ast
import copy
import gettext
import importlib
import io
import re

import pytest
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import connection
from django.template.loader import get_template
from django.test import Client
from django.utils import translation

from core.content import ContentError, digest, import_package, public_problem
from core.models import Draft, KnowledgeNode, Problem, ProblemVersion, Submission
from learning.models import TutorialReading
from localization.models import KnowledgeSnapshot, KnowledgeTranslation, ProblemTranslation
from localization.packages import import_translations, load_translations
from localization.presentation import localized_node, localized_problem


@pytest.fixture
def english(catalog):
    package = load_translations(settings.CONTENT_ROOT / "en.json")
    assert import_translations(package) == {"nodes": 20, "problems": 10}
    return package


@pytest.fixture
def en_client(client):
    client.defaults["HTTP_ACCEPT_LANGUAGE"] = "en-US,en;q=0.9"
    yield client
    translation.deactivate()


def test_all_templates_compile_and_catalogs_are_real():
    for path in (settings.BASE_DIR / "templates").rglob("*.html"):
        get_template(str(path.relative_to(settings.BASE_DIR / "templates")))
    for domain in ("django", "djangojs"):
        directory = settings.BASE_DIR / "locale/en/LC_MESSAGES"
        text = (directory / f"{domain}.po").read_text()
        assert not re.search(r"^#,.*\bfuzzy\b", text, re.MULTILINE)
        entries = {}
        row, field = {}, None
        for line in text.splitlines():
            if line.startswith("msgid "):
                if row.get("msgid"):
                    entries[row["msgid"]] = row["msgstr"]
                row, field = {"msgid": ast.literal_eval(line[6:]), "msgstr": ""}, "msgid"
            elif line.startswith("msgstr "):
                field = "msgstr"
                row[field] = ast.literal_eval(line[7:])
            elif line.startswith('"'):
                row[field] += ast.literal_eval(line)
        if row.get("msgid"):
            entries[row["msgid"]] = row["msgstr"]
        assert entries and all(entries.values())
        catalog = gettext.GNUTranslations(io.BytesIO((directory / f"{domain}.mo").read_bytes()))
        messages = {key: value for key, value in catalog._catalog.items() if key}
        assert messages == entries
        assert all(value != key for key, value in messages.items())
    with translation.override("en"):
        assert translation.gettext("保存草稿") == "Save draft"


def test_locale_switch_cookie_priority_csrf_and_safe_redirect(english):
    client = Client(enforce_csrf_checks=True, HTTP_ACCEPT_LANGUAGE="en")
    home = client.get("/")
    assert b'<html lang="en">' in home.content
    assert client.post("/i18n/setlang/", {"language": "zh-hans"}).status_code == 403
    token = client.cookies["csrftoken"].value
    response = client.post(
        "/i18n/setlang/",
        {"language": "zh-hans", "next": "https://evil.invalid/", "csrfmiddlewaretoken": token},
    )
    assert response.status_code == 302 and response.url == "/"
    assert response.cookies["django_language"]["httponly"]
    assert response.cookies["django_language"]["samesite"] == "Lax"
    assert b'<html lang="zh-hans">' in client.get("/").content
    response = client.post(
        "/i18n/setlang/", {"language": "unsupported", "csrfmiddlewaretoken": token}
    )
    assert "django_language" not in response.cookies
    assert "django_language" not in client.get("/i18n/setlang/?language=en").cookies
    translation.deactivate()


@pytest.mark.parametrize(
    "url,label",
    [
        ("/", "From a concept"),
        ("/learn/", "Knowledge and paths"),
        ("/learn/tutorial-functions/", "Turn a problem into a function contract"),
        ("/learn/python-to-numerical/", "From Python foundations"),
        ("/practice/", "Problem library"),
        ("/problems/sum-positive/", "Sum only positive values"),
        ("/accounts/login/", "Log in"),
        ("/accounts/register/", "Create a learning account"),
        ("/accounts/recover/", "Recover account"),
        ("/community/", "Discuss ideas"),
    ],
)
def test_english_public_pages(english, en_client, url, label):
    response = en_client.get(url)
    assert response.status_code == 200 and label in response.content.decode()
    assert response["Content-Language"] == "en"


def test_js_and_form_feedback_are_english(english, en_client, user):
    assert "Draft saved privately" in en_client.get("/jsi18n/").content.decode()
    en_client.force_login(user)
    response = en_client.post("/problems/sum-positive/draft/", {"version": "wrong", "source": "x"})
    assert response.status_code == 409 and response.json()["error"].startswith(
        "The problem changed"
    )
    assert "Title" in en_client.get("/community/new/").content.decode()
    assert "New password" in en_client.get("/accounts/recover/").content.decode()


def test_exact_translation_keeps_problem_identity_and_private_tests(english, en_client):
    version = Problem.objects.get(pk="sum-positive").current
    original = copy.deepcopy(version.data)
    text = en_client.get("/problems/sum-positive/").content.decode()
    for case in original["tests"]:
        if not case["public"]:
            assert case["name"] not in text
    assert original["reference"] not in text
    assert "Sum only positive values" in text and "Mixed signs" in text
    result = localized_problem(version, "en")
    assert result["starter"] == original["starter"]
    assert result["comparison"] == original["comparison"]
    assert [x["args"] for x in result["examples"]] == [
        x["args"] for x in public_problem(version)["examples"]
    ]
    version.refresh_from_db()
    assert version.data == original and version.digest == digest(original)


def test_fallback_is_per_object_and_old_translations_never_cover_new_sources(
    english, en_client, package
):
    assert "Chinese original" in en_client.get("/practice/").content.decode()
    assert (
        "No English translation is available"
        in en_client.get("/problems/argmax-first/").content.decode()
    )
    node = KnowledgeNode.objects.get(pk="functions")
    previous = digest(node.data)
    modified = copy.deepcopy(package)
    data = next(item for item in modified["nodes"] if item["id"] == node.pk)
    data["summary"] += " 新版本"
    problem = next(item for item in modified["problems"] if item["id"] == "sum-positive")
    problem["revision"] += 1
    problem["statement"] += " 新版本"
    import_package(modified)
    node.refresh_from_db()
    assert KnowledgeSnapshot.objects.filter(node=node).count() == 2
    assert KnowledgeSnapshot.objects.get(node=node, source_digest=previous).source_data != node.data
    assert localized_node(node, "en")["translation_fallback"]
    assert localized_problem(Problem.objects.get(pk="sum-positive").current, "en")[
        "translation_fallback"
    ]
    old = ProblemVersion.objects.get(problem_id="sum-positive", revision=problem["revision"] - 1)
    assert localized_problem(old, "en")["title"] == "Sum only positive values"


def test_translation_revision_is_immutable_and_can_be_withdrawn(english):
    row = ProblemTranslation.objects.get(version__problem_id="sum-positive")
    row.fields = {**row.fields, "title": "Modified"}
    with pytest.raises(ValidationError):
        row.save()
    modified = copy.deepcopy(english)
    modified["problems"][0]["fields"]["title"] = "Modified"
    with pytest.raises(ContentError, match="Immutable"):
        import_translations(modified)
    modified["problems"][0]["revision"] = 2
    import_translations(modified)
    version = row.version
    assert localized_problem(version, "en")["title"] == "Modified"
    modified["problems"][0]["published"] = False
    import_translations(modified)
    assert localized_problem(version, "en")["translation_fallback"]
    with pytest.raises(ContentError, match="roll back"):
        import_translations(english)


@pytest.mark.parametrize(
    "field",
    [
        "tests",
        "reference",
        "starter",
        "function",
        "comparison",
        "concepts",
        "language",
        "evaluator",
        "license",
    ],
)
def test_forbidden_problem_overrides_are_atomic(english, field):
    invalid = copy.deepcopy(english)
    invalid["nodes"][0]["revision"] = 2
    invalid["problems"][0]["fields"][field] = "not allowed"
    with pytest.raises(ContentError):
        import_translations(invalid)
    assert not KnowledgeTranslation.objects.filter(revision=2).exists()


@pytest.mark.parametrize(
    "field", ["relations", "steps", "domains", "topics", "concepts", "id", "kind"]
)
def test_forbidden_knowledge_overrides(english, field):
    invalid = copy.deepcopy(english)
    invalid["nodes"][0]["fields"][field] = []
    with pytest.raises(ContentError):
        import_translations(invalid)


@pytest.mark.parametrize("kind", ["nodes", "problems"])
def test_unknown_or_mismatched_source_is_rejected(english, kind):
    invalid = copy.deepcopy(english)
    invalid[kind][0]["source_digest"] = "0" * 64
    with pytest.raises(ContentError):
        import_translations(invalid)


def test_idempotent_import_and_known_current_snapshot_backfill(english):
    before = (
        KnowledgeSnapshot.objects.count(),
        KnowledgeTranslation.objects.count(),
        ProblemTranslation.objects.count(),
    )
    import_translations(english)
    migration = importlib.import_module("localization.migrations.0002_backfill_snapshots")
    from types import SimpleNamespace

    from django.apps import apps

    migration.backfill(apps, SimpleNamespace(connection=connection))
    assert before == (
        KnowledgeSnapshot.objects.count(),
        KnowledgeTranslation.objects.count(),
        ProblemTranslation.objects.count(),
    )


def test_locale_does_not_change_reading_digest_and_private_pages(
    english, en_client, user, other_user
):
    en_client.force_login(user)
    node = KnowledgeNode.objects.get(pk="tutorial-functions")
    assert (
        en_client.post(f"/learning/reading/{node.pk}/", {"action": "complete"}).status_code == 302
    )
    declaration = TutorialReading.objects.get(user=user)
    assert declaration.content_digest == digest(node.data)
    assert en_client.get(f"/learn/{node.pk}/").context["reading_current"]
    version = Problem.objects.get(pk="sum-positive").current
    job = Submission.objects.create(
        user=user,
        version=version,
        source="PRIVATE CODE",
        source_digest="a" * 64,
        status="finished",
        verdict="wrong_answer",
        result={
            "label": "答案有误",
            "public_cases": [
                {
                    "name": version.data["tests"][0]["name"],
                    "verdict": "wrong_answer",
                    "label": "答案有误",
                }
            ],
        },
    )
    page = en_client.get(f"/submissions/{job.pk}/")
    assert page.status_code == 200 and "Wrong answer" in page.content.decode()
    job.refresh_from_db()
    assert job.result["label"] == "答案有误"
    en_client.force_login(other_user)
    assert en_client.get(f"/submissions/{job.pk}/").status_code == 404
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    assert en_client.get("/problems/sum-positive/").status_code == 404
    assert b"Sum only positive values" not in en_client.get("/practice/").content


def test_source_and_translations_import_in_one_transaction(catalog, tmp_path, package, english):
    modified = copy.deepcopy(package)
    modified["nodes"][0]["title"] = "Replacement title"
    source = tmp_path / "source.json"
    import json

    source.write_text(json.dumps(modified))
    invalid = tmp_path / "translations.json"
    invalid.write_text('{"unexpected": true}')
    from django.core.management import CommandError

    with pytest.raises(CommandError):
        call_command("content_import", str(source), translations=str(invalid))
    assert KnowledgeNode.objects.get(pk=package["nodes"][0]["id"]).title != "Replacement title"


def test_private_user_text_is_never_automatically_translated(english, en_client, user):
    en_client.force_login(user)
    source = "# 私有源代码\ndef solve(values): return 0"
    version = Problem.objects.get(pk="sum-positive").current
    en_client.post("/problems/sum-positive/draft/", {"version": version.pk, "source": source})
    draft = Draft.objects.get(user=user)
    assert draft.source == source
    assert source in en_client.get(f"/drafts/{draft.pk}/").content.decode()
    assert "PRIVATE CODE" not in en_client.get("/community/").content.decode()
