"""Bound daemon OOM defaults for coordinator-owned containers, not GPU tasks."""

import os
import socket
import struct
import uuid

from judge.container_controller import identity
from judge.container_deployment_config import digest
from scripts.deploy_web import fingerprint


def validate_daemon(value):
    try:
        valid = (
            type(value) is dict
            and set(value)
            == {"engine", "socket", "peer", "start_ticks", "oom_score_adj", "version"}
            and str(uuid.UUID(value["engine"])) == value["engine"]
            and type(value["socket"]) is list
            and len(value["socket"]) == 2
            and all(type(n) is int and n >= 0 for n in value["socket"])
            and type(value["peer"]) is list
            and len(value["peer"]) == 3
            and all(type(n) is int and n > 0 for n in value["peer"])
            and value["peer"][0] > 1
            and type(value["start_ticks"]) is int
            and value["start_ticks"] > 0
            and type(value["oom_score_adj"]) is int
            and -1000 <= value["oom_score_adj"] <= 1000
            and type(value["version"]) is str
            and 1 <= len(value["version"]) <= 64
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError("Invalid original rootless daemon OOM observation")


def observe_daemon(engine):
    """Read kernel peer identity; never infer the daemon PID from a name or service."""
    info = engine.verify()
    if info.get("OomKillDisable") is not False:
        raise ValueError("Expected cgroup v2 OOM killer capability")
    before = identity(engine.path)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(2)
        peer.connect(str(engine.path))
        credentials = list(
            struct.unpack(
                "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            )
        )
        pid, uid, gid = credentials
        if pid <= 1 or uid != os.getuid() or gid != os.getgid() or uid == 0 or gid == 0:
            raise ValueError("Docker peer is not the original non-root operator")
        directory = os.open(f"/proc/{pid}", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if os.fstat(directory).st_uid != uid:
                raise ValueError("Docker peer process owner changed")

            def read(name):
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
                with os.fdopen(fd) as stream:
                    value = stream.read(8193)
                if len(value) > 8192:
                    raise ValueError("Oversized Docker peer process observation")
                return value

            def birth():
                value = read("stat")
                if int(value.partition(" (")[0]) != pid:
                    raise ValueError("Docker peer process identity changed")
                return int(value.rsplit(")", 1)[1].split()[19])

            ticks = birth()
            score = int(read("oom_score_adj").strip())
            if birth() != ticks:
                raise ValueError("Docker peer process restarted during observation")
        finally:
            os.close(directory)
    engine.check_socket()
    if identity(engine.path) != before:
        raise ValueError("Original Docker socket changed during OOM observation")
    result = {
        "engine": info["ID"],
        "socket": before,
        "peer": credentials,
        "start_ticks": ticks,
        "oom_score_adj": score,
        "version": info["ServerVersion"],
    }
    validate_daemon(result)
    return result


def legacy_identity(record):
    return digest(
        {
            key: record[key]
            for key in (
                "schema",
                "token",
                "mode",
                "generation",
                "binding_sha256",
                "metadata",
                "network",
                "image_config_sha256",
                "arguments",
                "id",
                "runtime_sha256",
            )
        }
    )


def runtime_fingerprint(info, record, proof=None):
    """Keep every effective setting in the hash; only false/null share a meaning."""
    if proof is not None:
        try:
            valid = (
                type(proof) is dict
                and set(proof)
                == {
                    "schema",
                    "identity_sha256",
                    "operation",
                    "started_at",
                    "observed_sha256",
                    "state_sha256",
                    "daemon",
                }
                and type(proof["schema"]) is int
                and proof["schema"] == 1
                and record["schema"] == 1
                and record["mode"] == "check"
                and record["phase"] in {"start_requested", "stop_requested", "stopped"}
                and proof["identity_sha256"] == legacy_identity(record)
                and str(uuid.UUID(proof["operation"])) == proof["operation"]
                and record["operation"] == proof["operation"]
                and proof["started_at"] == info["State"]["StartedAt"]
                and proof["observed_sha256"] == fingerprint(info)
                and proof["state_sha256"] == digest(info["State"])
                and (
                    record["phase"] == "start_requested"
                    or record["started_at"] == proof["started_at"]
                )
            )
            validate_daemon(proof["daemon"])
        except (KeyError, TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("Original stopped preflight OOM proof changed")
        return record["runtime_sha256"]
    if record["schema"] == 1:
        return fingerprint(info)
    if record["schema"] not in {2, 3}:
        raise ValueError("Unknown worker OOM lifecycle schema")
    validate_daemon(record["oom"])
    runtime = info["HostConfig"]
    if (
        "OomKillDisable" not in runtime
        or (runtime["OomKillDisable"] is not False and runtime["OomKillDisable"] is not None)
        or type(runtime.get("OomScoreAdj")) is not int
        or runtime["OomScoreAdj"] != max(0, record["oom"]["oom_score_adj"])
    ):
        raise ValueError("Worker effective OOM settings differ from the creation intent")
    return fingerprint(info | {"HostConfig": runtime | {"OomKillDisable": False}})
