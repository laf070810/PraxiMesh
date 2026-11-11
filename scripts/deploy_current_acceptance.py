"""Frozen F to current CPU application upgrade; never enables or queries a GPU."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.deploy_c_acceptance import PRIVILEGES, row_digest
from scripts.deploy_f_acceptance import FRehearsal
from scripts.production_acceptance import Client

ROOT = Path(__file__).resolve().parents[1]
ADDED_MIGRATIONS = {
    ("core", "0004_gpu_resources"),
    ("core", "0005_gpu_admission_environment"),
    ("showcase", "0002_shared_metric_scope"),
}


def f_migrations():
    return json.loads((ROOT / "tests/fixtures/f_migration_history.json").read_text())[
        "project_migrations"
    ]


def project_migrations(manifest):
    return {
        path: digest
        for path, digest in manifest["source_files"].items()
        if "/migrations/" in path and path.endswith(".py") and not path.endswith("/__init__.py")
    }


def validate_f_upgrade(previous, target):
    """Only the known F baseline and reviewed three-migration CPU successor."""
    for manifest in (previous, target):
        if (
            type(manifest.get("format")) is not int
            or manifest["format"] != 1
            or "gpu" in manifest
            or set(manifest.get("images", {})) != {"web", "worker"}
        ):
            raise ValueError("F upgrade acceptance requires two CPU-only format 1 releases")
    baseline = f_migrations()
    if project_migrations(previous) != baseline:
        raise ValueError("The previous release must match the frozen F migration inventory")
    expected = set(baseline) | {f"{app}/migrations/{name}.py" for app, name in ADDED_MIGRATIONS}
    if set(project_migrations(target)) != expected:
        raise ValueError("The target must contain exactly the reviewed post-F migrations")
    if any(target["source_files"].get(path) != digest for path, digest in baseline.items()):
        raise ValueError("An upgrade must preserve the frozen F migration sources")
    if any(previous["images"][role] == target["images"][role] for role in ("web", "worker")):
        raise ValueError("F upgrade acceptance requires different Web and worker images")
    return "post-f"


def original_export_fields(exported):
    """Check the two new fields explicitly before comparing the original export."""
    projected = copy.deepcopy(exported)
    budget = projected.pop("gpu_budget", None)
    if (
        not isinstance(budget, dict)
        or budget.get("policy") != "gpu-slot-seconds-v1"
        or any(
            type(budget.get(key)) is not int
            for key in ("reserved", "consumed", "limit", "remaining")
        )
        or budget["reserved"] != 0
        or budget["consumed"] != 0
        or budget["limit"] < 0
        or budget["remaining"] != budget["limit"]
    ):
        raise ValueError("CPU upgrade users must have an unused GPU budget")
    for entry in projected["showcase"]["shared_results"]:
        if entry.pop("metric_kind", None) != "sequential-v1":
            raise ValueError("An old shared result must retain its original public metric scope")
    return projected


class CurrentRehearsal(FRehearsal):
    def __init__(self, options, directory):
        super().__init__(options, directory)
        self.result["scope"] = (
            "frozen F to current CPU application; retained private records, real WASI "
            "history and CPU reservations; original sharing scope; loopback HTTPS; "
            "empty GPU database table, no GPU access or admission evidence"
        )

    def snapshot(self, generation, usernames):
        result = super().snapshot(generation, usernames)
        with self.connection(generation) as connection:
            # PostgreSQL removes an absent JSON key harmlessly on the old schema.
            # Its new value is independently checked after migration.
            rows = connection.execute(
                "SELECT to_jsonb(t)-'metric_kind' FROM showcase_sharedresult t "
                "WHERE submission_id IN (SELECT id FROM core_submission WHERE user_id=ANY(%s)) "
                "ORDER BY id",
                (result["user_ids"],),
            ).fetchall()
        result["tables"]["showcase_sharedresult"] = row_digest(rows)
        return result

    def seed_feedback(self, generation, username):
        client, _ = self.accounts[username]
        title = "Retained version feedback " + self.token
        assert (
            client.request(
                "/community/new/",
                {
                    "kind": "feedback",
                    "title": title,
                    "version": client.version,
                    "body": "Private upgrade fixture",
                },
            )[0]
            == 200
        )
        with self.connection(generation) as connection:
            row = connection.execute(
                "SELECT id, version_id FROM community_post "
                "WHERE author_id=%s AND kind='feedback' AND title=%s",
                (self.user_id(generation, username), title),
            ).fetchone()
        assert row and row[1] == int(client.version)
        return f"/community/{row[0]}/"

    def private_feedback(self, path, username, other_username):
        author, _ = self.accounts[username]
        other, _ = self.accounts[other_username]
        assert author.request(path)[0] == 200
        assert other.request(path)[0] == 404
        assert Client(self.base, self.context).request(path)[0] == 404

    def new_resource_permissions(self, generation):
        with self.connection(generation) as connection:
            assert connection.execute("SELECT count(*) FROM core_gpudevice").fetchone() == (0,)
            metrics = connection.execute("SELECT metric_kind FROM showcase_sharedresult").fetchall()
            assert metrics and all(row == ("sequential-v1",) for row in metrics)
        permissions = {}
        for role in ("web", "worker"):
            with self.runtime_connection(generation, role) as connection:
                permissions[role] = {
                    privilege: connection.execute(
                        "SELECT has_table_privilege(current_user,'core_gpudevice',%s)",
                        (privilege,),
                    ).fetchone()[0]
                    for privilege in PRIVILEGES
                }
            allowed = {"SELECT"} if role == "web" else {"SELECT", "INSERT", "UPDATE"}
            assert permissions[role] == {
                privilege: privilege in allowed for privilege in PRIVILEGES
            }
        return permissions

    def upgrade_rehearsal(self):
        self.result["phase"] = "install-f"
        self.run(*self.apply_args(initial=True, previous=True), log="previous-initial")
        previous = self.journal()["current"]
        old_migrations, old_tables = self.schema_state(previous)
        expected = {(Path(path).parts[0], Path(path).stem) for path in f_migrations()}
        projects = {app for app, _ in expected}
        assert {node for node in old_migrations if node[0] in projects} == expected
        assert "core_gpudevice" not in old_tables
        username = self.register("current-upgrade-before")
        keeper = self.register("current-course-owner", judge=False)
        retained = self.seed_c(previous, username, keeper)
        feedback = self.seed_feedback(previous, username)
        self.private_feedback(feedback, username, keeper)
        client, _ = self.accounts[username]
        retained["export_before"] = self.request_json(client, "/accounts/export/", {})
        before = self.snapshot(previous, [username, keeper])
        catalogue = self.catalogue_snapshot(previous)
        owner_content = self.owner_content_snapshot(previous)
        permissions = self.community_roles(previous)

        self.result["phase"] = "upgrade-and-preserve"
        self.run(*self.apply_args(), log="schema-upgrade")
        journal = self.journal()
        upgraded = journal["current"]
        assert journal["operation"]["phase"] == "active"
        assert upgraded["release_sha256"] == self.options.manifest_sha256
        assert upgraded["id"] != previous["id"] and upgraded["database"] != previous["database"]
        new_migrations, new_tables = self.schema_state(upgraded)
        assert new_migrations - old_migrations == ADDED_MIGRATIONS
        assert old_migrations <= new_migrations
        assert new_tables == old_tables | {"core_gpudevice"}
        restored = self.snapshot(upgraded, [username, keeper])
        assert restored["tables"] == before["tables"] and restored["user_ids"] == before["user_ids"]
        assert all(
            restored["sequences"][key][0] >= value[0] for key, value in before["sequences"].items()
        )
        assert self.catalogue_snapshot(upgraded) == catalogue
        assert self.owner_content_snapshot(upgraded) == owner_content
        assert self.community_roles(upgraded) == permissions
        resource_permissions = self.new_resource_permissions(upgraded)

        exported = original_export_fields(self.request_json(client, "/accounts/export/", {}))
        for key in (
            "username",
            "display_name",
            "submissions",
            "drafts",
            "paths",
            "community",
            "practice_api",
            "showcase",
            "organizations",
            "coaching",
        ):
            assert exported[key] == retained["export_before"][key], key
        for key in ("notes", "bookmarks", "reading", "challenges"):
            assert exported["learning"][key] == retained["export_before"]["learning"][key], key
        self.private_feedback(feedback, username, keeper)
        assert client.request(retained["course_path"])[0] == 200
        replay = self.request_json(
            client, retained["endpoint"], retained["payload"], retained["headers"], expected=202
        )
        assert not replay["created"] and replay["id"] == retained["queued"]["id"]
        assert (
            Client(self.base, self.context).request(f"/showcase/results/{retained['shared']}/")[0]
            == 200
        )

        self.result["phase"] = "new-submission-browser-and-deletion"
        self.submit_and_wait(client)
        self.browser(problem_feedback=True)
        assert self.catalogue_snapshot(upgraded) == catalogue
        self.delete_retained_account(upgraded, username, retained)
        assert Client(self.base, self.context).request(feedback)[0] == 404
        with self.connection(upgraded) as connection:
            assert connection.execute("SELECT count(*) FROM core_gpudevice").fetchone() == (0,)
        self.result.update(
            schema_changing_upgrade_tested=True,
            real_added_migrations=[list(item) for item in sorted(new_migrations - old_migrations)],
            previous_private_courses_plans_feedback_and_reservations_preserved=True,
            no_automatic_content_publication_on_upgrade=True,
            retained_session_token_idempotency_and_private_export=True,
            original_shared_metric_scope_preserved=True,
            gpu_device_table_empty=True,
            feature_role_permissions_tested=True,
            feature_role_permissions=permissions,
            new_resource_role_permissions=resource_permissions,
            backup_restore=journal["operation"]["backup"],
            generations_attempted=2,
            no_journal_or_verdict_edits=True,
            phase="complete",
            completed_at=datetime.now(UTC).isoformat(),
        )
