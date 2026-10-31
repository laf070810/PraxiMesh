"""One owned, device-free compilation through the trusted container controller.

The result is compilation metadata only. It is neither a verdict nor a receipt.
Learner source is sent only after the trusted caller's begin callback succeeds.
"""

import copy
import re

from judge.container_compile_protocol import (
    validate_compiled,
    validate_identity,
    validate_request,
)
from judge.container_compiler import CompilerContainerService, compiler_digest
from judge.container_connection import ContainerConnection
from judge.container_contract import PYTHON, WORKLOAD_PROFILE, resource_limits
from judge.container_journal import ContainerJournal
from judge.sandbox import SandboxUnavailable

GUEST_CLEANUP = [{"id": "compiler", "stopped": True, "removed": True}]
ERRORS = {"syntax_error", "runtime_error", "time_limit", "memory_limit", "output_limit"}


class CompilationCancelled(SandboxUnavailable):
    """The trusted caller cancelled; never a learner compilation error."""


def checked_result(value, target, compiled):
    fields = {"event", "status", "ready", "executed", "cleanup"}
    if type(value) is not dict or type(value.get("status")) is not str:
        raise SandboxUnavailable("Invalid compilation completion")
    status = value["status"]
    if status == "compiled":
        fields.add("compilation")
        validate_compiled(value.get("compilation"), target)
        if value["compilation"] != compiled:
            raise SandboxUnavailable("Compilation metadata changed before cleanup")
    elif status == "platform_error":
        if "failure_kind" in value:
            fields.add("failure_kind")
            if type(value["failure_kind"]) is not str or not re.fullmatch(
                "[A-Za-z][A-Za-z0-9_]{0,63}", value["failure_kind"]
            ):
                raise SandboxUnavailable("Unbounded compilation failure metadata")
    elif status not in ERRORS:
        raise SandboxUnavailable("Unknown compilation status")
    if (
        set(value) != fields
        or value["event"] != "compile-checked"
        or value["ready"] is not False
        or value["executed"] is not False
        or value["cleanup"] != GUEST_CLEANUP
        or any(
            item.get(key) is not True for item in value["cleanup"] for key in ("stopped", "removed")
        )
    ):
        raise SandboxUnavailable("Compilation guest cleanup was not confirmed")
    # Do not expose raw failure classes or guest-supplied extra fields.
    result = {"status": status, "executed": False, "ready": False}
    if status == "compiled":
        result["compilation"] = copy.deepcopy(compiled)
    return result


class CompilationTask:
    def __init__(self, journal, job_id, reservation_token, *, image):
        if type(journal) is not ContainerJournal:
            raise ValueError("An owned container journal is required")
        journal.check()
        if journal.load() is not None:
            raise SandboxUnavailable("Recover the previous task before compiling")
        self.journal = journal
        self.service = CompilerContainerService(
            journal,
            job_id,
            reservation_token,
            image=image,
            resource_profile=WORKLOAD_PROFILE,
        )
        self.contract = compiler_digest()
        self.used = False
        self.status = self.cleanup = None

    def original_record(self):
        record = self.journal.load()
        if record is None:
            if self.service.container_id is not None:
                raise SandboxUnavailable("Started compilation lost its original record")
            return None
        if (
            record["owner"] != self.service.owner
            or record["image"] != self.service.image
            or record["version"] != 2
            or record["resource_profile"] != WORKLOAD_PROFILE
            or record["container_id"] != self.service.container_id
        ):
            raise SandboxUnavailable("Compilation ownership changed; retain the record")
        return record

    def recover(self):
        if self.original_record() is None:
            return
        self.cleanup = self.journal.recover()
        if (
            self.cleanup is None
            or self.cleanup.get("removed") is not True
            or self.cleanup.get("ready") is not False
            or self.journal.load() is not None
        ):
            raise SandboxUnavailable("Compilation task cleanup was not confirmed")

    def run(self, message, *, deadline, on_begin, on_tick, on_compiled=lambda _value: None):
        if self.used:
            raise ValueError("A compilation task can only be invoked once")
        self.used = True
        message = copy.deepcopy(validate_request(message))
        connection = None
        try:
            self.status = self.service.start(
                [PYTHON, "-I", "/opt/praximesh/container_compile_entry.py", "normal"],
                deadline=deadline,
                on_tick=on_tick,
            )
            if (
                self.status["limits"] != resource_limits(WORKLOAD_PROFILE)
                or compiler_digest() != self.contract
            ):
                raise SandboxUnavailable("Compilation image or resource identity changed")
            connection = ContainerConnection(self.service, self.status, on_tick=on_tick)
            on_begin()
            self.original_record()
            connection.tick()
            channel = connection.channel
            channel.send(message)
            validate_identity(channel.receive())
            channel.send({"event": "begin"})
            value = channel.receive()
            compiled = None
            if type(value) is dict and value.get("event") == "compiled":
                if set(value) != {"event", "compilation"}:
                    raise SandboxUnavailable("Invalid compilation progress")
                validate_compiled(value["compilation"], message["architecture"])
                compiled = copy.deepcopy(value["compilation"])
                on_compiled(copy.deepcopy(compiled))
                self.original_record()
                connection.tick()
                channel.send({"event": "finish"})
                value = channel.receive()
            result = checked_result(value, message["architecture"], compiled)
            channel.send({"event": "acknowledged"})
            connection.finish()
        finally:
            try:
                if connection is not None:
                    connection.close()
            finally:
                self.recover()
        if compiler_digest() != self.contract:
            raise SandboxUnavailable("Compilation source identity changed during execution")
        return {
            **result,
            "artifacts": {
                "image": self.service.image,
                "compiler_contract": self.contract,
                "resource_profile": WORKLOAD_PROFILE,
                "limits": copy.deepcopy(self.status["limits"]),
            },
        }
