"""Initialize the Compose preview before importing Django settings."""

import fcntl
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PreviewError(ValueError):
    pass


def private_file(path):
    if path.is_symlink() or not path.is_file():
        raise PreviewError(f"Expected a regular preview file: {path.name}")
    info = path.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise PreviewError(f"Preview file must be owned by this user and private: {path.name}")


def secret_key(directory, provided):
    if provided:
        if len(provided) < 50:
            raise PreviewError("APP_SECRET_KEY must contain at least 50 characters.")
        return provided
    path = directory / "preview-secret.key"
    if not path.exists() and not path.is_symlink():
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".preview-key-") as temporary:
            temporary.write(secrets.token_urlsafe(64).encode())
            temporary.flush()
            os.fsync(temporary.fileno())
            try:
                os.link(temporary.name, path)
            except FileExistsError:
                pass
    private_file(path)
    value = path.read_text(encoding="ascii")
    if len(value) < 50 or not value.isascii() or any(character.isspace() for character in value):
        raise PreviewError("Stored preview key is invalid; refusing to replace it.")
    return value


def manage(environment, *arguments):
    subprocess.run(
        [sys.executable, str(ROOT / "manage.py"), *arguments],
        cwd=ROOT,
        env=environment,
        check=True,
    )


def prepare(directory, *, services=False):
    environment = dict(os.environ)
    if (
        environment.get("PRAXIMESH_MODE", "local") != "local"
        or environment.get("DATABASE_URL")
        or (not services and environment.get("JUDGE_ENABLED", "0") != "0")
        or (not services and environment.get("COACHING_ENABLED", "0") != "0")
    ):
        raise PreviewError("Preview requires local SQLite mode with judging and AI disabled.")
    if directory.is_symlink() or not directory.is_dir():
        raise PreviewError("Preview data directory must be an existing real directory.")
    database = directory / "praximesh.sqlite3"
    lock = os.open(directory / "preview.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock, "a") as locked:
        fcntl.flock(locked, fcntl.LOCK_EX)
        if database.is_symlink() or (database.exists() and not database.is_file()):
            raise PreviewError("Preview database must be a regular file.")
        environment.update(
            PRAXIMESH_MODE="local",
            DATABASE_URL="",
            SQLITE_PATH=str(database),
            APP_SECRET_KEY=secret_key(directory, environment.get("APP_SECRET_KEY", "")),
            JUDGE_ENABLED="1" if services else "0",
            COACHING_ENABLED=environment.get("COACHING_ENABLED", "0") if services else "0",
        )
        if database.exists():
            manage(environment, "migrate", "--noinput")
            print("Preview: existing database preserved; no content imported.", flush=True)
        else:
            with tempfile.TemporaryDirectory(dir=directory, prefix=".preview-bootstrap-") as stage:
                staged = Path(stage) / "praximesh.sqlite3"
                initial = dict(environment, SQLITE_PATH=str(staged))
                manage(initial, "migrate", "--noinput")
                manage(initial, "content_import", "--translations", str(ROOT / "content/en.json"))
                manage(initial, "project_import", str(ROOT / "content/projects.json"))
                manage(initial, "challenge_import", str(ROOT / "content/challenges.json"))
                staged.chmod(0o600)
                with staged.open("rb") as stream:
                    os.fsync(stream.fileno())
                os.link(staged, database)
                print(
                    "Preview: initialized the complete starter catalog in a new database.",
                    flush=True,
                )
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return environment


def main():
    try:
        environment = prepare(Path("/data"))
    except (PreviewError, OSError, UnicodeError, subprocess.CalledProcessError) as error:
        print(f"Preview startup failed: {error}", file=sys.stderr)
        return 1
    os.execvpe(
        "gunicorn",
        [
            "gunicorn",
            "praximesh.wsgi:application",
            "--bind=0.0.0.0:8000",
            "--workers=1",
            "--threads=2",
            "--worker-tmp-dir=/tmp",
            "--access-logfile=-",
        ],
        environment,
    )


if __name__ == "__main__":
    sys.exit(main())
