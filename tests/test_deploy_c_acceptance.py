"""Upgrade preflight/fixture boundaries; no claim of an actual image upgrade."""

import copy
import secrets
import uuid
from types import SimpleNamespace

import psycopg
import pytest
from django.db import connection
from psycopg import sql

from scripts.deploy_acceptance import (
    B2_MIGRATIONS,
    C_MIGRATIONS,
    UPGRADE_MIGRATIONS,
    validate_upgrade_manifests,
)
from scripts.deploy_c_acceptance import (
    COACHING_WRITE,
    NEW_TABLES,
    ORG_READ,
    ORG_WRITE,
    CRehearsal,
    role_rows,
    row_digest,
)


def manifests():
    previous = {
        "source_files": {
            f"{app}/migrations/{name}.py": "a" * 64
            for app, name in UPGRADE_MIGRATIONS
            | B2_MIGRATIONS
            | {("core", "0001_initial"), ("core", "0002_admission_and_policy")}
        },
        "images": {"web": "sha256:" + "a" * 64},
    }
    target = copy.deepcopy(previous)
    target["images"]["web"] = "sha256:" + "b" * 64
    target["source_files"].update(
        {f"{app}/migrations/{name}.py": "c" * 64 for app, name in C_MIGRATIONS}
    )
    return previous, target


def test_verified_b2_to_c_contract_selects_c_rehearsal():
    assert validate_upgrade_manifests(*manifests()) == "c"


@pytest.mark.parametrize("migration", sorted(C_MIGRATIONS))
def test_incomplete_target_c_schema_is_rejected(migration):
    previous, target = manifests()
    app, name = migration
    target["source_files"].pop(f"{app}/migrations/{name}.py")
    with pytest.raises(ValueError, match="all C migrations"):
        validate_upgrade_manifests(previous, target)


@pytest.mark.parametrize("migration", sorted(C_MIGRATIONS))
def test_already_partially_upgraded_previous_cannot_claim_b2_rehearsal(migration):
    previous, target = manifests()
    app, name = migration
    path = f"{app}/migrations/{name}.py"
    previous["source_files"][path] = target["source_files"][path]
    with pytest.raises(ValueError, match="precede the C schema"):
        validate_upgrade_manifests(previous, target)


@pytest.mark.parametrize("change", ["history-edited", "history-deleted", "same-image"])
def test_c_contract_preserves_b2_migrations_and_requires_real_new_image(change):
    previous, target = manifests()
    if change == "history-edited":
        target["source_files"]["core/migrations/0001_initial.py"] = "d" * 64
    elif change == "history-deleted":
        target["source_files"].pop("projects/migrations/0001_initial.py")
    else:
        target["images"]["web"] = previous["images"]["web"]
    with pytest.raises(ValueError):
        validate_upgrade_manifests(previous, target)


def test_new_rehearsal_cannot_initially_claim_success_or_auto_publish(tmp_path):
    options = SimpleNamespace(
        release=tmp_path / "c",
        previous_release=tmp_path / "b2",
        manifest_sha256="a" * 64,
        previous_manifest_sha256="b" * 64,
        upgrade_only=True,
        worker_restart_only=False,
    )
    rehearsal = CRehearsal(options, tmp_path)
    assert not rehearsal.result["passed"]
    assert not rehearsal.result["schema_changing_upgrade_tested"]
    assert not rehearsal.result["feature_role_permissions_tested"]
    assert not rehearsal.result["expanded_catalogue_published"]
    assert "B2 to C" in rehearsal.result["scope"]
    assert "--initial-content" not in rehearsal.apply_args()
    assert "--initial-content" in rehearsal.apply_args(initial=True, previous=True)
    with pytest.raises(ValueError, match="preserve the previous catalogue"):
        rehearsal.apply_args(initial=True)


def test_role_probe_graph_preserves_owner_content_and_service_lock():
    rows = role_rows(7, 13)
    assert set(rows) == set((*ORG_WRITE, *COACHING_WRITE))
    assert len(NEW_TABLES) == len(set(NEW_TABLES)) == 17
    assert not set(ORG_READ).intersection(rows)
    assert rows["coaching_slotlock"]["id"] == 2
    assert set(rows["coaching_executionlease"]) == {"id", "expires_at"}
    assert rows["organizations_courseunit"]["problem_version_id"] == 13
    assert rows["coaching_hintrequest"]["version_id"] == 13
    assert (
        rows["organizations_courseenrollment"]["revision_id"]
        == rows["organizations_courserevision"]["id"]
    )
    assert (
        rows["organizations_courseunit"]["revision_id"]
        == rows["organizations_courserevision"]["id"]
    )
    assert rows["organizations_courserevision"]["course_id"] == rows["organizations_course"]["id"]
    assert (
        rows["organizations_packagerevision"]["package_id"] == rows["organizations_package"]["id"]
    )
    assert rows["organizations_course"]["current_id"] is None
    assert rows["organizations_package"]["current_id"] is None


