"""Rehearsal preflight boundaries only; these tests do not certify a live upgrade."""

import copy
import json
import os
import stat

import pytest

from scripts import deploy_acceptance
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


@pytest.mark.parametrize("enhanced", [False, True])
def test_browser_contract_follows_verified_current_generation_not_inherited_override(
    tmp_path,
    monkeypatch,
    enhanced,
):
    options = parse_options(arguments("--artifacts", str(tmp_path / "artifacts")))
    options.artifacts.mkdir()
    rehearsal = Rehearsal(options, tmp_path)
    rehearsal.base, rehearsal.pin = "https://127.0.0.1:8443", "unused-test-pin"
    files = {"static/editor/manifest.json": "a" * 64} if enhanced else {}
    rehearsal.journal = lambda: {"current": {"manifest": {"source_files": files}}}
    monkeypatch.setenv("BROWSER_EDITOR", "textarea" if enhanced else "codemirror")
    captured = {}

    def capture(*args, **kwargs):
        captured.update(kwargs["env"])
        raise InterruptedError("No browser or network in this boundary check")

    monkeypatch.setattr(deploy_acceptance.subprocess, "run", capture)
    with pytest.raises(InterruptedError):
        rehearsal.browser()
    assert captured["BROWSER_EDITOR"] == ("codemirror" if enhanced else "textarea")
    assert captured["BROWSER_ARTIFACTS"] == str(options.artifacts / "browser")


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
        ["--recovery-only"],
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


@pytest.mark.parametrize(
    "extra",
    [
        ["--worker-restart-only"],
        ["--migration-only"],
        ["--failed-release", "/tmp/failed", "--failed-manifest-sha256", "c" * 64],
    ],
)
def test_targeted_recovery_excludes_other_modes(extra):
    with pytest.raises(SystemExit):
        parse_options(arguments("--recovery-only", *extra))


def test_targeted_recovery_report_does_not_claim_injected_faults(tmp_path):
    options = parse_options(arguments("--recovery-only"))
    report = Rehearsal(options, tmp_path).result
    assert report["recovery_only"] and not report["worker_restart_only"]
    assert "without injected process faults" in report["scope"]
    assert not report["schema_changing_upgrade_tested"]


@pytest.mark.parametrize(
    "invalid", [None, "name", "symlink", "hardlink", "permissions", "oversized"]
)
def test_rehearsal_retains_only_recorded_private_diagnostic(tmp_path, invalid):
    state, artifacts = tmp_path / "state", tmp_path / "artifacts"
    state.mkdir(mode=0o700)
    artifacts.mkdir()
    filename = "control-failure-" + "a" * 32 + ".json"
    source = state / filename
    source.write_text(json.dumps({"command": "container-stop", "stderr": "safe test diagnostic"}))
    source.chmod(0o600)
    if invalid == "name":
        filename = "../" + filename
    elif invalid == "symlink":
        source.rename(state / "actual.json")
        source.symlink_to(state / "actual.json")
    elif invalid == "hardlink":
        os.link(source, state / "other.json")
    elif invalid == "permissions":
        source.chmod(0o644)
    elif invalid == "oversized":
        source.write_bytes(b"x" * (deploy_acceptance.CONTROL_DIAGNOSTIC_MAX_BYTES + 1))
    if invalid:
        with pytest.raises((ValueError, OSError)):
            deploy_acceptance.retain_failure_diagnostic(state, artifacts, filename)
        assert not list(artifacts.iterdir())
    else:
        retained = deploy_acceptance.retain_failure_diagnostic(state, artifacts, filename)
        assert retained == filename
        assert (artifacts / filename).read_bytes() == source.read_bytes()
        assert stat.S_IMODE((artifacts / filename).stat().st_mode) == 0o600


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
