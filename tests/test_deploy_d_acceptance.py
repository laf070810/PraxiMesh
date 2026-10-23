"""Preflight boundary tests; actual C-to-D migration has its own disposable rehearsal."""

import copy
from types import SimpleNamespace

import pytest

from scripts.deploy_acceptance import (
    B2_MIGRATIONS,
    C_MIGRATIONS,
    D_MIGRATIONS,
    UPGRADE_MIGRATIONS,
    validate_upgrade_manifests,
)
from scripts.deploy_d_acceptance import DRehearsal


def manifests():
    previous = {
        "source_files": {
            f"{app}/migrations/{name}.py": "a" * 64
            for app, name in UPGRADE_MIGRATIONS | B2_MIGRATIONS | C_MIGRATIONS
        },
        "images": {"web": "sha256:" + "a" * 64},
    }
    target = copy.deepcopy(previous)
    target["images"]["web"] = "sha256:" + "b" * 64
    target["source_files"]["challenges/migrations/0001_initial.py"] = "c" * 64
    return previous, target


def test_c_to_d_contract_requires_complete_new_schema():
    assert validate_upgrade_manifests(*manifests()) == "d"


@pytest.mark.parametrize(
    "change", ["already-d", "missing-target", "history-edited", "history-deleted", "same-image"]
)
def test_d_preflight_rejects_incompatible_releases(change):
    previous, target = manifests()
    path = "challenges/migrations/0001_initial.py"
    if change == "already-d":
        previous["source_files"][path] = target["source_files"][path]
    elif change == "missing-target":
        target["source_files"].pop(path)
    elif change == "history-edited":
        target["source_files"]["core/migrations/0003_cpu_reservations.py"] = "d" * 64
    elif change == "history-deleted":
        target["source_files"].pop("organizations/migrations/0002_publication_lock.py")
    else:
        target["images"]["web"] = previous["images"]["web"]
    with pytest.raises(ValueError):
        validate_upgrade_manifests(previous, target)


def test_d_rehearsal_does_not_claim_success_and_preserves_catalogue(tmp_path):
    options = SimpleNamespace(
        release=tmp_path / "d",
        previous_release=tmp_path / "c",
        manifest_sha256="a" * 64,
        previous_manifest_sha256="b" * 64,
        upgrade_only=True,
        worker_restart_only=False,
    )
    rehearsal = DRehearsal(options, tmp_path)
    assert not rehearsal.result["passed"] and not rehearsal.result["schema_changing_upgrade_tested"]
    assert "C to D" in rehearsal.result["scope"] and D_MIGRATIONS == {
        ("challenges", "0001_initial")
    }
    assert "--initial-content" not in rehearsal.apply_args()
