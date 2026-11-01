"""Durable two-file GPU configuration publication under coordinator/node/DB locks.

Internal preparation only. No container mutation, device access, readiness, or
exposure decision; the coordinator still refuses GPU release activation.
"""

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from judge.container_deployment_config import GENERATION, TRANSITION, canonical, digest, inode
from judge.container_journal import MAX_RECORD, private, validate_record
from judge.container_service import IMAGE_ID
from judge.gpu_session import decode_frame
from scripts.deploy_gpu import read_private
from scripts.deploy_web import fingerprint
from scripts.release_gpu import WORKER_CONFIGURATION

FILES = ("worker.json", GENERATION)
CONTEXT = {"id", "database", "gpu_worker", "release_sha256", "manifest"}
HEX = re.compile(r"[a-f0-9]{64}")
NONCE = re.compile(r"[a-f0-9]{32}")
GENERATION_ID = re.compile(r"[a-f0-9]{12}")


def frozen(value):
    return json.loads(canonical(value))


def context(generation):
    return frozen({key: generation[key] for key in CONTEXT})


def raw_digest(raw):
    return None if raw is None else hashlib.sha256(raw).hexdigest()


def require_idle_journal(node):
    """Only original, fully removed archives may coexist with publication."""
    expected = {"installation.json", "publication.lock"}
    for name in set(os.listdir(node.journal_fd)) - expected:
        match = re.fullmatch(
            re.escape(node.config["device"]) + r"\.([a-f0-9]{32})\.done\.json", name
        )
        if match is None:
            raise ValueError("GPU configuration requires a fully recovered original task journal")
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=node.journal_fd)
        with os.fdopen(fd, "rb") as stream:
            private(os.fstat(stream.fileno()))
            raw = stream.read(MAX_RECORD + 1)
        if len(raw) > MAX_RECORD:
            raise ValueError("GPU task archive exceeded its bound")
        record = validate_record(
            decode_frame(raw), node.snapshot["installation"], node.config["device"]
        )
        if record["phase"] != "removed" or record["arguments"][2] != match[1]:
            raise ValueError("GPU task archive is not the original completed removal")
    read_private(node.journal_fd, "publication.lock")


