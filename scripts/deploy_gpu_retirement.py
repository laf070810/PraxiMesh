"""Retire exactly one drained, frozen GPU configuration before CPU exposure.

Keep the original node, locks, task journal, containers and verification history.
Only the two owned configuration files are removed, under the publication marker.
"""

import uuid

from judge.container_deployment_config import digest
from scripts.deploy_gpu_config import CONTEXT, HEX, Publication, context, frozen


class GPURetirementError(RuntimeError):
    pass


def record_key(record):
    return record["operation"] + ":" + record["generation"]["gpu_worker"]


def validate_record(record):
    try:
        valid = (
            type(record) is dict
            and set(record)
            == {"schema", "operation", "generation", "binding_sha256", "from", "phase"}
            and type(record["schema"]) is int
            and record["schema"] == 1
            and str(uuid.UUID(record["operation"])) == record["operation"]
            and set(record["generation"]) == CONTEXT | {"web", "worker"}
            and HEX.fullmatch(record["binding_sha256"])
            and record["phase"] in {"prepared", "retired"}
            and type(record["from"]) is dict
            and set(record["from"]) == {"generation", "enabled", "metadata"}
            and record["from"]["generation"] == context(record["generation"])
            and type(record["from"]["enabled"]) is bool
            and type(record["from"]["metadata"]) is dict
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise GPURetirementError("Invalid original GPU retirement journal")


class GPURetirement:
    def __init__(self, host, node, binding, generation):
        self.host, self.node, self.binding = host, node, binding
        self.generation = frozen(generation)
        self.publication = Publication(host, node, binding)
        self.record = None

    def original(self):
        self.publication.check()
        operation = self.host.state.get("operation") or {}
        token = operation.get("id")
        try:
            valid = type(token) is str and str(uuid.UUID(token)) == token
        except ValueError:
            valid = False
        prefix = f"px-{self.host.config['instance']}-{self.generation['id']}"
        if (
            not valid
            or operation.get("exposure_target")
            or set(self.generation) != CONTEXT | {"web", "worker"}
            or self.generation["gpu_worker"] != prefix + "-gpu-worker"
            or any(self.generation[k] != prefix + "-" + k for k in ("web", "worker"))
            or not any(
                value == self.generation
                for value in (
                    self.host.state.get("current"),
                    operation.get("previous"),
                    operation.get("candidate"),
                )
            )
        ):
            raise GPURetirementError(
                "Retirement requires the complete original unexposed generation"
            )
        if self.record is not None and (
            self.record["operation"] != token
            or self.record["generation"] != self.generation
            or self.record["binding_sha256"] != digest(self.binding)
        ):
            raise GPURetirementError("Original GPU retirement identity changed")
        # No row transaction is held here: there must be zero DB sessions.
        self.host.database._require_frozen(self.generation["database"])
        return token

    def configuration(self):
        value = self.host.state.get("gpu_configuration")
        if (
            type(value) is not dict
            or set(value) != {"schema", "active", "transition"}
            or type(value["schema"]) is not int
            or value["schema"] != 1
        ):
            raise GPURetirementError("Original GPU configuration journal is missing")
        return value

    def save(self, phase):
        self.record["phase"] = phase
        validate_record(self.record)
        self.host.state.setdefault("gpu_retirements", {})[self.key] = frozen(self.record)
        self.host.save()

    def load(self):
        token = self.original()
        self.key = token + ":" + self.generation["gpu_worker"]
        values = self.host.state.get("gpu_retirements", {})
        if type(values) is not dict:
            raise GPURetirementError("Invalid GPU retirement registry")
        for key, value in values.items():
            validate_record(value)
            if key != record_key(value) or key != self.key and value["phase"] != "retired":
                raise GPURetirementError("Finish the original GPU retirement first")
        self.record = frozen(values[self.key]) if self.key in values else None
        configuration = self.configuration()
        if self.record is None:
            active = configuration["active"]
            if (
                configuration["transition"] is not None
                or active is None
                or active.get("generation") != context(self.generation)
            ):
                raise GPURetirementError(
                    "Retirement requires the original fully published configuration"
                )
            expected = self.publication.render(active)
            if self.publication.read_pair() != expected:
                raise GPURetirementError("Original GPU configuration files changed")
            self.record = {
                "schema": 1,
                "operation": token,
                "generation": self.generation,
                "binding_sha256": digest(self.binding),
                "from": frozen(active),
                "phase": "prepared",
            }
            self.save("prepared")
        self.original()
        self.publication.render(self.record["from"])
        transition = configuration["transition"]
        if transition is None:
            valid = configuration["active"] in (None, self.record["from"])
        else:
            intent = transition.get("intent") or {}
            valid = intent.get("from") == self.record["from"] and intent.get("to") is None
        if (
            not valid
            or self.record["phase"] == "retired"
            and (configuration["active"] is not None or transition is not None)
        ):
            raise GPURetirementError("GPU retirement cannot consume another configuration")

    def run(self):
        self.load()
        self.publication.unpublish(self.generation)
        self.original()
        if (
            self.configuration()["active"] is not None
            or self.configuration()["transition"] is not None
        ):
            raise GPURetirementError("GPU retirement was not durably confirmed")
        if self.record["phase"] != "retired":
            self.save("retired")
        return frozen(self.record)
