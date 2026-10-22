import copy

from django.urls import reverse

from core.content import import_package
from core.models import Draft, Problem, ProblemVersion


def test_old_draft_keeps_its_code_and_statement_after_revision(catalog, user, logged_client):
    old = ProblemVersion.objects.get(problem_id="sum-positive")
    source = "# my private work\ndef solve(values): return 17"
    draft = Draft.objects.create(user=user, version=old, source=source)
    changed = copy.deepcopy(catalog)
    changed["problems"][0]["revision"] += 1
    changed["problems"][0]["statement"] = "新版题意。"
    import_package(changed)

    url = reverse("draft_detail", args=[draft.pk])
    assert url in logged_client.get("/me/").content.decode()
    response = logged_client.get(url)
    assert response.status_code == 200
    text = response.content.decode()
    assert source in text and old.data["statement"] in text
    assert "题目已有新版本" in text and "新版题意。" not in text
    assert response["Cache-Control"] == "no-store"
    assert source not in logged_client.get("/problems/sum-positive/").content.decode()
    draft.refresh_from_db()
    assert draft.version_id == old.pk and draft.source == source


def test_private_draft_access_and_escaping(catalog, user, other_user, logged_client, client):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    source = '<script>alert("private")</script>'
    draft = Draft.objects.create(user=user, version=version, source=source)
    url = reverse("draft_detail", args=[draft.pk])
    response = logged_client.get(url)
    assert response.status_code == 200
    assert source not in response.content.decode()
    assert "&lt;script&gt;" in response.content.decode()
    assert "继续编辑" in response.content.decode()
    assert logged_client.post(url).status_code == 405
    client.logout()
    assert client.get(url).status_code == 302
    client.force_login(other_user)
    assert client.get(url).status_code == 404
    assert client.get(reverse("draft_detail", args=[draft.pk + 1000])).status_code == 404


def test_withdrawn_problem_draft_remains_readable(catalog, user, logged_client):
    version = ProblemVersion.objects.get(problem_id="sum-positive")
    draft = Draft.objects.create(user=user, version=version, source="# retained private code")
    Problem.objects.filter(pk=version.problem_id).update(published=False)
    url = reverse("draft_detail", args=[draft.pk])
    assert url in logged_client.get("/me/").content.decode()
    response = logged_client.get(url)
    assert response.status_code == 200
    assert "题目已下线" in response.content.decode()
    assert draft.source in response.content.decode()
    assert logged_client.get("/problems/sum-positive/").status_code == 404


def test_drafts_older_than_first_page_are_reachable(catalog, user, logged_client):
    original = ProblemVersion.objects.get(problem_id="sum-positive")
    oldest = Draft.objects.create(user=user, version=original, source="# oldest draft")
    for revision in range(original.revision + 1, original.revision + 21):
        data = dict(original.data, revision=revision)
        version = ProblemVersion.objects.create(
            problem=original.problem, revision=revision, digest="a" * 64, data=data
        )
        Draft.objects.create(user=user, version=version, source="# later draft")
    url = reverse("draft_detail", args=[oldest.pk])
    first = logged_client.get("/me/").content.decode()
    assert url not in first and 'aria-label="草稿分页"' in first
    assert url in logged_client.get("/me/?draft_page=2").content.decode()
