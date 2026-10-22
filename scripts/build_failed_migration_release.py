"""Build an intentionally undeployable migration-failure acceptance fixture.

The verified release's captured source is the only application input. A genuine
atomic migration creates a table and then raises a fixed PostgreSQL exception.
Normal release tooling builds and hashes the modified source and both images;
no manifest is edited, no service is started, and the worktree is unchanged.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.release import ReleaseError, sha256, verify, verify_source  # noqa: E402

MIGRATION_NAME = "0003_acceptance_atomic_failure"
EXCEPTION_MARKER = "PRAXIMESH_ACCEPTANCE_MIGRATION_FAILED_AFTER_DDL"
MIGRATION = '''"""INTENTIONALLY FAILING: disposable deployment acceptance fixture only."""

from django.db import migrations


class Migration(migrations.Migration):
    atomic = True

    dependencies = [("core", "0002_admission_and_policy")]

    operations = [
        migrations.RunSQL(
            "CREATE TABLE public.core_acceptance_migration_probe (id bigint PRIMARY KEY)",
            reverse_sql="DROP TABLE public.core_acceptance_migration_probe",
        ),
        migrations.RunSQL(
            """
            DO $praximesh_acceptance$
            BEGIN
                IF to_regclass('public.core_acceptance_migration_probe') IS NULL THEN
                    RAISE EXCEPTION 'PRAXIMESH_ACCEPTANCE_MIGRATION_DDL_MISSING';
                END IF;
                RAISE EXCEPTION 'PRAXIMESH_ACCEPTANCE_MIGRATION_FAILED_AFTER_DDL';
            END;
            $praximesh_acceptance$;
            """,
            reverse_sql=migrations.RunSQL.noop,
        ),
    ]
'''


def capture_source(release, manifest, work):
    archive_path = work / "verified-source.tar.gz"
    shutil.copyfile(release / "source.tar.gz", archive_path)
    # Recheck the captured bytes, rather than trusting that the original path
    # remained unchanged between verification and copying.
    if sha256(archive_path) != manifest["payloads"]["source.tar.gz"]["sha256"]:
        raise ReleaseError("Captured source differs from the verified release")
    verify_source(archive_path, manifest["source_files"])
    source = work / "source"
    source.mkdir(mode=0o700)
    with tarfile.open(archive_path, "r:gz") as archive:
        archive.extractall(source, filter="data")
    return source


def add_failure(source, baseline_digest):
    migrations = source / "core/migrations"
    if not (migrations / "0002_admission_and_policy.py").is_file() or any(
        path.stem[:4].isdigit() and int(path.stem[:4]) >= 3 for path in migrations.glob("*.py")
    ):
        raise ReleaseError("Fixture requires the reviewed 0002 admission migration as its leaf")
    target = migrations / (MIGRATION_NAME + ".py")
    with target.open("x") as stream:
        stream.write(MIGRATION)
    documentation = source / "docs/acceptance-migration-failure.md"
    with documentation.open("x") as stream:
        stream.write(
            "# Intentionally failing migration fixture\n\n"
            "This release exists only for isolated deployment recovery acceptance. "
            "It is intentionally undeployable and must never replace a working site.\n\n"
            f"The captured baseline release manifest SHA-256 is `{baseline_digest}`. "
            "The application source is unchanged except for this note and the fixture migration. "
            "The ordinary release builder creates the source and image identities.\n\n"
            f"Migration `{MIGRATION_NAME}` uses an atomic PostgreSQL transaction. "
            "Its first statement creates `public.core_acceptance_migration_probe`. "
            "Its second statement checks that the table exists, then raises "
            f"`{EXCEPTION_MARKER}`. A failed migration must leave neither the table "
            "nor the migration history entry. Existing user records must survive "
            "automatic recovery of the previous deployment.\n"
        )


def build_fixture(release, digest, bundle, bundle_digest, output):
    if output.exists() or output.is_symlink():
        raise ReleaseError("Refusing to overwrite an existing fixture release")
    manifest = verify(release, digest)
    logs = ROOT / "test-results"
    logs.mkdir(exist_ok=True)
    log = logs / ("migration-fixture-build-" + uuid.uuid4().hex[:12] + ".log")
    print("Building an intentionally failing migration fixture from verified source", flush=True)
    with tempfile.TemporaryDirectory(prefix="praximesh-migration-fixture-", dir="/tmp") as name:
        source = capture_source(release, manifest, Path(name))
        add_failure(source, digest)
        descriptor = os.open(log, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                subprocess.run(
                    [
                        sys.executable,
                        str(source / "scripts/release.py"),
                        "build",
                        "--bundle",
                        str(bundle),
                        "--bundle-sha256",
                        bundle_digest,
                        "--output",
                        str(output),
                    ],
                    cwd=source,
                    env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    timeout=1900,
                    check=True,
                )
        finally:
            # Preserve the normal builder's Docker log before the private
            # captured checkout is removed, including on a failed build.
            with log.open("ab") as stream:
                for child_log in sorted((source / "test-results").glob("release-build-*.log")):
                    with child_log.open("rb") as child:
                        shutil.copyfileobj(child, stream)
    fixture_digest = sha256(output / "release.json")
    fixture = verify(output, fixture_digest)
    if f"core/migrations/{MIGRATION_NAME}.py" not in fixture["source_files"]:
        raise ReleaseError("Built release does not contain the required failing migration")
    print(f"Release directory: {output}")
    print(f"RELEASE_SHA256={fixture_digest}")
    print(f"Failure marker: {EXCEPTION_MARKER}")
    print(f"Build log: {log}")
    return fixture_digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    try:
        build_fixture(
            options.release.resolve(),
            options.sha256,
            options.bundle.resolve(),
            options.bundle_sha256,
            options.output.absolute(),
        )
    except (ReleaseError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Migration fixture build failed ({type(error).__name__}).\n")


if __name__ == "__main__":
    main()