def require_stopped_workers(host, node, binding):
    """Observe all containers; never adopt an unrecorded ID or mutate an object."""
    records = host.state.get("gpu_workers", {})
    if type(records) is not dict:
        raise ValueError("Invalid GPU worker runtime journal")
    known, names = {}, set()
    for generation_id, value in records.items():
        if (
            type(generation_id) is not str
            or not GENERATION_ID.fullmatch(generation_id)
            or type(value) is not dict
            or set(value) != {"id", "name", "image", "binding_sha256", "runtime_sha256"}
            or type(value["id"]) is not str
            or not HEX.fullmatch(value["id"])
            or type(value["image"]) is not str
            or not IMAGE_ID.fullmatch(value["image"])
            or value["binding_sha256"] != digest(binding)
            or type(value["runtime_sha256"]) is not str
            or not HEX.fullmatch(value["runtime_sha256"])
            or value["name"] != f"px-{host.config['instance']}-{generation_id}-gpu-worker"
            or value["id"] in known
        ):
            raise ValueError("GPU worker runtime has no complete original identity")
        known[value["id"]] = (generation_id, value)
        names.add("/" + value["name"])
    operation = host.state.get("operation") or {}
    for generation in (
        host.state.get("current"),
        operation.get("previous"),
        operation.get("candidate"),
    ):
        if generation and "gpu_worker" in generation:
            names.add("/" + generation["gpu_worker"])
    identifiers = (
        host.run("container", "ls", "--all", "--quiet", "--no-trunc", timeout=15).decode().split()
    )
    if (
        len(identifiers) > 512
        or len(set(identifiers)) != len(identifiers)
        or any(not HEX.fullmatch(v) for v in identifiers)
    ):
        raise ValueError("Cannot establish a bounded GPU worker inventory")
    for offset in range(0, len(identifiers), 16):
        batch = identifiers[offset : offset + 16]
        values = json.loads(host.run("container", "inspect", *batch, timeout=15))
        if (
            type(values) is not list
            or {v["Id"] for v in values} != set(batch)
            or len(values) != len(batch)
        ):
            raise ValueError("Container inventory changed during GPU configuration checks")
        for info in values:
            labels = info["Config"].get("Labels") or {}
            touches_node = any(
                type(mount.get("Source")) is str
                and mount["Source"].startswith("/")
                and (
                    Path(mount["Source"]) == node.root
                    or node.root in Path(mount["Source"]).parents
                    or Path(mount["Source"]) in node.root.parents
                )
                for mount in info.get("Mounts", [])
            )
            related = (
                info["Id"] in known
                or info["Name"] in names
                or touches_node
                or (
                    labels.get("org.praximesh.site") == host.state["site_id"]
                    and labels.get("org.praximesh.role") == "gpu_worker"
                )
            )
            if not related:
                continue
            if info["Id"] not in known:
                raise ValueError(
                    "An unrecorded container refers to the GPU worker or original state"
                )
            generation_id, expected = known[info["Id"]]
            state = info["State"]
            if (
                info["Name"] != "/" + expected["name"]
                or info["Image"] != expected["image"]
                or fingerprint(info) != expected["runtime_sha256"]
                or labels.get("org.praximesh.site") != host.state["site_id"]
                or labels.get("org.praximesh.generation") != generation_id
                or labels.get("org.praximesh.role") != "gpu_worker"
                or labels.get("org.praximesh.gpu-binding") != digest(binding)
                or state.get("Running") is not False
                or state.get("Paused") is not False
                or state.get("Restarting") is not False
                or state.get("Pid") != 0
                or state.get("Status") not in {"created", "exited"}
                or info["HostConfig"].get("RestartPolicy") != {"Name": "no", "MaximumRetryCount": 0}
            ):
                raise ValueError(
                    "Original GPU worker must be unchanged, stopped and unable to restart"
                )


