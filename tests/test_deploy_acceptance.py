"""Rehearsal preflight boundaries only; these tests do not certify a live upgrade."""

import copy

import pytest

from scripts.deploy_acceptance import Rehearsal, parse_options, validate_upgrade_manifests


def arguments(*extra):
    return [
        "--release",
        "/tmp/b1-release",
        "--manifest-sha256",
        "a" * 64,
        "--caddy",
        "/tmp/caddy",
        *extra,
    ]


def upgrade_arguments(*extra):
    return arguments(
        "--upgrade-only",
        "--previous-release",
        "/tmp/phase-a-release",
        "--previous-manifest-sha256",
        "b" * 64,
        *extra,
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--upgrade-only"],
        ["--previous-release", "/tmp/previous"],
        ["--previous-manifest-sha256", "b" * 64],
        ["--previous-release", "/tmp/previous", "--previous-manifest-sha256", "b" * 64],
    ],
)
def test_upgrade_requires_explicit_mode_and_paired_previous_identity(extra):
    with pytest.raises(SystemExit) as error:
        parse_options(arguments(*extra))
    assert error.value.code == 2


@pytest.mark.parametrize(
    "extra",
    [
        ["--worker-restart-only"],
        ["--migration-only"],
        ["--failed-release", "/tmp/failed", "--failed-manifest-sha256", "c" * 64],
        ["--manifest-sha256", "b" * 64],
        ["--previous-release", "/tmp/b1-release"],
    ],
)
def test_upgrade_rejects_conflicting_modes_and_same_release(extra):
    with pytest.raises(SystemExit) as error:
        parse_options(upgrade_arguments(*extra))
    assert error.value.code == 2


@pytest.mark.parametrize(
    "extra",
    [
        [],
        ["--worker-restart-only"],
        ["--failed-release", "/tmp/failed", "--failed-manifest-sha256", "c" * 64],
        [
            "--migration-only",
            "--failed-release",
            "/tmp/failed",
            "--failed-manifest-sha256",
            "c" * 64,
        ],
    ],
)
def test_existing_modes_remain_available(extra):
    options = parse_options(arguments(*extra))
    assert not options.upgrade_only and options.previous_release is None


def test_upgrade_command_cannot_republish_catalogue_on_new_generation(tmp_path):
    options = parse_options(upgrade_arguments())
    rehearsal = Rehearsal(options, tmp_path)
    initial = rehearsal.apply_args(initial=True, previous=True)
    upgraded = rehearsal.apply_args()
    assert initial == [
        "apply",
        "--release",
        "/tmp/phase-a-release",
        "--sha256",
        "b" * 64,
        "--initial-content",
    ]
    assert upgraded == ["apply", "--release", "/tmp/b1-release", "--sha256", "a" * 64]
    with pytest.raises(ValueError, match="initial generation"):
        rehearsal.apply_args(previous=True)
    with pytest.raises(ValueError, match="preserve the previous catalogue"):
        rehearsal.apply_args(initial=True)
    assert not rehearsal.result["passed"]
    assert not rehearsal.result["schema_changing_upgrade_tested"]
    assert not rehearsal.result["feature_role_permissions_tested"]
    assert rehearsal.result["previous_release_sha256"] == "b" * 64
    assert rehearsal.result["expanded_catalogue_published"] is False


def manifests():
    previous = {
        "source_files": {"core/migrations/0002_admission_and_policy.py": "a" * 64},
        "images": {"web": "sha256:" + "a" * 64},
    }
    target = copy.deepcopy(previous)
    target["source_files"].update(
        {
            "learning/migrations/0001_initial.py": "b" * 64,
            "community/migrations/0001_initial.py": "c" * 64,
        }
    )
    target["images"]["web"] = "sha256:" + "b" * 64
    return previous, target


def test_schema_preflight_accepts_expected_source_contract_only():
    previous, target = manifests()
    validate_upgrade_manifests(previous, target)


@pytest.mark.parametrize(
    "change",
    [
        "already-learning",
        "already-community",
        "missing-learning",
        "missing-community",
        "same-image",
    ],
)
def test_schema_preflight_rejects_a_false_upgrade_contract(change):
    previous, target = manifests()
    if change.startswith("already-"):
        app = change.removeprefix("already-")
        previous["source_files"][f"{app}/migrations/0001_initial.py"] = "f" * 64
    elif change.startswith("missing-"):
        app = change.removeprefix("missing-")
        target["source_files"].pop(f"{app}/migrations/0001_initial.py")
    else:
        target["images"]["web"] = previous["images"]["web"]
    with pytest.raises(ValueError):
        validate_upgrade_manifests(previous, target)


def b2_manifests():
    from scripts.deploy_acceptance import B2_MIGRATIONS

    _, previous = manifests()
    target = copy.deepcopy(previous)
    target["images"]["web"] = "sha256:" + "c" * 64
    target["source_files"].update(
        {f"{app}/migrations/{name}.py": "d" * 64 for app, name in B2_MIGRATIONS}
    )
    return previous, target


def test_b1_to_b2_upgrade_selects_extended_data_and_role_rehearsal():
    previous, target = b2_manifests()
    assert validate_upgrade_manifests(previous, target) == "b2"


@pytest.mark.parametrize(
    "change", ["partial-previous", "missing-target", "edited-history", "same-image"]
)
def test_b2_upgrade_contract_rejects_partial_schema_or_rewritten_history(change):
    previous, target = b2_manifests()
    path = "localization/migrations/0002_backfill_snapshots.py"
    if change == "partial-previous":
        previous["source_files"][path] = target["source_files"][path]
    elif change == "missing-target":
        target["source_files"].pop(path)
    elif change == "edited-history":
        target["source_files"]["community/migrations/0001_initial.py"] = "f" * 64
    else:
        target["images"]["web"] = previous["images"]["web"]
    with pytest.raises(ValueError):
        validate_upgrade_manifests(previous, target)
