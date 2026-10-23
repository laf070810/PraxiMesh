"""B2 to C rehearsal: preserved private state, least privilege and real CPU reservations.

Invoked by deploy_acceptance.py --upgrade-only after both release manifests have
been verified. No synthetic verdicts, quota edits, or migration journal edits.
Role probes are synthetic rows in explicitly rolled-back transactions; they do
not represent published courses, coaching requests, or model-provider execution.
"""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from scripts.deploy import Deployment
from scripts.deploy_acceptance import B2_MIGRATIONS, C_MIGRATIONS, CORRECT
from scripts.deploy_b2_acceptance import B2Rehearsal, wait_for_api_result
from scripts.production_acceptance import Client

BUDGET_FIELDS = (
    "budget_policy",
    "budget_day",
    "budget_units",
    "budget_state",
    "execution_started_at",
)
ORG_WRITE = (
    "organizations_organization",
    "organizations_membership",
    "organizations_invitation",
    "organizations_course",
    "organizations_courserevision",
    "organizations_courseunit",
    "organizations_courseenrollment",
    "organizations_package",
    "organizations_packagerevision",
    "organizations_organizationevent",
)
ORG_READ = ("organizations_managedobject", "organizations_publicationlock")
COACHING_WRITE = (
    "coaching_learningplan",
    "coaching_hintrequest",
    "coaching_dailyusage",
    "coaching_slotlock",
    "coaching_executionlease",
)
NEW_TABLES = (*ORG_WRITE, *ORG_READ, *COACHING_WRITE)
B2_OWNER_TABLES = (
    "projects_project",
    "projects_projectrevision",
    "projects_projectstage",
    "localization_knowledgesnapshot",
    "localization_knowledgetranslation",
    "localization_problemtranslation",
)
PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
    "MAINTAIN",
)
LEGACY_RESERVATION = {
    "policy": "",
    "day": None,
    "units": 0,
    "state": "legacy",
    "execution_started_at": None,
}


def row_digest(rows):
    data = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    return {"rows": len(rows), "sha256": hashlib.sha256(data).hexdigest()}


def role_rows(user_id, version_id):
    """Disposable valid FK graph, inserted/deleted in this order/reverse order."""
    now = datetime.now(UTC)
    course, package = uuid.uuid4(), uuid.uuid4()
    # Explicit negative bigint IDs do not collide with normal generated content.
    revision = -int(uuid.uuid4().hex[:12], 16)
    return {
        "organizations_organization": dict(
            id="upgrade-role-probe",
            name="Role probe",
            description="",
            created_by_id=user_id,
            created_at=now,
        ),
        "organizations_membership": dict(
            organization_id="upgrade-role-probe", user_id=user_id, role="owner", created_at=now
        ),
        "organizations_invitation": dict(
            id=uuid.uuid4(),
            organization_id="upgrade-role-probe",
            target_id=user_id,
            inviter_id=user_id,
            role="learner",
            state="pending",
            created_at=now,
        ),
        "organizations_course": dict(
            id=course,
            organization_id="upgrade-role-probe",
            slug="probe",
            current_id=None,
            published=False,
        ),
        "organizations_courserevision": dict(
            id=revision,
            course_id=course,
            revision=1,
            data=Jsonb({}),
            digest="0" * 64,
            author_id=user_id,
            created_at=now,
            published_at=None,
        ),
        "organizations_courseunit": dict(
            revision_id=revision,
            key="probe",
            position=1,
            title="Probe",
            description="",
            problem_version_id=version_id,
        ),
        "organizations_courseenrollment": dict(
            user_id=user_id, revision_id=revision, created_at=now
        ),
        "organizations_package": dict(
            id=package,
            organization_id="upgrade-role-probe",
            slug="probe",
            current_id=None,
            state="private",
        ),
        "organizations_packagerevision": dict(
            package_id=package,
            revision=1,
            payload=Jsonb({}),
            digest="0" * 64,
            author_id=user_id,
            state="draft",
            approved_by_id=None,
            approved_digest="",
            review_note="",
            created_at=now,
        ),
        "organizations_organizationevent": dict(
            organization_id="upgrade-role-probe",
            actor_id=user_id,
            target_kind="probe",
            target_id="probe",
            action="probe",
            created_at=now,
        ),
        "coaching_learningplan": dict(
            id=uuid.uuid4(),
            user_id=user_id,
            targets=Jsonb([]),
            suggestions=Jsonb([]),
            skipped=Jsonb([]),
            active=True,
            created_at=now,
        ),
        "coaching_hintrequest": dict(
            id=uuid.uuid4(),
            user_id=user_id,
            version_id=version_id,
            state="preview",
            payload=Jsonb({}),
            instructions="Role probe",
            request_digest="0" * 64,
            configuration_digest="0" * 64,
            provider_description="No model invoked",
            model="probe",
            prompt_version="probe",
            response="",
            error_code="",
            truncated=False,
            input_tokens=None,
            output_tokens=None,
            elapsed_ms=None,
            charged_day=None,
            lease_until=None,
            created_at=now,
            finished_at=None,
        ),
        "coaching_dailyusage": dict(user_id=user_id, day=now.date(), count=0),
        "coaching_slotlock": dict(id=2),
        "coaching_executionlease": dict(id=uuid.uuid4(), expires_at=now + timedelta(seconds=30)),
    }


