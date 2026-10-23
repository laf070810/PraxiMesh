"""Safe fixture construction and preflight, without building or starting images."""

import ast

import pytest

from scripts.build_failed_migration_release import (
    EXCEPTION_MARKER,
    add_failure,
    next_migration,
)
from scripts.deploy_acceptance import failure_migration_name
from scripts.release import ReleaseError


def migration(directory, name, dependencies):
    path = directory / f"{name}.py"
    path.write_text(
        "raise RuntimeError('Captured source must not execute on the host')\n"
        "class Migration:\n"
        f"    dependencies = {dependencies}\n"
    )
    return path


def source(tmp_path, *, c=False):
    directory = tmp_path / "core/migrations"
    directory.mkdir(parents=True)
    (tmp_path / "docs").mkdir()
    migration(
        directory, "0001_initial", "[migrations.swappable_dependency(settings.AUTH_USER_MODEL)]"
    )
    migration(directory, "0002_admission_and_policy", "[('core', '0001_initial')]")
    if c:
        migration(
            directory,
            "0003_cpu_reservations",
            "[('core', '0002_admission_and_policy'), migrations.swappable_dependency(settings.AUTH_USER_MODEL)]",
        )
    return directory


@pytest.mark.parametrize("c", [False, True])
def test_failure_migration_appends_to_verified_b2_or_c_graph_without_executing(tmp_path, c):
    directory = source(tmp_path, c=c)
    original = {path.name: path.read_bytes() for path in directory.iterdir()}
    number = 4 if c else 3
    parent = "0003_cpu_reservations" if c else "0002_admission_and_policy"
    expected = f"{number:04d}_acceptance_atomic_failure"
    assert next_migration(directory) == (expected, parent)
    assert add_failure(tmp_path, "a" * 64) == expected
    for name, data in original.items():
        assert (directory / name).read_bytes() == data
    text = (directory / f"{expected}.py").read_text()
    tree = ast.parse(text)
    cls = next(item for item in tree.body if isinstance(item, ast.ClassDef))
    assignments = {
        item.targets[0].id: item.value for item in cls.body if isinstance(item, ast.Assign)
    }
    assert ast.literal_eval(assignments["dependencies"]) == [("core", parent)]
    assert ast.literal_eval(assignments["atomic"]) is True
    assert "CREATE TABLE public.core_acceptance_migration_probe" in text
    assert EXCEPTION_MARKER in text
    assert text.index("CREATE TABLE") < text.index("RAISE EXCEPTION '" + EXCEPTION_MARKER)
    assert expected in (tmp_path / "docs/acceptance-migration-failure.md").read_text()
    assert (
        failure_migration_name({"source_files": {f"core/migrations/{expected}.py": "a" * 64}})
        == expected
    )
    with pytest.raises(ReleaseError, match="normal numbered"):
        next_migration(directory)


@pytest.mark.parametrize(
    "kind",
    [
        "two-leaves",
        "missing-parent",
        "cycle",
        "dynamic",
        "replaces",
        "run-before",
        "invalid-name",
        "duplicate-dependencies",
    ],
)
def test_ambiguous_or_dynamic_graph_fails_closed(tmp_path, kind):
    directory = source(tmp_path)
    if kind == "two-leaves":
        migration(directory, "0003_other", "[('core', '0001_initial')]")
    elif kind == "missing-parent":
        migration(directory, "0003_other", "[('core', '0999_absent')]")
    elif kind == "cycle":
        migration(directory, "0003_a", "[('core', '0004_b')]")
        migration(directory, "0004_b", "[('core', '0003_a')]")
    elif kind == "dynamic":
        migration(directory, "0003_other", "compute_dependencies()")
    elif kind in {"replaces", "run-before", "duplicate-dependencies"}:
        path = directory / "0002_admission_and_policy.py"
        attribute = {
            "replaces": "replaces",
            "run-before": "run_before",
            "duplicate-dependencies": "dependencies",
        }[kind]
        path.write_text(path.read_text() + f"    {attribute} = []\n")
    else:
        migration(directory, "bad_name", "[]")
    with pytest.raises(ReleaseError):
        next_migration(directory)


@pytest.mark.parametrize(
    "paths",
    [
        [],
        ["core/migrations/0002_acceptance_atomic_failure.py"],
        ["other/migrations/0003_acceptance_atomic_failure.py"],
        [
            "core/migrations/0003_acceptance_atomic_failure.py",
            "core/migrations/0004_acceptance_atomic_failure.py",
        ],
    ],
)
def test_fixture_preflight_rejects_missing_or_ambiguous_failure_migration(paths):
    with pytest.raises(ValueError, match="exactly one"):
        failure_migration_name({"source_files": dict.fromkeys(paths, "a" * 64)})
