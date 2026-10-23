"""C-to-D upgrade with retained private state, real reservations and activity participation."""

import json
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql

from scripts.deploy import Deployment
from scripts.deploy_acceptance import C_MIGRATIONS, D_MIGRATIONS
from scripts.deploy_c_acceptance import NEW_TABLES as C_TABLES
from scripts.deploy_c_acceptance import PRIVILEGES, CRehearsal, row_digest

DEFINITIONS = ("challenges_challenge", "challenges_challengerevision", "challenges_challengetask")
NEW_TABLES = (*DEFINITIONS, "challenges_participation")


class DRehearsal(CRehearsal):
    def __init__(self, options, directory):
        super().__init__(options, directory)
        self.result["scope"] = (
            "C to D schema upgrade; retained C data and real CPU reservations; "
            "explicit owner challenge publication and private real formal progress; "
            "private Docker networks and loopback HTTPS; no GPU or model-provider execution"
        )

    def snapshot(self, generation, usernames):
        result = super().snapshot(generation, usernames)
        with self.connection(generation) as connection:
            # Unlike B2-to-C, preserve and compare every reservation field.
            result["tables"]["core_submission"] = row_digest(
                connection.execute(
                    "SELECT to_jsonb(t) FROM core_submission t WHERE user_id=ANY(%s) ORDER BY id",
                    (result["user_ids"],),
                ).fetchall()
            )
            for table in C_TABLES:
                result["tables"][table] = row_digest(
                    connection.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY id").format(
                            sql.Identifier(table)
                        )
                    ).fetchall()
                )
        return result

    def seed_c(self, generation, username, keeper):
        retained = self.seed_b2(generation, username)
        client, _ = self.accounts[username]
        owner, _ = self.accounts[keeper]
        namespace = "upgrade-" + self.token
        assert (
            owner.request(
                "/organizations/new/",
                {
                    "namespace": namespace,
                    "name": "Retained course organization",
                    "description": "Synthetic upgrade fixture",
                },
            )[0]
            == 200
        )
        assert (
            owner.request(
                f"/organizations/{namespace}/invite/", {"username": username, "role": "instructor"}
            )[0]
            == 200
        )
        with self.connection(generation) as connection:
            invitation = connection.execute(
                "SELECT id FROM organizations_invitation WHERE organization_id=%s AND target_id=%s",
                (namespace, self.user_id(generation, username)),
            ).fetchone()[0]
            version, revision, fingerprint = connection.execute(
                "SELECT v.id,v.revision,v.digest FROM core_problem p JOIN core_problemversion v ON v.id=p.current_id WHERE p.id='sum-positive'"
            ).fetchone()
        assert client.request(f"/organizations/invitations/{invitation}/accept/", {})[0] == 200
        plan = {
            "title": "Retained pinned course",
            "summary": "Private course",
            "units": [
                {
                    "key": "sum",
                    "title": "Sum",
                    "description": "Pinned version",
                    "problem": "sum-positive",
                    "revision": revision,
                    "digest": fingerprint,
                }
            ],
        }
        assert (
            client.request(
                f"/organizations/{namespace}/courses/new/",
                {"slug": "basics", "plan": json.dumps(plan), "acknowledge": "on"},
            )[0]
            == 200
        )
        with self.connection(generation) as connection:
            course, course_revision, course_digest = connection.execute(
                "SELECT c.id,r.id,r.digest FROM organizations_course c JOIN organizations_courserevision r ON r.course_id=c.id WHERE c.organization_id=%s",
                (namespace,),
            ).fetchone()
        assert (
            client.request(
                f"/organizations/{namespace}/course-revisions/{course_revision}/publish/",
                {"digest": course_digest},
            )[0]
            == 200
        )
        assert (
            client.request(f"/organizations/{namespace}/enrollments/{course_revision}/join/", {})[0]
            == 200
        )
        assert client.request("/coaching/goals/", {"targets": "sequences"})[0] == 200
        exported = self.request_json(client, "/accounts/export/", {})
        assert len(exported["submissions"]) == 3
        assert all(
            item["resource_reservation"]["state"] == "consumed" for item in exported["submissions"]
        )
        retained.update(
            export_before=exported,
            course_path=f"/organizations/{namespace}/courses/{course}/revisions/1/",
        )
        return retained

    def feature_roles(self, generation, username):
        with self.connection(generation) as connection:
            assert all(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                == 0
                for table in NEW_TABLES
            )
            sequences = {
                table: connection.execute(
                    "SELECT pg_get_serial_sequence(%s,'id')", (table,)
                ).fetchone()[0]
                for table in NEW_TABLES
            }
        for role in ("web", "worker"):
            with self.runtime_connection(generation, role) as connection:
                for table in NEW_TABLES:
                    allowed = (
                        (
                            {"SELECT"}
                            if table in DEFINITIONS
                            else {"SELECT", "INSERT", "UPDATE", "DELETE"}
                        )
                        if role == "web"
                        else set()
                    )
                    for privilege in PRIVILEGES:
                        actual, grantable = connection.execute(
                            "SELECT has_table_privilege(current_user,%s,%s),has_table_privilege(current_user,%s,%s)",
                            (table, privilege, table, privilege + " WITH GRANT OPTION"),
                        ).fetchone()
                        assert actual == (privilege in allowed) and not grantable
                    for statement, privilege in (
                        ("INSERT INTO {} DEFAULT VALUES", "INSERT"),
                        ("UPDATE {} SET id=id WHERE false", "UPDATE"),
                        ("DELETE FROM {} WHERE false", "DELETE"),
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
                                    raise AssertionError("Unexpected access to challenge table")
                    if sequences[table]:
                        for privilege in ("USAGE", "SELECT", "UPDATE"):
                            expected = (
                                role == "web"
                                and table == "challenges_participation"
                                and privilege != "UPDATE"
                            )
                            actual = connection.execute(
                                "SELECT has_sequence_privilege(current_user,%s,%s)",
                                (sequences[table], privilege),
                            ).fetchone()[0]
                            assert actual == expected
        return {
            "web_read_only_tables": list(DEFINITIONS),
            "web_crud_tables": ["challenges_participation"],
            "worker_access_denied": list(NEW_TABLES),
            "exact_table_and_sequence_privileges": True,
            "actual_denial_statements": True,
        }

    def activity_progress(self, generation, username, retained, other_username):
        # Publish one explicit reviewed synthetic activity through the normal owner
        # command. No definition, participation or verdict is inserted via SQL.
        with self.connection(generation) as connection:
            version, number, fingerprint = connection.execute(
                "SELECT v.id,v.revision,v.digest FROM core_problem p JOIN core_problemversion v ON v.id=p.current_id WHERE p.id='sum-positive'"
            ).fetchone()
        now = datetime.now(UTC)
        slug = "upgrade-activity-" + self.token
        package = {
            "schema_version": 1,
            "challenges": [
                {
                    "id": slug,
                    "revision": 1,
                    "title": "Upgrade activity",
                    "summary": "Private formal evidence",
                    "rules": "Voluntary; pinned task after joining within the activity window.",
                    "starts_at": (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "ends_at": (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "tasks": [
                        {
                            "id": "sum",
                            "title": "Sum",
                            "description": "Pinned public task",
                            "problem": "sum-positive",
                            "problem_revision": number,
                            "problem_digest": fingerprint,
                        }
                    ],
                }
            ],
        }
        program = (
            "import json\nfrom challenges.catalog import import_package\nimport_package(json.loads("
            + repr(json.dumps(package))
            + "))\n"
        )
        deployment = Deployment(self.config, self.state_dir)
        with deployment.locked(), deployment.database.operation_lock():
            deployment.control(generation, "shell", "-c", program)
        client, _ = self.accounts[username]
        path = f"/challenges/{slug}/revisions/1/"
        assert client.request(path)[0] == 200
        assert b'id="challenge-progress"' not in client.request(path)[2]
        assert client.request(path + "participate/", {"action": "join"})[0] == 200
        assert b"0 / 1" in client.request(path)[2]
        result_id = self.submit_and_wait(client)
        assert b"1 / 1" in client.request(path)[2]
        other, _ = self.accounts[other_username]
        assert other.request(f"/submissions/{result_id}/")[0] == 404
        exported = self.request_json(client, "/accounts/export/", {})
        assert len(exported["learning"]["challenges"]) == 1
        facts = {item["id"]: item for item in exported["learning"]["achievements"]}
        assert (
            facts["challenge-complete-v1"]["achieved"] and facts["five-problems-v1"]["count"] == 1
        )
        previous_reservations = {
            item["id"]: item["resource_reservation"]
            for item in retained["export_before"]["submissions"]
        }
        current = {item["id"]: item["resource_reservation"] for item in exported["submissions"]}
        assert all(current[key] == value for key, value in previous_reservations.items())
        assert current[result_id]["state"] == "consumed" and current[result_id]["units"] > 0
        assert client.request(path + "participate/", {"action": "leave"})[0] == 200
        assert client.request(path + "participate/", {"action": "join"})[0] == 200
        assert b"0 / 1" in client.request(path)[2]
        identity = self.user_id(generation, username)
        self.delete_retained_account(generation, username, retained)
        with self.connection(generation) as connection:
            for table in (
                "challenges_participation",
                "coaching_learningplan",
                "organizations_courseenrollment",
                "organizations_membership",
            ):
                assert (
                    connection.execute(
                        sql.SQL("SELECT count(*) FROM {} WHERE user_id=%s").format(
                            sql.Identifier(table)
                        ),
                        (identity,),
                    ).fetchone()[0]
                    == 0
                )
            assert (
                connection.execute(
                    "SELECT count(*) FROM challenges_challenge WHERE id=%s", (slug,)
                ).fetchone()[0]
                == 1
            )
        self.result.update(
            explicit_challenge_publication=True,
            real_post_join_formal_result_counted=True,
            pre_join_results_excluded=True,
            old_cpu_reservations_unchanged=True,
            rejoining_resets_activity_progress=True,
            private_achievement_and_participation_export_verified=True,
            account_deletion_clears_participation_and_private_c_state=True,
            challenge_definition_survives_account_deletion=True,
        )

    def upgrade_rehearsal(self):
        print(
            "Installing verified C and creating private courses, plans and real formal results",
            flush=True,
        )
        self.run(*self.apply_args(initial=True, previous=True), log="previous-initial")
        previous = self.journal()["current"]
        old_migrations, old_tables = self.schema_state(previous)
        assert C_MIGRATIONS <= old_migrations and not D_MIGRATIONS.intersection(old_migrations)
        assert not set(NEW_TABLES).intersection(old_tables)
        username = self.register("d-upgrade-before")
        keeper = self.register("d-course-owner", judge=False)
        retained = self.seed_c(previous, username, keeper)
        before = self.snapshot(previous, [username, keeper])
        assert before["tables"]["organizations_courserevision"]["rows"] == 1
        assert before["tables"]["coaching_learningplan"]["rows"] == 1
        catalogue, owner_content = (
            self.catalogue_snapshot(previous),
            self.owner_content_snapshot(previous),
        )
        print("Applying D migration without automatically publishing activities", flush=True)
        self.run(*self.apply_args(), log="schema-upgrade")
        journal = self.journal()
        upgraded = journal["current"]
        assert (
            journal["operation"]["phase"] == "active"
            and upgraded["release_sha256"] == self.options.manifest_sha256
        )
        assert upgraded["id"] != previous["id"] and upgraded["database"] != previous["database"]
        new_migrations, new_tables = self.schema_state(upgraded)
        assert old_migrations <= new_migrations and D_MIGRATIONS <= new_migrations - old_migrations
        assert set(NEW_TABLES) <= new_tables - old_tables
        restored = self.snapshot(upgraded, [username, keeper])
        assert restored["tables"] == before["tables"] and restored["user_ids"] == before["user_ids"]
        assert all(
            restored["sequences"][key][0] >= value[0] for key, value in before["sequences"].items()
        )
        assert (
            self.catalogue_snapshot(upgraded) == catalogue
            and self.owner_content_snapshot(upgraded) == owner_content
        )
        permissions = self.feature_roles(upgraded, username)
        client, _ = self.accounts[username]
        exported = self.request_json(client, "/accounts/export/", {})
        for key in (
            "submissions",
            "drafts",
            "paths",
            "community",
            "practice_api",
            "showcase",
            "organizations",
            "coaching",
        ):
            assert exported[key] == retained["export_before"][key]
        for key in ("notes", "bookmarks", "reading"):
            assert exported["learning"][key] == retained["export_before"]["learning"][key]
        assert exported["learning"]["challenges"] == []
        assert client.request(retained["course_path"])[0] == 200
        replay = self.request_json(
            client, retained["endpoint"], retained["payload"], retained["headers"], expected=202
        )
        assert not replay["created"] and replay["id"] == retained["queued"]["id"]
        assert client.request("/practice/")[1]["Content-Language"] == "en"
        self.activity_progress(upgraded, username, retained, keeper)
        self.result.update(
            schema_changing_upgrade_tested=True,
            real_added_migrations=[list(item) for item in sorted(new_migrations - old_migrations)],
            previous_private_courses_plans_and_reservations_preserved=True,
            original_catalogue_preserved_before_explicit_publication=catalogue,
            no_automatic_content_publication_on_upgrade=True,
            feature_role_permissions_tested=True,
            feature_role_permissions=permissions,
            backup_restore=journal["operation"]["backup"],
            generations_attempted=2,
            no_journal_or_verdict_edits=True,
            completed_at=datetime.now(UTC).isoformat(),
        )
