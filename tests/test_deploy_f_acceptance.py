"""Upgrade source-contract checks; actual migration evidence uses frozen images."""

import copy
from types import SimpleNamespace

import pytest

from scripts.deploy_acceptance import (
    B2_MIGRATIONS,
    C_MIGRATIONS,
    D_MIGRATIONS,
    F_MIGRATIONS,
    UPGRADE_MIGRATIONS,
    validate_upgrade_manifests,
)
from scripts.deploy_f_acceptance import FRehearsal


def manifests():
    previous = {
        "source_files": {
            f"{app}/migrations/{name}.py": "a" * 64
            for app, name in UPGRADE_MIGRATIONS | B2_MIGRATIONS | C_MIGRATIONS | D_MIGRATIONS
        },
        "images": {"web": "sha256:" + "a" * 64},
    }
    target = copy.deepcopy(previous)
    target["images"]["web"] = "sha256:" + "b" * 64
    target["source_files"]["community/migrations/0003_problem_feedback.py"] = "c" * 64
    return previous, target


def test_feedback_upgrade_requires_new_migration_and_preserved_history():
    assert validate_upgrade_manifests(*manifests()) == "f"


@pytest.mark.parametrize(
    "change", ["already-f", "missing-target", "edited-history", "deleted-history", "same-image"]
)
def test_feedback_upgrade_rejects_false_or_incompatible_pair(change):
    previous, target = manifests()
    path = "community/migrations/0003_problem_feedback.py"
    if change == "already-f":
        previous["source_files"][path] = target["source_files"][path]
    elif change == "missing-target":
        target["source_files"].pop(path)
    elif change == "edited-history":
        target["source_files"]["community/migrations/0001_initial.py"] = "d" * 64
    elif change == "deleted-history":
        target["source_files"].pop("core/migrations/0003_cpu_reservations.py")
    else:
        target["images"]["web"] = previous["images"]["web"]
    with pytest.raises(ValueError):
        validate_upgrade_manifests(previous, target)


def test_feedback_rehearsal_starts_unverified_and_does_not_republish(tmp_path):
    options = SimpleNamespace(
        release=tmp_path / "f",
        previous_release=tmp_path / "e",
        manifest_sha256="a" * 64,
        previous_manifest_sha256="b" * 64,
        upgrade_only=True,
        worker_restart_only=False,
    )
    rehearsal = FRehearsal(options, tmp_path)
    assert not rehearsal.result["passed"] and not rehearsal.result["schema_changing_upgrade_tested"]
    assert "D/E to F" in rehearsal.result["scope"]
    assert F_MIGRATIONS == {("community", "0003_problem_feedback")}
    assert "--initial-content" not in rehearsal.apply_args()