class Publication:
    def __init__(self, host, node, binding):
        self.host, self.node, self.binding = host, node, binding
        self.database_check = None

    def check(self):
        self.host.require_locked()
        self.node.require_owner(self.binding)
        if self.database_check is not None:
            self.database_check()
        if self.host.proxy.probe()["port"] is not None:
            raise ValueError("GPU configuration publication requires the actual maintenance route")
        require_stopped_workers(self.host, self.node, self.binding)
        require_idle_journal(self.node)

    def render(self, snapshot):
        if snapshot is None:
            return {name: None for name in FILES}
        if (
            type(snapshot) is not dict
            or set(snapshot) != {"generation", "enabled", "metadata"}
            or set(snapshot["generation"]) != CONTEXT
        ):
            raise ValueError("Unrecognized GPU configuration snapshot")
        generation = snapshot["generation"]
        if generation["manifest"]["gpu"].get("worker_configuration") != WORKER_CONFIGURATION:
            raise ValueError(
                "Frozen GPU worker does not support interrupted configuration publication"
            )
        config, metadata = self.node.generation_configuration(
            self.binding,
            generation,
            self.host.database.worker_configuration(generation["database"]),
            enabled=snapshot["enabled"],
        )
        if canonical(metadata) != canonical(snapshot["metadata"]):
            raise ValueError("GPU configuration snapshot or original credentials changed")
        return dict(
            zip(FILES, (canonical(config) + b"\n", canonical(metadata) + b"\n"), strict=True)
        )

    def snapshot(self, generation, enabled):
        generation = context(generation)
        config, metadata = self.node.generation_configuration(
            self.binding,
            generation,
            self.host.database.worker_configuration(generation["database"]),
            enabled=enabled,
        )
        value = {"generation": generation, "enabled": config["enabled"], "metadata": metadata}
        self.render(value)
        return value

    def read_pair(self):
        return {name: read_private(self.node.fd, name, optional=True) for name in FILES}

    def temporary(self, intent, name):
        return ".deployment-config-" + intent["id"] + "-" + name + ".tmp"

    def validate_temporaries(self, intent, target):
        allowed = set() if intent is None else {self.temporary(intent, name) for name in FILES}
        present = {
            name for name in os.listdir(self.node.fd) if name.startswith(".deployment-config-")
        }
        if present - allowed:
            raise ValueError("Unknown GPU configuration temporary must be preserved")
        for name in FILES:
            if intent is not None and self.temporary(intent, name) in present:
                raw = read_private(self.node.fd, self.temporary(intent, name))
                if not target[name].startswith(raw):
                    raise ValueError("GPU configuration temporary differs from the original intent")

    def marker(self, intent):
        return (
            canonical(
                {
                    "schema": 1,
                    "id": intent["id"],
                    "binding_sha256": digest(self.binding),
                    "intent_sha256": digest(intent),
                }
            )
            + b"\n"
        )

    def establish_marker(self, expected):
        self.check()
        current = read_private(self.node.fd, TRANSITION, optional=True)
        if current is not None and not expected.startswith(current):
            raise ValueError("GPU configuration transition marker is not this intent")
        # Presence alone prevents readers proceeding, even if killed mid-write.
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        if current is None:
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(TRANSITION, flags, 0o600, dir_fd=self.node.fd)
        try:
            info = os.fstat(fd)
            private(info)
            named = os.stat(TRANSITION, dir_fd=self.node.fd, follow_symlinks=False)
            if inode(info) != inode(named) or os.pread(fd, len(expected) + 1, 0) != (
                current or b""
            ):
                raise ValueError("GPU transition marker changed")
            if current != expected:
                os.ftruncate(fd, 0)
                with os.fdopen(fd, "wb", closefd=False) as stream:
                    stream.write(expected)
                    stream.flush()
            # A complete read after a killed writer does not prove its fsync ran.
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.node.fd)

    def replace(self, intent, name, expected, previous):
        self.check()
        if read_private(self.node.fd, TRANSITION) != self.marker(intent):
            raise ValueError("GPU configuration transition marker changed")
        current = read_private(self.node.fd, name, optional=True)
        if current == expected:
            return
        if current != previous:
            raise ValueError("GPU configuration file differs from the original transition")
        temporary = self.temporary(intent, name)
        leftover = read_private(self.node.fd, temporary, optional=True)
        if leftover is not None:
            if not expected.startswith(leftover):
                raise ValueError("GPU configuration temporary differs from the original intent")
            os.unlink(temporary, dir_fd=self.node.fd)
            os.fsync(self.node.fd)
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.node.fd,
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(expected)
            stream.flush()
            os.fsync(stream.fileno())
        self.check()
        if read_private(self.node.fd, name, optional=True) != current or read_private(
            self.node.fd, TRANSITION
        ) != self.marker(intent):
            raise ValueError("GPU configuration changed outside the held original locks")
        os.rename(temporary, name, src_dir_fd=self.node.fd, dst_dir_fd=self.node.fd)
        os.fsync(self.node.fd)

    def run(self, generation, enabled):
        self.check()
        operation = self.host.state.get("operation") or {}
        token = operation.get("id")
        try:
            valid_token = type(token) is str and str(uuid.UUID(token)) == token
        except ValueError:
            valid_token = False
        candidates = [
            self.host.state.get("current"),
            operation.get("previous"),
            operation.get("candidate"),
        ]
        if not valid_token or not any(
            value and "gpu_worker" in value and context(value) == context(generation)
            for value in candidates
        ):
            raise ValueError(
                "GPU configuration target must belong to the original deployment operation"
            )
        target = self.snapshot(generation, enabled)
        state = self.host.state.get(
            "gpu_configuration", {"schema": 1, "active": None, "transition": None}
        )
        if (
            type(state) is not dict
            or set(state) != {"schema", "active", "transition"}
            or type(state["schema"]) is not int
            or state["schema"] != 1
        ):
            raise ValueError("Invalid GPU configuration publication journal")
        transition = state["transition"]
        if transition is None:
            old = state["active"]
            intent = None
        else:
            if (
                type(transition) is not dict
                or set(transition) != {"intent", "phase"}
                or transition["phase"] not in {"prepared", "published"}
            ):
                raise ValueError("Invalid GPU configuration transition")
            intent = transition["intent"]
            if (
                type(intent) is not dict
                or set(intent)
                != {"schema", "id", "operation", "binding_sha256", "from", "to", "files"}
                or type(intent["schema"]) is not int
                or intent["schema"] != 1
                or type(intent["id"]) is not str
                or not NONCE.fullmatch(intent["id"])
                or intent["operation"] != token
                or intent["binding_sha256"] != digest(self.binding)
                or intent["to"] != target
                or state["active"] != intent["from" if transition["phase"] == "prepared" else "to"]
            ):
                raise ValueError(
                    "GPU configuration recovery requires the exact original intent and target"
                )
            old = intent["from"]
        before, after = self.render(old), self.render(target)
        if (
            old
            and old["generation"]["id"] == target["generation"]["id"]
            and old["generation"] != target["generation"]
        ):
            raise ValueError("Cannot rewrite an immutable GPU deployment generation")
        hashes = {
            name: {"before": raw_digest(before[name]), "after": raw_digest(after[name])}
            for name in FILES
        }
        if intent is not None and intent["files"] != hashes:
            raise ValueError("GPU configuration intent file digests changed")
        self.validate_temporaries(intent, after)
        names = [target["generation"]["database"]]
        if old is not None:
            names.append(old["generation"]["database"])
        with self.host.database.gpu_configuration_guard(names, token) as check:
            self.database_check = check
            try:
                self.check()
                pair = self.read_pair()
                marker = read_private(self.node.fd, TRANSITION, optional=True)
                if intent is None:
                    if marker is not None or pair != before:
                        raise ValueError(
                            "Existing GPU files do not match the recorded active configuration"
                        )
                    if old == target:
                        return target["metadata"]
                    intent = {
                        "schema": 1,
                        "id": uuid.uuid4().hex,
                        "operation": token,
                        "binding_sha256": digest(self.binding),
                        "from": old,
                        "to": target,
                        "files": hashes,
                    }
                    transition = {"intent": intent, "phase": "prepared"}
                    state["transition"] = transition
                    self.host.state["gpu_configuration"] = state
                    self.host.save()
                expected_marker = self.marker(intent)
                middle = {FILES[0]: after[FILES[0]], FILES[1]: before[FILES[1]]}
                if transition["phase"] == "prepared":
                    if pair not in (before, middle, after) or (
                        marker != expected_marker and pair != before
                    ):
                        raise ValueError(
                            "GPU files are not an ordered state of the original publication"
                        )
                    self.establish_marker(expected_marker)
                    for name in FILES:
                        self.replace(intent, name, after[name], before[name])
                    if self.read_pair() != after:
                        raise ValueError("GPU target configuration was not durably published")
                    self.check()
                    # Recovery may observe both renames before their directory fsync.
                    os.fsync(self.node.fd)
                    state["active"], transition["phase"] = target, "published"
                    self.host.save()
                elif pair != after or marker not in (None, expected_marker):
                    raise ValueError("Confirmed GPU publication no longer matches its files")
                self.check()
                if self.read_pair() != after:
                    raise ValueError("GPU target configuration changed before confirmation")
                marker = read_private(self.node.fd, TRANSITION, optional=True)
                if marker is not None:
                    if marker != expected_marker:
                        raise ValueError("GPU publication marker changed before confirmation")
                    os.unlink(TRANSITION, dir_fd=self.node.fd)
                os.fsync(self.node.fd)
                # Remove only recognized temporaries that could precede a rename.
                for name in FILES:
                    temporary = self.temporary(intent, name)
                    raw = read_private(self.node.fd, temporary, optional=True)
                    if raw is not None:
                        if not after[name].startswith(raw):
                            raise ValueError(
                                "GPU configuration temporary changed before confirmation"
                            )
                        os.unlink(temporary, dir_fd=self.node.fd)
                os.fsync(self.node.fd)
                state["transition"] = None
                self.host.save()
                return target["metadata"]
            finally:
                self.database_check = None


def publish(host, node, binding, generation, *, enabled=False):
    return Publication(host, node, binding).run(generation, enabled)