def test_snapshot_hash_detects_private_value_changes_without_emitting_rows():
    rows = [({"source": "private", "result": {"verdict": "accepted"}},)]
    before = row_digest(rows)
    assert set(before) == {"rows", "sha256"}
    assert before["rows"] == 1 and len(before["sha256"]) == 64
    changed = copy.deepcopy(rows)
    changed[0][0]["source"] = "changed"
    assert row_digest(changed) != before
    assert row_digest(rows + rows)["rows"] == 2


@pytest.mark.django_db(transaction=True)
def test_c_role_probe_executes_real_postgres_fk_graph_and_denials(user, monkeypatch, tmp_path):
    """Validate the harness's SQL against the schema; this is not a release upgrade."""
    if connection.vendor != "postgresql":
        pytest.skip("Actual PostgreSQL permissions and sequence contract")
    from coaching.models import SlotLock
    from core.models import Problem, ProblemVersion
    from organizations.models import PublicationLock
    from scripts.deploy_database import web_read_tables, web_write_tables

    PublicationLock.objects.get_or_create(pk=1)
    SlotLock.objects.get_or_create(pk=1)
    problem = Problem.objects.create(pk="sum-positive")
    version = ProblemVersion.objects.create(problem=problem, revision=1, digest="a" * 64, data={})
    problem.current = version
    problem.save(update_fields=["current"])
    options = SimpleNamespace(
        release=tmp_path / "c",
        previous_release=tmp_path / "b2",
        manifest_sha256="a" * 64,
        previous_manifest_sha256="b" * 64,
        upgrade_only=True,
        worker_restart_only=False,
    )
    rehearsal = CRehearsal(options, tmp_path)
    config = connection.settings_dict
    credentials = dict(
        dbname=config["NAME"],
        user=config["USER"],
        password=config["PASSWORD"],
        host=config["HOST"],
        port=config["PORT"],
        autocommit=True,
        options="-c statement_timeout=15000 -c lock_timeout=3000",
    )
    passwords = {role: secrets.token_urlsafe(24) for role in ("web", "worker")}
    rehearsal.instance = "test" + uuid.uuid4().hex[:12]
    names = {role: f"px_{rehearsal.instance}_{role}" for role in passwords}
    monkeypatch.setattr(rehearsal, "connection", lambda generation: psycopg.connect(**credentials))
    monkeypatch.setattr(
        rehearsal,
        "runtime_connection",
        lambda generation, role: psycopg.connect(
            **dict(credentials, user=names[role], password=passwords[role])
        ),
    )
    with psycopg.connect(**credentials) as owner:
        try:
            for role, name in names.items():
                owner.execute(
                    sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        sql.Identifier(name), sql.Literal(passwords[role])
                    )
                )
                owner.execute(
                    sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(name))
                )
            writable = web_write_tables(owner)
            for table in web_read_tables(owner):
                if table not in NEW_TABLES:
                    continue
                owner.execute(
                    sql.SQL("GRANT SELECT ON {} TO {}").format(
                        sql.Identifier(table), sql.Identifier(names["web"])
                    )
                )
            for table in writable:
                if table not in NEW_TABLES:
                    continue
                owner.execute(
                    sql.SQL("GRANT SELECT,INSERT,UPDATE,DELETE ON {} TO {}").format(
                        sql.Identifier(table), sql.Identifier(names["web"])
                    )
                )
                sequence = owner.execute(
                    "SELECT pg_get_serial_sequence(%s, 'id')", (table,)
                ).fetchone()[0]
                if sequence:
                    owner.execute(
                        sql.SQL("GRANT USAGE,SELECT ON SEQUENCE {} TO {}").format(
                            sql.Identifier(*sequence.split(".")), sql.Identifier(names["web"])
                        )
                    )
            result = rehearsal.feature_roles({}, user.username)
            assert result["actual_crud_and_denials"] and result["probe_rows_rolled_back"]
            assert len(result["web_crud_tables"]) == 15
        finally:
            for name in names.values():
                if owner.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone():
                    owner.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(name)))
                    owner.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(name)))
