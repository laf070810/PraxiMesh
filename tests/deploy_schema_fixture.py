"""Build a fresh, owned F database using unchanged historical migrations.

The checked-in inventory came from the independently verified F source archive.
No archived Python, old image, learner code, GPU or evaluation result is run.
"""

import hashlib
import io
import json
import os
import re
import uuid
from datetime import timedelta
from pathlib import Path

import django

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tests/fixtures/f_migration_history.json"


def baseline_targets(loader):
    baseline = json.loads(BASELINE.read_text())
    assert django.get_version() == baseline["django_version"]
    for name, digest in (baseline["project_migrations"] | baseline["content_files"]).items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest, name
    historical = {(Path(name).parts[0], Path(name).stem) for name in baseline["project_migrations"]}
    projects = {app for app, _ in historical}
    targets = sorted(
        node
        for node in historical
        if not any(child.key in historical for child in loader.graph.node_map[node].children)
    )
    framework = [tuple(node) for node in baseline["framework_targets"]]
    assert set(framework) == {node for node in loader.graph.leaf_nodes() if node[0] not in projects}
    targets += framework
    closure = set().union(*(set(loader.graph.forwards_plan(node)) for node in targets))
    assert {node for node in closure if node[0] in projects} == historical
    return targets, closure


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def seed(apps):
    from django.utils import timezone

    model = apps.get_model
    now = timezone.now()
    user = model("auth", "User").objects.create(
        username="schema-fixture", password="!", first_name="private-before-upgrade"
    )
    model("core", "AccountProfile").objects.create(
        user=user, display_name="private profile", recovery_hash="unusable-fixture-hash"
    )
    package = json.loads((ROOT / "content/starter.json").read_text())
    node_data = next(value for value in package["nodes"] if value["kind"] == "path")
    node = model("core", "KnowledgeNode").objects.create(
        id=node_data["id"], kind=node_data["kind"], title=node_data["title"], data=node_data
    )
    problem_data = next(value for value in package["problems"] if value["id"] == "sum-positive")
    problem = model("core", "Problem").objects.create(id=problem_data["id"])
    versions = []
    for revision in (1, 2):
        data = dict(problem_data, revision=revision, changelog=f"Synthetic revision {revision}")
        versions.append(
            model("core", "ProblemVersion").objects.create(
                problem=problem, revision=revision, digest=digest(data), data=data
            )
        )
    problem.current = versions[1]
    problem.save()
    version = versions[0]
    source = "Private migration fixture; never executed"
    model("core", "Draft").objects.create(user=user, version=version, source=source)
    model("core", "Enrollment").objects.create(user=user, path=node)
    model("learning", "Bookmark").objects.create(user=user, problem=problem)
    model("learning", "Note").objects.create(
        user=user, version=version, title="Private historical note", body="Private body"
    )
    model("community", "Post").objects.create(
        author=user, version=version, kind="feedback", title="Private draft", body="Private text"
    )
    model("coaching", "LearningPlan").objects.create(
        user=user, targets=[node.pk], suggestions=[{"version": version.pk}]
    )
    model("practice_api", "AccessToken").objects.create(
        user=user,
        label="Never-issued fixture token",
        secret_digest=hashlib.sha256(b"never-issued").hexdigest(),
        can_read_results=True,
        expires_at=now + timedelta(days=1),
    )
    model("sessions", "Session").objects.create(
        session_key="never-issued-fixture",
        session_data="private",
        expire_date=now + timedelta(days=1),
    )
    organization = model("organizations", "Organization").objects.create(
        id="schema-fixture", name="Private organization", created_by=user
    )
    model("organizations", "Membership").objects.create(
        organization=organization, user=user, role="owner"
    )
    course = model("organizations", "Course").objects.create(
        organization=organization, slug="historical", published=True
    )
    data = {"title": "Historical course", "version": version.pk}
    course_revision = model("organizations", "CourseRevision").objects.create(
        course=course, revision=1, data=data, digest=digest(data), author=user, published_at=now
    )
    course.current = course_revision
    course.save()
    model("organizations", "CourseUnit").objects.create(
        revision=course_revision, key="first", position=1, title="First", problem_version=version
    )
    model("organizations", "CourseEnrollment").objects.create(user=user, revision=course_revision)
    jobs = {}
    for state in ("legacy", "reserved", "consumed", "released"):
        budget = (
            {}
            if state == "legacy"
            else {
                "budget_day": now.date(),
                "budget_policy": "cpu-invocations-v1",
                "budget_state": state,
                "budget_units": 3,
                "execution_started_at": now if state == "consumed" else None,
            }
        )
        job = model("core", "Submission").objects.create(
            user=user,
            version=version,
            source=source,
            source_digest=hashlib.sha256(source.encode()).hexdigest(),
            mode="submit",
            status="queued" if state == "reserved" else "finished",
            verdict="" if state == "reserved" else "wrong_answer",
            result={"synthetic_fixture": True, "private": "historical result"},
            environment={"synthetic_fixture": True, "private": "historical environment"},
            **budget,
        )
        jobs[state] = str(job.pk)
        if state in {"legacy", "consumed"}:
            model("showcase", "SharedResult").objects.create(
                submission=job, is_public=state == "legacy", revision=3
            )
    model("practice_api", "SubmissionRequest").objects.create(
        user=user,
        key=uuid.uuid4(),
        request_digest="a" * 64,
        submission_id=jobs["legacy"],
    )
    model("showcase", "PublicProfile").objects.create(user=user, is_public=False, bio="Private bio")
    model("core", "WorkerHealth").objects.create(
        id="synthetic-unready-worker", ready=False, checked_at=now, fingerprint="a" * 64
    )
    return {"user": user.pk, "version": version.pk, "jobs": jobs}


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
    django.setup()
    from django.core.management.sql import emit_post_migrate_signal
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    assert connection.vendor == "postgresql"
    assert connection.settings_dict["HOST"] == "127.0.0.1"
    name = os.environ["PRAXIMESH_SCHEMA_FIXTURE_DATABASE"]
    marker = os.environ["PRAXIMESH_SCHEMA_FIXTURE_MARKER"]
    assert re.fullmatch(r"px_gr[a-f0-9]{12}_[a-f0-9]{12}", name)
    assert connection.settings_dict["NAME"] == name
    assert os.environ.get("JUDGE_ENABLED") == "0" and not os.environ.get("JUDGE_GPU_DEVICES")
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_user,pg_get_userbyid(datdba),shobj_description(oid,'pg_database') "
            "FROM pg_database WHERE datname=current_database()"
        )
        user, owner, actual = cursor.fetchone()
        assert user == owner and actual == marker
        cursor.execute("SELECT to_regclass('public.django_migrations')")
        assert cursor.fetchone() == (None,)
    executor = MigrationExecutor(connection)
    targets, expected = baseline_targets(executor.loader)
    plan = executor.migration_plan(targets)
    assert not any(backwards for _, backwards in plan)
    state = executor.migrate(targets, plan=plan)
    assert set(executor.recorder.applied_migrations()) == expected
    state.clear_delayed_apps_cache()
    emit_post_migrate_signal(
        0, False, connection.alias, apps=state.apps, plan=plan, stdout=io.StringIO()
    )
    result = seed(state.apps)
    result["migrations"] = sorted(expected)
    connection.close()
    print(json.dumps(result))


if __name__ == "__main__":
    main()
