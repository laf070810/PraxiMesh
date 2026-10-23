"""D/E-to-F schema upgrade retaining private records and adding versioned feedback."""

from datetime import UTC, datetime

from psycopg import sql

from scripts.deploy_acceptance import D_MIGRATIONS, F_MIGRATIONS, UPGRADE_TABLES
from scripts.deploy_c_acceptance import row_digest
from scripts.deploy_d_acceptance import NEW_TABLES as D_TABLES
from scripts.deploy_d_acceptance import DRehearsal


class FRehearsal(DRehearsal):
    def __init__(self, options, directory):
        super().__init__(options, directory)
        self.result["scope"] = (
            "D/E to F versioned-feedback migration; retained catalogue and private records; "
            "real WASI results and HTTPS feedback publication, withdrawal and account deletion"
        )

    def snapshot(self, generation, usernames):
        result = super().snapshot(generation, usernames)
        with self.connection(generation) as connection:
            for table in D_TABLES:
                result["tables"][table] = row_digest(
                    connection.execute(
                        sql.SQL("SELECT to_jsonb(t) FROM {} t ORDER BY id").format(
                            sql.Identifier(table)
                        )
                    ).fetchall()
                )
        return result

    def community_roles(self, generation):
        result = {}
        for role in ("web", "worker"):
            with self.runtime_connection(generation, role) as connection:
                result[role] = {
                    table: {
                        privilege: connection.execute(
                            "SELECT has_table_privilege(current_user,%s,%s)", (table, privilege)
                        ).fetchone()[0]
                        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
                    }
                    for table in UPGRADE_TABLES
                }
        assert all(all(value.values()) for value in result["web"].values())
        assert not any(any(value.values()) for value in result["worker"].values())
        return result

    def upgrade_rehearsal(self):
        print(
            "Installing the verified previous release and creating real private records", flush=True
        )
        self.run(*self.apply_args(initial=True, previous=True), log="previous-initial")
        previous = self.journal()["current"]
        old_migrations, old_tables = self.schema_state(previous)
        assert D_MIGRATIONS <= old_migrations and not F_MIGRATIONS.intersection(old_migrations)
        username = self.register("f-upgrade-before")
        keeper = self.register("f-course-owner", judge=False)
        retained = self.seed_c(previous, username, keeper)
        before = self.snapshot(previous, [username, keeper])
        catalogue = self.catalogue_snapshot(previous)
        owner_content = self.owner_content_snapshot(previous)
        permissions = self.community_roles(previous)
        print(
            "Applying the feedback migration without changing catalogue or private state",
            flush=True,
        )
        self.run(*self.apply_args(), log="schema-upgrade")
        journal = self.journal()
        upgraded = journal["current"]
        assert journal["operation"]["phase"] == "active"
        assert upgraded["release_sha256"] == self.options.manifest_sha256
        assert upgraded["id"] != previous["id"] and upgraded["database"] != previous["database"]
        new_migrations, new_tables = self.schema_state(upgraded)
        assert new_migrations - old_migrations == F_MIGRATIONS
        assert old_migrations <= new_migrations and old_tables == new_tables
        restored = self.snapshot(upgraded, [username, keeper])
        assert restored["tables"] == before["tables"] and restored["user_ids"] == before["user_ids"]
        assert all(
            restored["sequences"][key][0] >= value[0] for key, value in before["sequences"].items()
        )
        assert self.catalogue_snapshot(upgraded) == catalogue
        assert self.owner_content_snapshot(upgraded) == owner_content
        assert self.community_roles(upgraded) == permissions
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
        for key in ("notes", "bookmarks", "reading", "challenges"):
            assert exported["learning"][key] == retained["export_before"]["learning"][key]
        assert client.request(retained["course_path"])[0] == 200
        replay = self.request_json(
            client, retained["endpoint"], retained["payload"], retained["headers"], expected=202
        )
        assert not replay["created"] and replay["id"] == retained["queued"]["id"]
        self.browser(problem_feedback=True)
        assert self.catalogue_snapshot(upgraded) == catalogue
        self.delete_retained_account(upgraded, username, retained)
        self.result.update(
            schema_changing_upgrade_tested=True,
            real_added_migrations=[list(item) for item in sorted(new_migrations - old_migrations)],
            previous_private_courses_plans_and_reservations_preserved=True,
            no_automatic_content_publication_on_upgrade=True,
            unchanged_tables_and_community_permissions=True,
            retained_token_idempotency_and_private_export=True,
            feature_role_permissions_tested=True,
            feature_role_permissions=permissions,
            backup_restore=journal["operation"]["backup"],
            generations_attempted=2,
            no_journal_or_verdict_edits=True,
            completed_at=datetime.now(UTC).isoformat(),
        )
