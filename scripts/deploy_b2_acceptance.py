"""B1 to B2 migration rehearsal using real retained data and runtime roles."""

import hashlib
import json
import math
import re
import time
import uuid
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg import sql

from scripts.deploy_acceptance import B2_MIGRATIONS, CORRECT, Rehearsal
from scripts.deploy_database import FEATURE_READ_TABLES, FEATURE_TABLES
from scripts.production_acceptance import Client

PRIVATE = tuple(
    table for app in ("practice_api", "showcase") for table in FEATURE_TABLES[(app, "0001_initial")]
)
READ_ONLY = tuple(
    table
    for app in ("projects", "localization")
    for table in FEATURE_READ_TABLES[(app, "0001_initial")]
)


def wait_for_api_result(client, path, headers, *, timeout=120):
    """Poll within the ordinary bearer quota; rate limiting never renews the deadline."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, response_headers, body = client.request(path, headers=headers)
        if time.monotonic() >= deadline:
            break
        delay = 2.0
        if status == 429:
            try:
                retry = float(response_headers.get("Retry-After", "2"))
            except (TypeError, ValueError):
                retry = 2.0
            if math.isfinite(retry):
                delay = min(60.0, max(2.0, retry))
        else:
            assert status == 200, "Unexpected HTTPS result status"
            result = json.loads(body)
            if result["status"] == "finished":
                assert result["verdict"] == "accepted" and result["formal_result"] is True
                return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(delay, remaining))
    raise AssertionError("Real authenticated submission did not complete before its deadline")


class B2Rehearsal(Rehearsal):
    def __init__(self, options, directory):
        super().__init__(options, directory)
        self.result["scope"] = (
            "B1 to B2 schema upgrade; synthetic private data; original catalogue retained; "
            "private Docker networks; loopback HTTPS"
        )

    def snapshot(self, generation, usernames):
        result = super().snapshot(generation, usernames)
        with self.connection(generation) as connection:
            for table, key in {
                "learning_bookmark": "user_id",
                "learning_note": "user_id",
                "learning_tutorialreading": "user_id",
                "community_post": "author_id",
                "community_comment": "author_id",
                "community_report": "reporter_id",
                "community_appeal": "author_id",
                "community_moderationevent": "actor_id",
            }.items():
                rows = connection.execute(
                    sql.SQL("SELECT to_jsonb(t) FROM {} t WHERE {}=ANY(%s) ORDER BY id").format(
                        sql.Identifier(table), sql.Identifier(key)
                    ),
                    (result["user_ids"],),
                ).fetchall()
                data = json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
                result["tables"][table] = {
                    "rows": len(rows),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
        return result

    def feature_roles(self, generation, username):
        identity = self.user_id(generation, username)
        now = datetime.now(UTC)
        with self.connection(generation) as connection:
            submission = connection.execute(
                "SELECT id FROM core_submission WHERE user_id=%s AND verdict='accepted' "
                "ORDER BY created_at LIMIT 1",
                (identity,),
            ).fetchone()[0]
            assert all(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                == 0
                for table in PRIVATE
            )
        rows = {
            "practice_api_accesstoken": {
                "id": uuid.uuid4(),
                "user_id": identity,
                "label": "Disposable role probe",
                "secret_digest": "0" * 64,
                "can_submit": True,
                "can_read_results": True,
                "created_at": now,
                "expires_at": now + timedelta(days=1),
                "revoked_at": None,
            },
            "practice_api_submissionrequest": {
                "user_id": identity,
                "submission_id": submission,
                "key": uuid.uuid4(),
                "request_digest": "0" * 64,
                "created_at": now,
            },
            "showcase_publicprofile": {
                "user_id": identity,
                "is_public": False,
                "public_name": "",
                "bio": "",
                "revision": 1,
                "updated_at": now,
            },
            "showcase_sharedresult": {
                "id": uuid.uuid4(),
                "submission_id": submission,
                "is_public": False,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            },
        }
        privileges = {
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "TRUNCATE",
            "REFERENCES",
            "TRIGGER",
            "MAINTAIN",
        }
        for role in ("web", "worker"):
            with self.runtime_connection(generation, role) as connection:
                assert (
                    connection.execute("SELECT current_user").fetchone()[0]
                    == f"px_{self.instance}_{role}"
                )
                for table in (*PRIVATE, *READ_ONLY):
                    allowed = (
                        (
                            {"SELECT", "INSERT", "UPDATE", "DELETE"}
                            if table in PRIVATE
                            else {"SELECT"}
                        )
                        if role == "web"
                        else set()
                    )
                    for privilege in privileges:
                        actual, delegable = connection.execute(
                            "SELECT has_table_privilege(current_user,%s,%s), has_table_privilege(current_user,%s,%s)",
                            (table, privilege, table, privilege + " WITH GRANT OPTION"),
                        ).fetchone()
                        assert actual == (privilege in allowed) and not delegable
                    if "SELECT" in allowed:
                        connection.execute(
                            sql.SQL("SELECT id FROM {} LIMIT 1").format(sql.Identifier(table))
                        )
                    statements = [
                        "INSERT INTO {} DEFAULT VALUES",
                        "UPDATE {} SET id=id WHERE FALSE",
                        "DELETE FROM {} WHERE FALSE",
                    ]
                    if role == "worker":
                        statements.append("SELECT id FROM {} LIMIT 0")
                    if role == "worker" or table in READ_ONLY:
                        for statement in statements:
                            try:
                                with connection.transaction(force_rollback=True):
                                    connection.execute(
                                        sql.SQL(statement).format(sql.Identifier(table))
                                    )
                            except psycopg.errors.InsufficientPrivilege:
                                pass
                            else:
                                raise AssertionError("Unapproved runtime access to a B2 table")
                if role == "web":
                    with connection.transaction(force_rollback=True):
                        for table, values in rows.items():
                            row_id = connection.execute(
                                sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
                                    sql.Identifier(table),
                                    sql.SQL(",").join(map(sql.Identifier, values)),
                                    sql.SQL(",").join(sql.Placeholder() for _ in values),
                                ),
                                tuple(values.values()),
                            ).fetchone()[0]
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
            assert all(
                connection.execute(
                    sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
                ).fetchone()[0]
                == 0
                for table in PRIVATE
            )
        return {
            "private_tables": list(PRIVATE),
            "read_only_tables": list(READ_ONLY),
            "web_real_crud_rolled_back": True,
            "web_content_writes_denied": True,
            "worker_real_crud_denied": True,
            "no_extra_table_privileges_or_grant_options": True,
        }

    def b2_http(self, generation, username):
        client, password = self.accounts[username]
        for endpoint in (
            "/accounts/tokens/",
            "/showcase/settings/",
            "/showcase/mine/",
            "/projects/",
        ):
            assert client.request(endpoint)[0] == 200
        status, _, body = client.request(
            "/accounts/tokens/",
            {
                "label": "Upgrade acceptance",
                "days": 1,
                "can_submit": "on",
                "can_read_results": "on",
                "password": password,
            },
        )
        assert status == 200
        matches = re.findall(rb"pm_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}", body)
        assert len(matches) == 1
        raw = matches[0].decode()
        assert raw.encode() not in client.request("/accounts/tokens/")[2]
        headers = {
            "Authorization": "Bearer " + raw,
            "Content-Type": "application/json",
            "Idempotency-Key": str(uuid.uuid4()),
        }
        payload = json.dumps(
            {"version_id": int(client.version), "source": CORRECT, "mode": "submit"}
        ).encode()
        endpoint = "/api/v1/problems/sum-positive/submissions/"
        status, _, body = client.request(endpoint, payload, headers)
        assert status == 202
        queued = json.loads(body)
        status, _, body = client.request(endpoint, payload, headers)
        replay = json.loads(body)
        assert (
            status == 202
            and queued["created"]
            and not replay["created"]
            and queued["id"] == replay["id"]
        )
        result_path = f"/api/v1/submissions/{queued['id']}/"
        wait_for_api_result(client, result_path, headers)
        assert (
            client.request(
                "/showcase/settings/",
                {
                    "revision": 0,
                    "public_name": "Upgrade public result",
                    "bio": "",
                    "is_public": "on",
                    "confirm_public": "on",
                },
            )[0]
            == 200
        )
        with self.connection(generation) as connection:
            retained = connection.execute(
                "SELECT id FROM core_submission WHERE user_id=%s AND verdict='accepted' ORDER BY created_at LIMIT 1",
                (self.user_id(generation, username),),
            ).fetchone()[0]
        for submission in (str(retained), queued["id"]):
            assert (
                client.request(
                    f"/showcase/share/{submission}/",
                    {
                        "profile_revision": 1,
                        "entry_revision": 0,
                        "confirm_public": "on",
                    },
                )[0]
                == 200
            )
        status, _, body = client.request("/accounts/export/", {})
        assert status == 200 and raw.encode() not in body
        exported = json.loads(body)
        assert len(exported["practice_api"]["tokens"]) == 1
        assert len(exported["practice_api"]["submission_requests"]) == 1
        assert len(exported["showcase"]["shared_results"]) == 2
        guest = Client(self.base, self.context)
        assert guest.request(f"/showcase/profiles/{username}/")[0] == 200
        assert client.request("/showcase/settings/close/", {"revision": 1})[0] == 200
        assert guest.request(f"/showcase/profiles/{username}/")[0] == 404
        with self.connection(generation) as connection:
            token_id = connection.execute(
                "SELECT id FROM practice_api_accesstoken WHERE user_id=%s",
                (self.user_id(generation, username),),
            ).fetchone()[0]
        assert client.request(f"/accounts/tokens/{token_id}/revoke/", {})[0] == 200
        assert client.request(result_path, headers=headers)[0] == 401
        self.result.update(
            real_token_idempotent_submit_read_and_revoke=True,
            old_and_new_runtime_results_shared_by_explicit_choice=True,
            profile_close_withdraws_results=True,
            export_excludes_token_secret=True,
        )

    def upgrade_rehearsal(self):
        print(
            "Installing verified B1 and creating private notes, drafts and real accepted submissions",
            flush=True,
        )
        self.run(*self.apply_args(initial=True, previous=True), log="previous-initial")
        previous = self.journal()["current"]
        old_migrations, old_tables = self.schema_state(previous)
        assert not B2_MIGRATIONS.intersection(old_migrations)
        assert not set((*PRIVATE, *READ_ONLY)).intersection(old_tables)
        catalogue = self.catalogue_snapshot(previous)
        assert catalogue["core_problem"]["rows"] == 18
        username = self.register("b2-upgrade-before")
        self.upgraded_http(previous, username)
        before = self.snapshot(previous, [username])
        assert before["tables"]["core_submission"]["rows"] == 2
        assert before["tables"]["learning_note"]["rows"] == 1
        assert before["tables"]["community_post"]["rows"] == 1
        print("Applying B2 migrations while preserving the published B1 catalogue", flush=True)
        self.run(*self.apply_args(), log="schema-upgrade")
        journal = self.journal()
        upgraded = journal["current"]
        assert journal["operation"]["phase"] == "active"
        assert upgraded["release_sha256"] == self.options.manifest_sha256
        assert upgraded["id"] != previous["id"] and upgraded["database"] != previous["database"]
        new_migrations, new_tables = self.schema_state(upgraded)
        assert old_migrations <= new_migrations and B2_MIGRATIONS <= new_migrations - old_migrations
        assert set((*PRIVATE, *READ_ONLY)) <= new_tables - old_tables
        restored = self.snapshot(upgraded, [username])
        assert restored["tables"] == before["tables"] and restored["user_ids"] == before["user_ids"]
        assert all(
            restored["sequences"][key][0] >= value[0] for key, value in before["sequences"].items()
        )
        assert self.catalogue_snapshot(upgraded) == catalogue
        backup = journal["operation"]["backup"]
        assert backup["backup_bytes"] > 0 and backup["sequences"] > 0
        permissions = self.feature_roles(upgraded, username)
        with self.connection(upgraded) as connection:
            assert connection.execute("SELECT count(*) FROM projects_project").fetchone()[0] == 0
            assert (
                connection.execute(
                    "SELECT count(*) FROM localization_problemtranslation"
                ).fetchone()[0]
                == 0
            )
            assert (
                connection.execute(
                    "SELECT count(*) FROM localization_knowledgesnapshot"
                ).fetchone()[0]
                == catalogue["core_knowledgenode"]["rows"]
            )
        self.b2_http(upgraded, username)
        self.browser()
        assert self.catalogue_snapshot(upgraded) == catalogue
        self.result.update(
            schema_changing_upgrade_tested=True,
            real_added_migrations=[list(item) for item in sorted(new_migrations - old_migrations)],
            previous_user_draft_and_formal_AC_preserved=True,
            previous_learning_and_community_private_data_preserved=True,
            original_catalogue_preserved=catalogue,
            feature_role_permissions_tested=True,
            feature_role_permissions=permissions,
            translation_snapshot_backfill_verified=True,
            new_projects_and_translations_not_automatically_published=True,
            upgraded_private_features_and_export_via_https=True,
            retained_user_new_real_https_submission_accepted=True,
            browser_real_https_run_wrong_accepted=True,
            backup_restore=backup,
            generations_attempted=2,
            no_journal_or_verdict_edits=True,
            completed_at=datetime.now(UTC).isoformat(),
        )