class CRehearsal(B2Rehearsal):
    def __init__(self, options, directory):
        super().__init__(options, directory)
        self.result["scope"] = (
            "B2 to C schema upgrade; synthetic private data and real WASI results; "
            "retained B2 catalogue followed by explicit owner publication; "
            "private Docker networks and loopback HTTPS; no GPU or model-provider execution"
        )

    def snapshot(self, generation, usernames):
        result = super().snapshot(generation, usernames)
        with self.connection(generation) as connection:
            # Both projections have the identical B2 columns; the new fields are
            # independently checked as zero-cost legacy state, never ignored.
            rows = connection.execute(
                "SELECT to_jsonb(t)-%s::text[] FROM core_submission t "
                "WHERE user_id=ANY(%s) ORDER BY id",
                (list(BUDGET_FIELDS), result["user_ids"]),
            ).fetchall()
            result["tables"]["core_submission"] = row_digest(rows)
            for table, where in {
                "practice_api_accesstoken": "t.user_id=ANY(%s)",
                "practice_api_submissionrequest": "t.user_id=ANY(%s)",
                "showcase_publicprofile": "t.user_id=ANY(%s)",
                "showcase_sharedresult": "t.submission_id IN (SELECT id FROM core_submission WHERE user_id=ANY(%s))",
            }.items():
                rows = connection.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE " + where + " ORDER BY id").format(
                        sql.Identifier(table)
                    ),
                    (result["user_ids"],),
                ).fetchall()
                result["tables"][table] = row_digest(rows)
        return result

    def owner_content_snapshot(self, generation):
        with self.connection(generation) as connection:
            return {
                table: row_digest(
                    connection.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY id").format(
                            sql.Identifier(table)
                        )
                    ).fetchall()
                )
                for table in B2_OWNER_TABLES
            }

    def request_json(self, client, path, data=None, headers=None, expected=200):
        status, _, body = client.request(path, data, headers)
        assert status == expected, "Unexpected HTTPS status in C rehearsal"
        return json.loads(body)

    def wait_result(self, client, path, headers):
        return wait_for_api_result(client, path, headers, timeout=210)

    def seed_b2(self, generation, username):
        self.upgraded_http(generation, username)
        client, password = self.accounts[username]
        status, _, body = client.request(
            "/accounts/tokens/",
            {
                "label": "Retained upgrade token",
                "days": 1,
                "password": password,
                "can_submit": "on",
                "can_read_results": "on",
            },
        )
        matches = re.findall(rb"pm_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}", body)
        assert status == 200 and len(matches) == 1
        token = matches[0].decode()
        assert token.encode() not in client.request("/accounts/tokens/")[2]
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        payload = json.dumps(
            {"version_id": int(client.version), "source": CORRECT, "mode": "submit"}
        ).encode()
        endpoint = "/api/v1/problems/sum-positive/submissions/"
        queued = self.request_json(client, endpoint, payload, headers, expected=202)
        assert queued["created"]
        self.wait_result(client, queued["url"], headers)
        assert (
            client.request(
                "/showcase/settings/",
                {
                    "revision": 0,
                    "public_name": "Upgrade public result",
                    "bio": "Explicitly public",
                    "is_public": "on",
                    "confirm_public": "on",
                },
            )[0]
            == 200
        )
        assert (
            client.request(
                f"/showcase/share/{queued['id']}/",
                {
                    "profile_revision": 1,
                    "entry_revision": 0,
                    "confirm_public": "on",
                },
            )[0]
            == 200
        )
        assert (
            client.request("/i18n/setlang/", {"language": "en", "next": "/problems/sum-positive/"})[
                0
            ]
            == 200
        )
        assert client.request("/problems/sum-positive/")[1]["Content-Language"] == "en"
        exported = self.request_json(client, "/accounts/export/", {})
        assert len(exported["practice_api"]["tokens"]) == 1
        assert len(exported["showcase"]["shared_results"]) == 1
        with self.connection(generation) as connection:
            shared = connection.execute(
                "SELECT id FROM showcase_sharedresult WHERE submission_id=%s", (queued["id"],)
            ).fetchone()[0]
        assert Client(self.base, self.context).request(f"/showcase/results/{shared}/")[0] == 200
        return {
            "headers": headers,
            "payload": payload,
            "endpoint": endpoint,
            "queued": queued,
            "shared": str(shared),
            "token": token,
        }

    def check_retained_http(self, generation, username, retained):
        client, _ = self.accounts[username]
        replay = self.request_json(
            client, retained["endpoint"], retained["payload"], retained["headers"], expected=202
        )
        assert not replay["created"] and replay["id"] == retained["queued"]["id"]
        result = self.wait_result(client, retained["queued"]["url"], retained["headers"])
        assert result["resource_reservation"] == LEGACY_RESERVATION
        assert result["cpu_budget"]["reserved"] == result["cpu_budget"]["consumed"] == 0
        assert result["cpu_budget"]["remaining"] == result["cpu_budget"]["limit"] == 120
        assert client.request("/problems/sum-positive/")[1]["Content-Language"] == "en"
        for path in (
            "/learning/notes/",
            "/learning/bookmarks/",
            "/community/mine/",
            "/projects/",
            "/showcase/mine/",
        ):
            assert client.request(path)[0] == 200
        guest = Client(self.base, self.context)
        assert guest.request(f"/showcase/results/{retained['shared']}/")[0] == 200
        with self.connection(generation) as connection:
            identity = self.user_id(generation, username)
            post = connection.execute(
                "SELECT id FROM community_post WHERE author_id=%s", (identity,)
            ).fetchone()[0]
            draft = connection.execute(
                "SELECT id FROM core_draft WHERE user_id=%s", (identity,)
            ).fetchone()[0]
        assert guest.request(f"/community/{post}/")[0] == 404
        assert client.request(f"/drafts/{draft}/")[0] == 200
        exported = self.request_json(client, "/accounts/export/", {})
        assert len(exported["submissions"]) == 3 and len(exported["drafts"]) == 1
        assert all(
            row["resource_reservation"] == LEGACY_RESERVATION for row in exported["submissions"]
        )
        assert len(exported["learning"]["notes"]) == len(exported["community"]["posts"]) == 1
        assert retained["token"] not in json.dumps(exported)

    def feature_roles(self, generation, username):
        identity = self.user_id(generation, username)
        with self.connection(generation) as connection:
            version = connection.execute(
                "SELECT current_id FROM core_problem WHERE id='sum-positive'"
            ).fetchone()[0]
            counts = {
                table: connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                for table in NEW_TABLES
            }
            assert counts == {
                table: int(table in {"organizations_publicationlock", "coaching_slotlock"})
                for table in NEW_TABLES
            }
            sequences = {
                table: connection.execute(
                    "SELECT pg_get_serial_sequence(%s, 'id')", (table,)
                ).fetchone()[0]
                for table in NEW_TABLES
            }
        rows = role_rows(identity, version)
        assert set(rows) == set((*ORG_WRITE, *COACHING_WRITE))
        for role in ("web", "worker"):
            with self.runtime_connection(generation, role) as connection:
                assert (
                    connection.execute("SELECT current_user").fetchone()[0]
                    == f"px_{self.instance}_{role}"
                )
                for table in NEW_TABLES:
                    allowed = (
                        (
                            {"SELECT"}
                            if table in ORG_READ
                            else {"SELECT", "INSERT", "UPDATE", "DELETE"}
                        )
                        if role == "web"
                        else set()
                    )
                    for privilege in PRIVILEGES:
                        actual, delegable = connection.execute(
                            "SELECT has_table_privilege(current_user,%s,%s),has_table_privilege(current_user,%s,%s)",
                            (table, privilege, table, privilege + " WITH GRANT OPTION"),
                        ).fetchone()
                        assert actual == (privilege in allowed) and not delegable
                    if allowed:
                        connection.execute(
                            sql.SQL("SELECT id FROM {} LIMIT 1").format(sql.Identifier(table))
                        )
                    for statement, privilege in (
                        ("INSERT INTO {} DEFAULT VALUES", "INSERT"),
                        ("UPDATE {} SET id=id WHERE FALSE", "UPDATE"),
                        ("DELETE FROM {} WHERE FALSE", "DELETE"),
                        ("SELECT id FROM {} LIMIT 0", "SELECT"),
                    ):
                        if privilege not in allowed:
                            with connection.transaction(force_rollback=True):
                                try:
                                    connection.execute(
                                        sql.SQL(statement).format(sql.Identifier(table))
                                    )
                                except psycopg.errors.InsufficientPrivilege:
                                    pass
                                else:
                                    raise AssertionError("Unapproved runtime access to a C table")
                    if sequences[table]:
                        sequence = sequences[table]
                        for privilege in ("USAGE", "SELECT", "UPDATE"):
                            allowed_seq = (
                                role == "web" and table in rows and privilege in {"USAGE", "SELECT"}
                            )
                            actual, delegable = connection.execute(
                                "SELECT has_sequence_privilege(current_user,%s,%s),has_sequence_privilege(current_user,%s,%s)",
                                (sequence, privilege, sequence, privilege + " WITH GRANT OPTION"),
                            ).fetchone()
                            assert actual == allowed_seq and not delegable
                        if role == "worker" or table in ORG_READ:
                            with connection.transaction(force_rollback=True):
                                try:
                                    connection.execute("SELECT nextval(%s::regclass)", (sequence,))
                                except psycopg.errors.InsufficientPrivilege:
                                    pass
                                else:
                                    raise AssertionError("Unapproved runtime sequence use")
                if role == "web":
                    with connection.transaction(force_rollback=True):
                        inserted = []
                        for table, values in rows.items():
                            row_id = connection.execute(
                                sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
                                    sql.Identifier(table),
                                    sql.SQL(",").join(map(sql.Identifier, values)),
                                    sql.SQL(",").join(sql.Placeholder() for _ in values),
                                ),
                                tuple(values.values()),
                            ).fetchone()[0]
                            inserted.append((table, row_id))
                            assert (
                                connection.execute(
                                    sql.SQL("SELECT id FROM {} WHERE id=%s").format(
                                        sql.Identifier(table)
                                    ),
                                    (row_id,),
                                ).fetchone()[0]
                                == row_id
                            )
                            assert (
                                connection.execute(
                                    sql.SQL("UPDATE {} SET id=id WHERE id=%s").format(
                                        sql.Identifier(table)
                                    ),
                                    (row_id,),
                                ).rowcount
                                == 1
                            )
                        for table, row_id in reversed(inserted):
                            assert (
                                connection.execute(
                                    sql.SQL("DELETE FROM {} WHERE id=%s").format(
                                        sql.Identifier(table)
                                    ),
                                    (row_id,),
                                ).rowcount
                                == 1
                            )
        with self.connection(generation) as connection:
            assert counts == {
                table: connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                for table in NEW_TABLES
            }
        return {
            "web_crud_tables": list(rows),
            "web_read_only_tables": list(ORG_READ),
            "worker_access_denied": list(NEW_TABLES),
            "exact_table_and_sequence_privileges": True,
            "actual_crud_and_denials": True,
            "probe_rows_rolled_back": True,
        }

    def publish_then_submit(self, generation, username, retained):
        # An explicit trusted owner action, separate from the schema deployment.
        deployment = Deployment(self.config, self.state_dir)
        with deployment.locked(), deployment.database.operation_lock():
            assert deployment.state["current"]["id"] == generation["id"]
            deployment.control(
                generation,
                "content_import",
                "/app/content/starter.json",
                "--translations",
                "/app/content/en.json",
            )
        with self.connection(generation) as connection:
            version, data = connection.execute(
                "SELECT v.id,v.data FROM core_problem p JOIN core_problemversion v ON v.id=p.current_id WHERE p.id='train-linear-model' AND p.published"
            ).fetchone()
        assert data["evaluator"] == "python-ml-linear-v1"
        units = len(data["tests"])
        assert 1 <= units <= 8
        client, _ = self.accounts[username]
        headers = dict(retained["headers"], **{"Idempotency-Key": str(uuid.uuid4())})
        payload = json.dumps(
            {"version_id": version, "source": data["reference"], "mode": "submit"}
        ).encode()
        endpoint = "/api/v1/problems/train-linear-model/submissions/"
        queued = self.request_json(client, endpoint, payload, headers, expected=202)
        assert queued["created"]
        result = self.wait_result(client, queued["url"], headers)
        reservation = result["resource_reservation"]
        assert reservation["policy"] == "cpu-invocations-v1" and reservation["state"] == "consumed"
        assert reservation["units"] == units and reservation["execution_started_at"]
        replay = self.request_json(client, endpoint, payload, headers, expected=202)
        assert not replay["created"] and replay["id"] == queued["id"]
        with self.connection(generation) as connection:
            identity = self.user_id(generation, username)
            created = connection.execute(
                "SELECT created_at FROM core_submission WHERE id=%s", (queued["id"],)
            ).fetchone()[0]
            # Reservation belongs to admission's UTC day, including execution
            # that legitimately begins after midnight.
            assert reservation["day"] == created.astimezone(UTC).date().isoformat()
            assert connection.execute(
                "SELECT count(*),coalesce(sum(budget_units),0) FROM core_submission WHERE user_id=%s AND budget_state='consumed'",
                (identity,),
            ).fetchone() == (1, units)
        exported = self.request_json(client, "/accounts/export/", {})
        assert len(exported["submissions"]) == 4
        old = [row for row in exported["submissions"] if row["id"] != queued["id"]]
        assert len(old) == 3 and all(
            row["resource_reservation"] == LEGACY_RESERVATION for row in old
        )
        new = next(row for row in exported["submissions"] if row["id"] == queued["id"])
        assert new["resource_reservation"] == reservation
        assert len(exported["showcase"]["shared_results"]) == 1
        assert client.request(f"/submissions/{queued['id']}/")[0] == 200
        assert retained["token"] not in json.dumps(exported)
        self.result.update(
            expanded_catalogue_published=True,
            content_publication_was_explicit_owner_action=True,
            new_real_specialist_formal_submission_accepted=True,
            new_cpu_reservation={
                "policy": reservation["policy"],
                "units": units,
                "state": "consumed",
            },
            retained_token_and_idempotency_work=True,
            legacy_history_not_recharged=True,
            specialist_not_automatically_shared=True,
        )

    def delete_retained_account(self, generation, username, retained):
        client, password = self.accounts[username]
        identity = self.user_id(generation, username)
        assert (
            client.request("/accounts/delete/", {"password": password, "confirm": "DELETE"})[0]
            == 200
        )
        assert self.user_id(generation, username) is None
        assert client.request(retained["queued"]["url"], headers=retained["headers"])[0] == 401
        assert (
            Client(self.base, self.context).request(f"/showcase/results/{retained['shared']}/")[0]
            == 404
        )
        with self.connection(generation) as connection:
            for table, key in {
                "core_draft": "user_id",
                "core_submission": "user_id",
                "core_enrollment": "user_id",
                "core_accountprofile": "user_id",
                "learning_bookmark": "user_id",
                "learning_note": "user_id",
                "learning_tutorialreading": "user_id",
                "community_post": "author_id",
                "practice_api_accesstoken": "user_id",
                "practice_api_submissionrequest": "user_id",
                "showcase_publicprofile": "user_id",
            }.items():
                assert (
                    connection.execute(
                        sql.SQL("SELECT count(*) FROM {} WHERE {}=%s").format(
                            sql.Identifier(table), sql.Identifier(key)
                        ),
                        (identity,),
                    ).fetchone()[0]
                    == 0
                )
        self.result["retained_account_web_role_delete_cascades"] = True

    def upgrade_rehearsal(self):
        print(
            "Installing verified B2; creating retained private data and real accepted submissions",
            flush=True,
        )
        self.run(*self.apply_args(initial=True, previous=True), log="previous-initial")
        previous = self.journal()["current"]
        old_migrations, old_tables = self.schema_state(previous)
        assert B2_MIGRATIONS <= old_migrations and not C_MIGRATIONS.intersection(old_migrations)
        assert not set(NEW_TABLES).intersection(old_tables)
        catalogue = self.catalogue_snapshot(previous)
        owner_content = self.owner_content_snapshot(previous)
        assert all(row["rows"] > 0 for row in owner_content.values())
        with self.connection(previous) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM core_problem WHERE id='train-linear-model'"
                ).fetchone()[0]
                == 0
            )
        username = self.register("c-upgrade-before")
        retained = self.seed_b2(previous, username)
        before = self.snapshot(previous, [username])
        assert before["tables"]["core_submission"]["rows"] == 3
        print(
            "Applying C migrations; preserving B2 content, private state and historical results",
            flush=True,
        )
        self.run(*self.apply_args(), log="schema-upgrade")
        journal = self.journal()
        upgraded = journal["current"]
        assert journal["operation"]["phase"] == "active"
        assert upgraded["release_sha256"] == self.options.manifest_sha256
        assert upgraded["id"] != previous["id"] and upgraded["database"] != previous["database"]
        new_migrations, new_tables = self.schema_state(upgraded)
        assert old_migrations <= new_migrations and C_MIGRATIONS <= new_migrations - old_migrations
        assert set(NEW_TABLES) <= new_tables - old_tables
        restored = self.snapshot(upgraded, [username])
        assert restored["tables"] == before["tables"] and restored["user_ids"] == before["user_ids"]
        assert all(
            restored["sequences"][key][0] >= value[0] for key, value in before["sequences"].items()
        )
        assert self.catalogue_snapshot(upgraded) == catalogue
        assert self.owner_content_snapshot(upgraded) == owner_content
        with self.connection(upgraded) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM core_submission WHERE budget_state<>'legacy' OR budget_policy<>'' OR budget_day IS NOT NULL OR budget_units<>0 OR execution_started_at IS NOT NULL"
                ).fetchone()[0]
                == 0
            )
        backup = journal["operation"]["backup"]
        assert backup["backup_bytes"] > 0 and backup["sequences"] > 0
        permissions = self.feature_roles(upgraded, username)
        self.check_retained_http(upgraded, username, retained)
        assert self.catalogue_snapshot(upgraded) == catalogue
        assert self.owner_content_snapshot(upgraded) == owner_content
        print(
            "Explicitly publishing reviewed C content, then judging a real reserved ML submission",
            flush=True,
        )
        self.publish_then_submit(upgraded, username, retained)
        published_owner_content = self.owner_content_snapshot(upgraded)
        assert all(
            published_owner_content[table] == owner_content[table]
            for table in B2_OWNER_TABLES
            if table.startswith("projects_")
        )
        with self.connection(upgraded) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM projects_projectstage s JOIN core_problemversion v ON v.id=s.problem_version_id"
                ).fetchone()[0]
                == owner_content["projects_projectstage"]["rows"]
            )
        self.delete_retained_account(upgraded, username, retained)
        self.result.update(
            schema_changing_upgrade_tested=True,
            real_added_migrations=[list(item) for item in sorted(new_migrations - old_migrations)],
            previous_user_draft_and_formal_AC_preserved=True,
            previous_learning_community_tokens_and_showcase_preserved=True,
            project_pins_and_localization_preserved=owner_content,
            original_catalogue_preserved_before_explicit_publication=catalogue,
            no_automatic_content_publication_on_upgrade=True,
            feature_role_permissions_tested=True,
            feature_role_permissions=permissions,
            legacy_cpu_defaults_verified=True,
            retained_private_export_and_locale_via_https=True,
            fresh_feature_browser_tested=False,
            model_provider_tested=False,
            gpu_tested=False,
            backup_restore=backup,
            generations_attempted=2,
            no_journal_or_verdict_edits=True,
            completed_at=datetime.now(UTC).isoformat(),
        )
