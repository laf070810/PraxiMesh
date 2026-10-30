"""Private file/identity tests with synthetic documents, never GPU acceptance."""

import copy
import json
import os
import stat
import time
import uuid
from types import SimpleNamespace

import pytest

from judge import gpu_receipt
from judge.gpu_adapter import GPUExecutionAdapter
from judge.gpu_device_lock import DeviceLock, DeviceLockError
from judge.gpu_execution_contract import canonical_digest, environment_identity
from judge.gpu_receipt import ReceiptStore, binding, check_receipt, validate_receipt
from judge.sandbox import SandboxUnavailable
from tests.gpu_evidence_fixtures import environment_fixture
from tests.test_gpu_adapter import TARGET


def adapter_for(directory):
    adapter = GPUExecutionAdapter.__new__(GPUExecutionAdapter)
    adapter.target = copy.deepcopy(TARGET)
    adapter.identity = environment_fixture()["artifacts"]
    adapter.environment = environment_identity(adapter.target, adapter.identity)
    adapter.fingerprint = canonical_digest(adapter.environment)
    adapter.directory = directory / "jobs"
    return adapter


@pytest.fixture
def receipt(tmp_path):
    adapter = adapter_for(tmp_path)
    with DeviceLock(tmp_path / "locks", TARGET["uuid"]) as guard:
        directory = tmp_path / "receipts"
        worker = "receipt-unit-test"
        value = {
            **binding(guard, worker, adapter),
            "created_wall": time.time(),
            "created_monotonic": time.monotonic(),
            "evidence_digest": canonical_digest({"scope": "synthetic serialization fixture only"}),
        }

        def publish(document=None):
            with ReceiptStore(directory, guard, create=True) as store:
                store._write(json.dumps(value if document is None else document).encode())

        yield SimpleNamespace(
            guard=guard,
            directory=directory,
            worker=worker,
            adapter=adapter,
            value=value,
            publish=publish,
            path=directory / (guard.device_uuid + ".json"),
        )


def test_private_atomic_document_roundtrip(receipt):
    receipt.publish()
    assert stat.S_IMODE(receipt.directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.path.stat().st_mode) == 0o600
    assert receipt.path.stat().st_nlink == 1
    assert list(receipt.directory.iterdir()) == [receipt.path]
    assert (
        check_receipt(receipt.directory, receipt.guard, receipt.worker, receipt.adapter)
        == receipt.value
    )


@pytest.mark.parametrize(
    "field",
    [
        "schema",
        "checks",
        "suite",
        "owner_token",
        "worker",
        "host",
        "environment",
        "evidence_digest",
    ],
)
def test_receipt_cannot_change_its_owner_host_or_complete_suite(receipt, field):
    value = copy.deepcopy(receipt.value)
    if field == "schema":
        value[field] = True
    elif field == "checks":
        value[field].pop()
    elif field == "host":
        value[field]["boot_id"] = str(uuid.uuid4())
    elif field == "environment":
        value[field]["target"]["index"] = False
    elif field == "evidence_digest":
        value[field] = "missing"
    else:
        value[field] = "changed"
    with pytest.raises(SandboxUnavailable):
        validate_receipt(value, receipt.guard, receipt.worker, receipt.adapter)


@pytest.mark.parametrize("field", ["created_wall", "created_monotonic"])
@pytest.mark.parametrize("defect", ["expired", "future", "near-expiry", "nan", "bool"])
def test_both_clocks_and_a_complete_task_margin_are_required(receipt, field, defect):
    value = copy.deepcopy(receipt.value)
    if defect == "expired":
        value[field] -= gpu_receipt.TTL_SECONDS + 1
    elif defect == "future":
        value[field] += 10
    elif defect == "near-expiry":
        value[field] -= gpu_receipt.TTL_SECONDS - 100
    elif defect == "nan":
        value[field] = float("nan")
    else:
        value[field] = True
    with pytest.raises(SandboxUnavailable, match="expired|clock"):
        validate_receipt(value, receipt.guard, receipt.worker, receipt.adapter)


def test_reacquiring_the_same_host_lock_cannot_reuse_a_receipt(receipt):
    receipt.publish()
    receipt.guard.close()
    with pytest.raises(DeviceLockError):
        check_receipt(receipt.directory, receipt.guard, receipt.worker, receipt.adapter)
    with DeviceLock(receipt.guard.directory, receipt.guard.device_uuid) as replacement:
        with pytest.raises(SandboxUnavailable, match="current owner"):
            check_receipt(receipt.directory, replacement, receipt.worker, receipt.adapter)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "public"])
def test_unsafe_receipt_files_are_neither_read_nor_deleted(receipt, kind):
    receipt.directory.mkdir(mode=0o700)
    other = receipt.directory / "unrelated"
    if kind in {"symlink", "hardlink"}:
        other.write_bytes(b"keep")
        other.chmod(0o600)
        if kind == "symlink":
            receipt.path.symlink_to(other)
        else:
            os.link(other, receipt.path)
    elif kind == "fifo":
        os.mkfifo(receipt.path, 0o600)
    else:
        receipt.path.write_bytes(b"keep")
        receipt.path.chmod(0o644)
    with ReceiptStore(receipt.directory, receipt.guard) as store:
        for action in (store.read, store.invalidate):
            with pytest.raises((SandboxUnavailable, OSError)):
                action()
    assert os.path.lexists(receipt.path)
    if other.exists():
        assert other.read_bytes() == b"keep"


def test_a_replaced_directory_cannot_redirect_open_store(receipt):
    receipt.publish()
    with ReceiptStore(receipt.directory, receipt.guard) as store:
        receipt.directory.rename(receipt.directory.with_name("original-receipts"))
        receipt.directory.mkdir(mode=0o700)
        with pytest.raises(SandboxUnavailable, match="unchanged"):
            store.read()


def test_receipt_replacement_during_read_is_detected(receipt, monkeypatch):
    receipt.publish()
    original_read = os.read
    changed = []

    def replace_after_read(fd, size):
        value = original_read(fd, size)
        if not changed:
            replacement = receipt.directory / "replacement"
            replacement.write_bytes(b"replacement must remain")
            replacement.chmod(0o600)
            replacement.replace(receipt.path)
            changed.append(True)
        return value

    with ReceiptStore(receipt.directory, receipt.guard) as store:
        monkeypatch.setattr(gpu_receipt.os, "read", replace_after_read)
        with pytest.raises(SandboxUnavailable, match="changed during"):
            store.read()
    assert receipt.path.read_bytes() == b"replacement must remain"


def test_publication_does_not_overwrite_an_unexpected_file(receipt):
    receipt.publish()
    original = receipt.path.read_bytes()
    with ReceiptStore(receipt.directory, receipt.guard) as store:
        with pytest.raises(FileExistsError):
            store._write(b"another document")
    assert receipt.path.read_bytes() == original
    assert list(receipt.directory.iterdir()) == [receipt.path]


@pytest.mark.parametrize(
    "raw", [b"[]", b'{"x":1,"x":2}', b'{"x":NaN}', b"x" * (gpu_receipt.MAX_BYTES + 1)]
)
def test_oversized_or_ambiguous_json_is_rejected(receipt, raw):
    receipt.directory.mkdir(mode=0o700)
    receipt.path.write_bytes(raw)
    receipt.path.chmod(0o600)
    with ReceiptStore(receipt.directory, receipt.guard) as store:
        with pytest.raises(SandboxUnavailable):
            store.read()
