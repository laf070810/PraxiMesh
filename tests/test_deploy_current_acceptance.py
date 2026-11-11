"""F upgrade preflight and preservation checks; old application execution is separate."""

import copy
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.deploy_acceptance import validate_upgrade_manifests
from scripts.deploy_current_acceptance import (
    ADDED_MIGRATIONS,
    CurrentRehearsal,
    f_migrations,
    original_export_fields,
)
from tests.test_deploy_database_postgres import owned_pg as owned_pg
from tests.test_deploy_schema_upgrade_postgres import upgraded_f as upgraded_f

ROOT = Path(__file__).resolve().parents[1]


def manifests():
    previous = {
        "format": 1,
        "source_files": f_migrations(),
        "images": {"web": "sha256:" + "a" * 64, "worker": "sha256:" + "b" * 64},
    }
    target = copy.deepcopy(previous)
    target["images"] = {"web": "sha256:" + "c" * 64, "worker": "sha256:" + "d" * 64}
    for app, name in ADDED_MIGRATIONS:
        path = f"{app}/migrations/{name}.py"
        target["source_files"][path] = hashlib.sha256((ROOT / path).read_bytes()).hexdigest()
    return previous, target


def test_f_to_current_release_pair_has_its_own_upgrade_branch():
    assert validate_upgrade_manifests(*manifests()) == "post-f"


@pytest.mark.parametrize(
    "change",
    [
        "missing-old",
        "edited-old",
        "both-edited-history",
        "missing-new",
        "unknown-new",
        "already-upgraded",
        "same-web",
        "same-worker",
        "missing-worker",
        "gpu-format",
        "gpu-metadata",
        "boolean-format",
    ],
)
def test_f_upgrade_rejects_unreviewed_schema_or_image_pairs(change):
    previous, target = manifests()
    history = "core/migrations/0001_initial.py"
    added = "core/migrations/0004_gpu_resources.py"
    if change == "missing-old":
        previous["source_files"].pop(history)
    elif change == "edited-old":
        target["source_files"][history] = "f" * 64
    elif change == "both-edited-history":
        previous["source_files"][history] = target["source_files"][history] = "f" * 64
    elif change == "missing-new":
        target["source_files"].pop(added)
    elif change == "unknown-new":
        target["source_files"]["core/migrations/0006_unknown.py"] = "f" * 64
    elif change == "already-upgraded":
        previous["source_files"][added] = target["source_files"][added]
    elif change.startswith("same-"):
        role = change.removeprefix("same-")
        target["images"][role] = previous["images"][role]
    elif change == "missing-worker":
        target["images"].pop("worker")
    elif change == "gpu-format":
        target["format"] = 2
    elif change == "gpu-metadata":
        target["gpu"] = {}
    else:
        target["format"] = True
    with pytest.raises(ValueError):
        validate_upgrade_manifests(previous, target)


def current_export():
    return {
        "username": "private-user",
        "submissions": [{"source": "Private historical source", "result": {"value": 7}}],
        "gpu_budget": {
            "policy": "gpu-slot-seconds-v1",
            "reserved": 0,
            "consumed": 0,
            "limit": 200,
            "remaining": 200,
        },
        "showcase": {
            "shared_results": [
                {"id": "original", "is_public": True, "revision": 3, "metric_kind": "sequential-v1"}
            ]
        },
    }


def test_export_projection_keeps_all_old_values_and_does_not_mutate_the_response():
    exported = current_export()
    original = copy.deepcopy(exported)
    expected = copy.deepcopy(exported)
    del expected["gpu_budget"]
    del expected["showcase"]["shared_results"][0]["metric_kind"]
    assert original_export_fields(exported) == expected
    assert exported == original


@pytest.mark.parametrize(
    "change",
    [
        "metric",
        "missing-metric",
        "budget-policy",
        "reserved",
        "consumed",
        "remaining",
        "negative-limit",
        "boolean-budget",
    ],
)
def test_export_projection_cannot_hide_a_new_public_scope_or_gpu_usage(change):
    exported = current_export()
    if change == "metric":
        exported["showcase"]["shared_results"][0]["metric_kind"] = "gpu-batch-ms-v1"
    elif change == "missing-metric":
        exported["showcase"]["shared_results"][0].pop("metric_kind")
    elif change == "budget-policy":
        exported["gpu_budget"]["policy"] = "cpu-invocations-v1"
    elif change == "boolean-budget":
        exported["gpu_budget"]["consumed"] = False
    elif change == "negative-limit":
        exported["gpu_budget"]["limit"] = exported["gpu_budget"]["remaining"] = -1
    else:
        exported["gpu_budget"][change] = 1
    with pytest.raises(ValueError):
        original_export_fields(exported)


def rehearsal(tmp_path):
    options = SimpleNamespace(
        release=tmp_path / "current",
        previous_release=tmp_path / "f",
        manifest_sha256="a" * 64,
        previous_manifest_sha256="b" * 64,
        upgrade_only=True,
        worker_restart_only=False,
        recovery_only=False,
    )
    return CurrentRehearsal(options, tmp_path)


def test_new_upgrade_report_starts_unverified_and_keeps_initial_import_separate(tmp_path):
    item = rehearsal(tmp_path)
    assert not item.result["passed"] and not item.result["schema_changing_upgrade_tested"]
    assert "no GPU access" in item.result["scope"]
    assert "--initial-content" in item.apply_args(initial=True, previous=True)
    assert "--initial-content" not in item.apply_args()


@pytest.mark.django_db(transaction=True)
def test_current_rehearsal_projects_real_original_f_rows_and_checks_new_grants(
    upgraded_f, tmp_path
):
    state = upgraded_f
    item = rehearsal(tmp_path)
    item.connection = lambda generation: state.item.database.connection(generation["database"])
    item.runtime_connection = lambda generation, role: state.item.runtime(
        generation["database"], role
    )
    previous = {"database": state.item.name}
    target = {"database": state.target}
    before = item.snapshot(previous, ["schema-fixture"])
    after = item.snapshot(target, ["schema-fixture"])
    assert after["tables"] == before["tables"] and after["user_ids"] == before["user_ids"]
    assert item.new_resource_permissions(target)["web"]["SELECT"]
    with state.item.database.connection(state.target) as connection:
        connection.execute("UPDATE showcase_sharedresult SET metric_kind='gpu-batch-ms-v1'")
    with pytest.raises(AssertionError):
        item.new_resource_permissions(target)
