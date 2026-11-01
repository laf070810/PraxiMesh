"""Read-only application/role preflight; never migrates or publishes readiness."""

from django.db import connection, transaction

from core.models import GPUDevice, ProblemVersion, Submission, WorkerHealth
from scripts.deploy_database import FEATURE_WORKER_PRIVILEGES, WORKER_PRIVILEGES

GRANTS = {
    table: set(privileges.split(", "))
    for group in (WORKER_PRIVILEGES, *FEATURE_WORKER_PRIVILEGES.values())
    for table, privileges in group.items()
}
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def check_database():
    if connection.vendor != "postgresql":
        raise ValueError("Container GPU workers require the application PostgreSQL database")
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET TRANSACTION READ ONLY")
        cursor.execute("SET LOCAL statement_timeout = '5s'")
        cursor.execute("SET LOCAL lock_timeout = '1s'")
        cursor.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls "
            "FROM pg_roles WHERE rolname = current_user"
        )
        flags = cursor.fetchone()
        if flags is None or any(flags):
            raise ValueError("A dedicated unprivileged worker database role is required")
        cursor.execute(
            "SELECT pg_has_role(current_user, datdba, 'USAGE'), "
            "has_schema_privilege(current_user, 'public', 'CREATE') "
            "FROM pg_database WHERE datname = current_database()"
        )
        if any(cursor.fetchone()):
            raise ValueError("Worker database roles must not own or alter the application schema")
        privileges = list(PRIVILEGES)
        if connection.pg_version >= 170000:
            privileges.append("MAINTAIN")
        for function, names in (
            ("has_table_privilege", privileges),
            ("has_any_column_privilege", ["SELECT", "INSERT", "UPDATE", "REFERENCES"]),
        ):
            cursor.execute(
                f"SELECT c.relname,p.priv,{function}(current_user,c.oid,p.priv), "
                f"{function}(current_user,c.oid,p.priv || ' WITH GRANT OPTION') "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "CROSS JOIN unnest(%s::text[]) p(priv) WHERE n.nspname='public' "
                "AND c.relkind IN ('r','p','v','m','f')",
                [names],
            )
            rows = cursor.fetchall()
            if not GRANTS.keys() <= {row[0] for row in rows} or any(
                allowed != (privilege in GRANTS.get(table, set())) or grantable
                for table, privilege, allowed, grantable in rows
            ):
                raise ValueError("Worker database permissions differ from the runtime grants")
        cursor.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relkind='S' AND "
            "has_sequence_privilege(current_user,c.oid,'SELECT,USAGE,UPDATE'))"
        )
        if cursor.fetchone()[0]:
            raise ValueError("Unexpected worker sequence privileges")
        cursor.execute(
            "SELECT has_database_privilege(current_user,current_database(),'CONNECT'), "
            "has_database_privilege(current_user,current_database(),'CREATE'), "
            "has_database_privilege(current_user,current_database(),'TEMP'), "
            "has_schema_privilege(current_user,'public','USAGE'), "
            "EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND has_function_privilege(current_user,p.oid,'EXECUTE'))"
        )
        if cursor.fetchone() != (True, False, False, True, False):
            raise ValueError("Unexpected worker database, schema or function privileges")
        for model in (GPUDevice, ProblemVersion, Submission, WorkerHealth):
            columns = ", ".join(
                connection.ops.quote_name(field.column)
                for field in model._meta.local_concrete_fields
            )
            table = connection.ops.quote_name(model._meta.db_table)
            cursor.execute(f"SELECT {columns} FROM {table} WHERE false")
