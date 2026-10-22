import hashlib
import json
from pathlib import Path

import pytest

from judge.backends import configured_sandbox
from judge.sandbox import SandboxUnavailable
from judge.wasi import WasiSandbox
from judge.wasi_policy import BUNDLE_FILES, PYTHON_VERSION, TARGET, WASMTIME_VERSION


@pytest.fixture
def artifact(tmp_path):
    # Opaque fixtures exercise artifact rejection, never execute these bytes.
    root = tmp_path / "artifact"
    (root / "stdlib").mkdir(parents=True)
    files = {}
    for name in BUNDLE_FILES:
        (root / name).write_bytes(name.encode())
        files[name] = hashlib.sha256(name.encode()).hexdigest()
    data = {
        "format": 2,
        "python": PYTHON_VERSION,
        "wasmtime": WASMTIME_VERSION,
        "architecture": "x86_64",
        "target": TARGET,
        "files": files,
        "engine_policy": hashlib.sha256(Path("judge/wasi_policy.py").read_bytes()).hexdigest(),
    }
    manifest = json.dumps(data).encode()
    (root / "manifest.json").write_bytes(manifest)
    return root, hashlib.sha256(manifest).hexdigest()


@pytest.mark.parametrize(
    "change",
    ["code", "library", "manifest", "symlink", "extra", "writable", "target", "format", "notice"],
)
def test_changed_artifact_never_reaches_execution(artifact, monkeypatch, change):
    root, digest = artifact
    sandbox = WasiSandbox(root, digest)
    sandbox.verify_bundle()
    if change == "code":
        (root / "python.cwasm").write_bytes(b"modified native code")
    elif change == "library":
        (root / "stdlib/python314.zip").write_bytes(b"modified library")
    elif change == "manifest":
        (root / "manifest.json").write_text("{}")
    elif change == "symlink":
        (root / "stdlib/host").symlink_to("/etc")
    elif change == "extra":
        (root / "stdlib/credentials").write_text("must not become visible")
    elif change in {"target", "format"}:
        data = json.loads((root / "manifest.json").read_text())
        data[change] = "aarch64-unknown-linux-gnu" if change == "target" else 1
        content = json.dumps(data).encode()
        (root / "manifest.json").write_bytes(content)
        sandbox.digest = hashlib.sha256(content).hexdigest()
    elif change == "notice":
        (root / "THIRD_PARTY_NOTICES.txt").unlink()
    else:
        root.chmod(0o777)

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid artifacts must be rejected before launching a driver")

    monkeypatch.setattr("judge.wasi.subprocess.Popen", forbidden)
    with pytest.raises(SandboxUnavailable):
        sandbox.execute("def solve(): return 42", "solve", [])


def test_unknown_backend_never_falls_back(settings):
    settings.JUDGE_BACKEND = "host-python"
    with pytest.raises(SandboxUnavailable, match="no fallback"):
        configured_sandbox()


def test_receipt_cannot_cross_backend_or_expire(settings, tmp_path, monkeypatch):
    from core.management.commands import sandbox_check

    identity = {"policy": "wasi-python-function-v1", "bundle": "a" * 64}
    monkeypatch.setattr(sandbox_check.time, "time", lambda: 100000)
    path = tmp_path / "receipt.json"
    settings.JUDGE_RECEIPT = str(path)
    receipt = {
        "created": 100000,
        "identity": identity,
        "suite": sandbox_check.suite_digest(identity),
        "passed": [p[0] for p in sandbox_check.probes_for(identity)],
    }
    path.write_text(json.dumps(receipt))
    sandbox_check.check_receipt(identity)
    with pytest.raises(SandboxUnavailable):
        sandbox_check.check_receipt({**identity, "bundle": "b" * 64})
    monkeypatch.setattr(sandbox_check.time, "time", lambda: 186400)
    with pytest.raises(SandboxUnavailable):
        sandbox_check.check_receipt(identity)
