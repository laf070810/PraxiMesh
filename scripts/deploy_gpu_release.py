"""Internal whole-site release/recovery using the original GPU components.

No public activation bypass. Real Docker/PostgreSQL/GPU acceptance must precede
connecting these entry points to the public coordinator commands.
"""

import uuid
from pathlib import Path

from scripts.deploy_gpu import GPUDeploymentNode
from scripts.deploy_gpu_candidate import MAX_ATTEMPTS
from scripts.deploy_gpu_candidate import validate_record as validate_preparation
from scripts.deploy_gpu_check import require_protocol
from scripts.deploy_gpu_check import validate_record as validate_check
from scripts.deploy_gpu_config import (
    FILES,
    Publication,
    context,
    frozen,
    require_idle_journal,
    require_stopped_workers,
)
from scripts.deploy_gpu_retirement import validate_record as validate_retirement
from scripts.deploy_gpu_worker import ZERO, WorkerRuntime, stopped
from scripts.release import initial_content_commands, inspect_images, verify

PHASES = {
    "prepared",
    "draining",
    "quiescing",
    "copying",
    "migrating",
    "preparing",
    "exposing",
    "active",
    "drain_failed",
    "recovering",
    "needs_recovery",
    "failed_initial",
}
RECOVERY_PHASES = {
    "maintenance",
    "reconcile",
    "recover_source",
    "freeze_source",
    "retire_source",
    "stop_target",
    "drain_target",
    "reset_target",
    "prepare_target",
    "recover_target",
    "expose_target",
    "finished",
}


class GPUReleaseError(RuntimeError):
    pass


def gpu(generation):
    return generation is not None and "gpu" in generation["manifest"]


def archive_path(value):
    path = Path(value).absolute()
    if path.resolve() != path or not path.is_dir():
        raise GPUReleaseError("Coordinated release needs its original canonical archive")
    return path


def validate_record(record):
    try:
        valid = (
            type(record) is dict
            and set(record)
            == {
                "schema",
                "operation",
                "previous",
                "candidate",
                "archives",
                "initial_content",
                "phase",
                "recoveries",
            }
            and type(record["schema"]) is int
            and record["schema"] == 1
            and str(uuid.UUID(record["operation"])) == record["operation"]
            and type(record["initial_content"]) is bool
            and type(record["archives"]) is dict
            and record["phase"] in PHASES
            and type(record["recoveries"]) is list
            and len(record["recoveries"]) <= MAX_ATTEMPTS
        )
        generations = {
            row["id"]: row for row in (record["previous"], record["candidate"]) if row is not None
        }
        valid = valid and len(generations) == (2 if record["previous"] else 1)
        for row in generations.values():
            valid = valid and set(row) == {
                "id",
                "database",
                "web",
                "worker",
                "manifest",
                "release_sha256",
            } | ({"gpu_worker"} if gpu(row) else set())
        required = {record["candidate"]["id"]} | {
            row["id"] for row in generations.values() if gpu(row)
        }
        valid = valid and set(record["archives"]) == required
        valid = valid and all(
            type(path) is str and Path(path).is_absolute() for path in record["archives"].values()
        )
        for row in record["recoveries"]:
            valid = valid and type(row) is dict and set(row) == {"id", "target", "source", "phase"}
            valid = valid and str(uuid.UUID(row["id"])) == row["id"]
            valid = valid and row["phase"] in RECOVERY_PHASES
            valid = valid and row["target"] in set(generations) | {None}
            valid = valid and row["source"] in {
                key for key, value in generations.items() if gpu(value)
            } | {None}
        valid = valid and len({row["id"] for row in record["recoveries"]}) == len(
            record["recoveries"]
        )
        valid = valid and all(row["phase"] == "finished" for row in record["recoveries"][:-1])
    except (KeyError, ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise GPUReleaseError("Invalid coordinated GPU release journal")


class GPURelease:
    def __init__(self, host):
        self.host = host
        self.record = None

    def original(self):
        self.host.require_locked()
        self.host.check_gpu_attachment()
        self.host.database._require_operation_lock()
        if (
            "gpu" not in self.host.config
            or (self.host.state.get("gpu_attachment") or {}).get("phase") != "attached"
        ):
            raise GPUReleaseError("Coordinated release requires the attached original GPU node")
        if self.record is not None:
            validate_record(self.record)
            operation = self.host.state["operation"]
            if (
                any(operation[key] != self.record[key] for key in ("previous", "candidate"))
                or operation["id"] != self.record["operation"]
            ):
                raise GPUReleaseError("Coordinated release cannot change its original operation")
            exposed = operation.get("exposure_target")
            current = self.host.state.get("current")
            if exposed:
                if (
                    current is None
                    or current["id"] != exposed
                    or current not in (self.record["previous"], self.record["candidate"])
                ):
                    raise GPUReleaseError("Exposure decision lost its original database")
            elif current != self.record["previous"]:
                raise GPUReleaseError("Unexposed release changed its original current generation")
            for generation in (self.record["previous"], self.record["candidate"]):
                if generation is None:
                    continue
                prefix = f"px-{self.host.config['instance']}-{generation['id']}"
                if (
                    generation["database"] != self.host.database.name(generation["id"])
                    or any(generation[role] != prefix + "-" + role for role in ("web", "worker"))
                    or gpu(generation)
                    and generation["gpu_worker"] != prefix + "-gpu-worker"
                ):
                    raise GPUReleaseError(
                        "Coordinated generation service/database identity changed"
                    )

    def generation(self, identifier):
        if identifier is None:
            return None
        for value in (self.record["previous"], self.record["candidate"]):
            if value is not None and value["id"] == identifier:
                return value
        raise GPUReleaseError("Unknown generation in coordinated release")

    def verify_archive(self, generation, path):
        path = archive_path(path)
        if verify(path, generation["release_sha256"]) != generation["manifest"]:
            raise GPUReleaseError("Frozen release differs from the recorded generation")
        if gpu(generation):
            require_protocol(generation, path)
        return path

    def archive(self, generation):
        return self.verify_archive(generation, self.record["archives"][generation["id"]])

    def prior_archive(self, generation):
        paths = set()
        for record in self.host.state.get("gpu_preparations", {}).values():
            validate_preparation(record)
            if record["generation"] == generation:
                paths.add(record["release"])
        if len(paths) != 1:
            raise GPUReleaseError("Previous GPU generation needs one original preparation archive")
        return self.verify_archive(generation, paths.pop())

    def save(self, phase, *, public_phase=None):
        self.record["phase"] = phase
        validate_record(self.record)
        self.host.state.setdefault("gpu_releases", {})[self.record["operation"]] = self.record
        self.host.save(public_phase or phase)

    def load(self):
        self.original()
        values = self.host.state.get("gpu_releases", {})
        token = (self.host.state.get("operation") or {}).get("id")
        if type(values) is not dict or token not in values:
            raise GPUReleaseError("No original coordinated release to recover")
        for key, record in values.items():
            validate_record(record)
            if key != record["operation"]:
                raise GPUReleaseError("Coordinated release key changed")
        self.record = values[token]
        self.original()
        for identifier, path in self.record["archives"].items():
            self.verify_archive(self.generation(identifier), path)

    def inactive(self, *, maintenance=True):
        """Actual absent pair plus original stopped inventory; no cached retirement."""
        self.original()
        binding = self.host.state["gpu_attachment"]["binding"]
        engine = self.host.run("info", "--format", "{{.ID}}").decode().strip()
        with GPUDeploymentNode(
            self.host.config["gpu"], self.host.directory, self.host.state["site_id"], engine
        ).locked() as node:
            publication = Publication(self.host, node, binding)
            if maintenance:
                publication.check()
            else:
                node.require_owner(binding)
                require_stopped_workers(self.host, node, binding)
                require_idle_journal(node)
            state = self.host.state.get("gpu_configuration")
            if state not in (
                None,
                {"schema": 1, "active": None, "transition": None},
            ) or publication.read_pair() != {name: None for name in FILES}:
                raise GPUReleaseError(
                    "CPU generation requires the original GPU configuration to be retired"
                )
            from judge.container_deployment_config import TRANSITION
            from scripts.deploy_gpu import read_private

            if read_private(node.fd, TRANSITION, optional=True) is not None:
                raise GPUReleaseError("Finish original GPU configuration transition first")
            for record in self.host.state.get("gpu_retirements", {}).values():
                validate_retirement(record)
                if record["phase"] != "retired":
                    raise GPUReleaseError("Finish original GPU retirement first")
                if self.record and record["operation"] == self.record["operation"]:
                    self.host.database._require_frozen(record["generation"]["database"])

    def active_configuration(self):
        state = self.host.state.get("gpu_configuration")
        if state is None:
            return None
        if (
            type(state) is not dict
            or set(state) != {"schema", "active", "transition"}
            or type(state["schema"]) is not int
            or state["schema"] != 1
            or state["transition"] is not None
        ):
            raise GPUReleaseError("Finish original GPU configuration transition first")
        active = state["active"]
        if active is None:
            return None
        generation = self.generation(active["generation"]["id"])
        if (
            not gpu(generation)
            or active["generation"] != context(generation)
            or active["enabled"] is not True
        ):
            raise GPUReleaseError("Active GPU configuration is outside this release")
        return generation

    def previous_configuration(self, generation):
        runtime = WorkerRuntime(self.host, generation)
        config, metadata, _ = runtime.configuration()
        record = runtime.load()
        if not config["enabled"] or record is None or record["metadata"] != metadata:
            raise GPUReleaseError("Previous GPU generation lost its original configuration")
        runtime.inspect()

    def exposure_worker_stopped(self, generation):
        """Reconcile a failed exposure without restarting a live original worker."""
        if not gpu(generation):
            return not self.host.ready(generation)
        runtime = WorkerRuntime(self.host, generation)
        config, metadata, _ = runtime.configuration()
        record = runtime.load()
        if not config["enabled"] or record is None or record["metadata"] != metadata:
            raise GPUReleaseError("Exposure lost its original GPU configuration")
        info = runtime.inspect()
        if info["State"]["StartedAt"] != record["started_at"]:
            raise GPUReleaseError("Exposure worker changed its original startup")
        return stopped(info)

    def reconcile_configuration(self):
        for record in list(self.host.state.get("gpu_retirements", {}).values()):
            validate_retirement(record)
            if record["phase"] != "retired":
                if (
                    record["operation"] != self.record["operation"]
                    or self.generation(record["generation"]["id"]) != record["generation"]
                ):
                    raise GPUReleaseError("Cannot take over another GPU retirement")
                self.host.retire_gpu_configuration(record["generation"])
        state = self.host.state.get("gpu_configuration") or {}
        transition = state.get("transition")
        if transition is not None:
            intent = transition["intent"]
            target = intent["to"]
            if target is None or intent["operation"] != self.record["operation"]:
                raise GPUReleaseError("Unknown original configuration transition")
            generation = self.generation(target["generation"]["id"])
            if target["generation"] != context(generation) or target["enabled"] is not True:
                raise GPUReleaseError("Configuration transition changed its original target")
            self.host.publish_gpu_configuration(generation, enabled=True)
        return self.active_configuration()

    def pause(self, generation):
        self.host.control(
            generation, "judge_admission", "pause", "--token", self.record["operation"]
        )

    def cpu_drain(self, generation):
        self.host.database.thaw(generation["database"])
        self.pause(generation)
        self.host.service(generation, "worker")
        self.host.wait_ready(generation)
        self.host.control(generation, "maintain")
        self.host.control(
            generation,
            "judge_admission",
            "drain",
            "--token",
            self.record["operation"],
            "--timeout",
            "180",
            timeout=210,
        )

    def cleanup_rounds(self, generation):
        for record in list(self.host.state.get("gpu_candidate_checks", {}).values()):
            validate_check(record)
            if (
                record["operation"] == self.record["operation"]
                and record["generation"] == context(generation)
                and record["phase"] not in {"verified", "cleaned"}
            ):
                self.host.check_gpu_candidate(
                    generation, self.archive(generation), cleanup=True, round_id=record["round_id"]
                )

    def settle_gpu(self, generation):
        self.host.database.thaw(generation["database"])
        self.pause(generation)
        info = self.host.gpu_worker_runtime(generation, "create")
        key = self.record["operation"] + ":" + generation["gpu_worker"]
        if info["State"]["StartedAt"] in ZERO and key not in self.host.state.get(
            "gpu_recoveries", {}
        ):
            self.host.gpu_worker_runtime(generation, "create", mode="check")
            self.host.gpu_worker_runtime(generation, "stop", mode="check")
            for record in self.host.state.get("gpu_candidate_checks", {}).values():
                validate_check(record)
                if record["generation"] == context(generation):
                    raise GPUReleaseError(
                        "Unstarted worker has unexplained GPU verification history"
                    )
            # No GPU startup just to retire an unused configuration. Freeze below
            # still checks actual original objects, all slots, queue and journal.
            return
        self.host.service(generation, "worker")
        self.host.wait_ready(generation)
        self.host.control(generation, "maintain")
        self.host.recover_gpu_worker(generation, self.archive(generation))
        self.cleanup_rounds(generation)

    def expose_cpu(self, generation, phase):
        port = self.host.port(generation)
        route = self.host.proxy.probe()["port"]
        decided = (
            self.host.state["operation"].get("exposure_target") == generation["id"]
            and self.host.state.get("current") == generation
        )
        if route is not None and (not decided or route != port):
            raise GPUReleaseError("CPU exposure found an unrelated or undecided route")
        self.inactive(maintenance=route is None)
        if not self.host.ready(generation):
            raise GPUReleaseError("Original CPU worker is not ready for exposure")
        self.host.state["current"] = frozen(generation)
        self.host.state["operation"]["exposure_target"] = generation["id"]
        self.host.save("exposing")
        self.host.control(
            generation, "judge_admission", "resume", "--token", self.record["operation"]
        )
        self.inactive(maintenance=route is None)
        if (
            not self.host.ready(generation)
            or self.host.port(generation) != port
            or self.host.proxy.probe()["port"] != route
        ):
            raise GPUReleaseError("Original CPU worker lost readiness")
        if route is None:
            self.host.proxy.activate(port)
        self.host.health(generation, external=True)
        self.host.save(phase)

    def expose(self, generation, phase="active"):
        if gpu(generation):
            return self.host.expose_gpu(generation, self.archive(generation), phase=phase)
        return self.expose_cpu(generation, phase)

    def apply(self, release, digest, *, initial_content=False):
        self.original()
        operation = self.host.state.get("operation")
        if operation and operation["phase"] not in {"active", "rolled_back", "failed_initial"}:
            raise GPUReleaseError("Recover the unfinished original deployment first")
        release = archive_path(release)
        manifest = verify(release, digest)
        previous = frozen(self.host.state.get("current"))
        if previous and initial_content:
            raise GPUReleaseError("Initial content is only allowed on first installation")
        identifier = uuid.uuid4().hex[:12]
        prefix = f"px-{self.host.config['instance']}-{identifier}"
        candidate = {
            "id": identifier,
            "database": self.host.database.name(identifier),
            "web": prefix + "-web",
            "worker": prefix + "-worker",
            "manifest": manifest,
            "release_sha256": digest,
        }
        if gpu(candidate):
            candidate["gpu_worker"] = prefix + "-gpu-worker"
            require_protocol(candidate, release)
        archives = {identifier: str(release)}
        if gpu(previous):
            archives[previous["id"]] = str(self.prior_archive(previous))
            self.previous_configuration(previous)
        else:
            self.inactive(maintenance=False)
        self.host.run("image", "load", "--input", str(release / "images.tar"), timeout=900)
        inspect_images(manifest)
        self.host.proxy.probe()
        self.host.networks()
        self.host.cleanup_controls()
        self.host.database.ensure_roles()
        token = str(uuid.uuid4())
        self.record = {
            "schema": 1,
            "operation": token,
            "previous": previous,
            "candidate": candidate,
            "archives": archives,
            "initial_content": initial_content,
            "phase": "prepared",
            "recoveries": [],
        }
        self.host.state["operation"] = {
            "id": token,
            "phase": "prepared",
            "previous": previous,
            "candidate": candidate,
            "exposure_target": None,
        }
        self.save("prepared")
        try:
            if previous:
                self.save("draining")
                self.host.control(
                    previous,
                    "judge_admission",
                    "drain",
                    "--token",
                    token,
                    "--timeout",
                    "180",
                    timeout=210,
                )
            self.save("quiescing")
            self.host.proxy.maintenance()
            self.host.quiesce(previous)
            if gpu(previous) and not gpu(candidate):
                self.host.retire_gpu_configuration(previous)
            self.save("copying")
            if previous:
                backup = self.host.directory / "backups"
                backup.mkdir(mode=0o700, exist_ok=True)
                self.host.state["operation"]["backup"] = self.host.database.backup_restore(
                    previous["database"], candidate["database"], backup / (identifier + ".dump")
                )
            else:
                self.host.database.create(candidate["database"])
            self.save("migrating")
            self.host.control(candidate, "migrate", "--noinput", timeout=300)
            if initial_content:
                for command in initial_content_commands(manifest):
                    self.host.control(candidate, *command)
            self.host.database.grant(candidate["database"])
            self.host.database.invalidate_health(candidate["database"])
            self.pause(candidate)
            self.save("preparing")
            self.host.prepare_candidate(candidate, release)
            self.save("exposing")
            self.expose(candidate)
            self.save("active")
        except Exception as error:
            self.host.state["operation"]["failure"] = self.host.failure_record(error)
            if self.host.state["operation"]["exposure_target"]:
                self.save("needs_recovery")
                self.host.proxy.maintenance()
            elif self.record["phase"] == "draining":
                self.save("drain_failed")
            else:
                self.save("recovering")
                self.recover()
            raise GPUReleaseError(
                "Coordinated release failed; original data and journal retained"
            ) from None

    @property
    def attempt(self):
        return self.record["recoveries"][-1]

    def recovery_phase(self, phase):
        self.attempt["phase"] = phase
        self.save("recovering")

    def recover(self):
        self.load()
        operation = self.host.state["operation"]
        target = (
            self.host.state["current"]
            if operation.get("exposure_target")
            else self.record["previous"]
        )
        target_id = target["id"] if target else None
        if not self.record["recoveries"] or self.attempt["phase"] == "finished":
            if len(self.record["recoveries"]) >= MAX_ATTEMPTS:
                raise GPUReleaseError("Recovery attempt limit reached; all history retained")
            self.record["recoveries"].append(
                {
                    "id": str(uuid.uuid4()),
                    "target": target_id,
                    "source": None,
                    "phase": "maintenance",
                }
            )
            self.save("recovering")
        elif self.attempt["target"] != target_id:
            raise GPUReleaseError("Recovery cannot change its original target database")
        try:
            if self.attempt["phase"] == "expose_target" and self.exposure_worker_stopped(target):
                # A lost acknowledgement with a live worker follows the existing
                # exposure. A confirmed original exit needs a fresh recovery and
                # verification round on the same decided database.
                self.recovery_phase("maintenance")
            self._recover(target)
        except Exception as error:
            self.host.state["operation"]["failure"] = self.host.failure_record(error)
            self.save("recovering")
            raise

    def _recover(self, target):
        self.original()
        if self.attempt["phase"] == "maintenance":
            self.host.proxy.maintenance()
            self.host.cleanup_controls()
            self.recovery_phase("reconcile")
        if self.attempt["phase"] == "reconcile":
            source = self.reconcile_configuration()
            self.attempt["source"] = source["id"] if source else None
            candidate = self.record["candidate"]
            if (
                candidate != target
                and candidate != source
                and self.host.database.owned_exists(candidate["database"])
            ):
                self.host.stop(candidate)
            self.recovery_phase(
                "stop_target"
                if gpu(target) and source == target
                else "recover_source"
                if source
                else "drain_target"
                if target and not gpu(target)
                else "reset_target"
            )
        source = self.generation(self.attempt["source"])
        if self.attempt["phase"] == "recover_source":
            self.settle_gpu(source)
            self.recovery_phase("freeze_source")
        if self.attempt["phase"] == "freeze_source":
            self.host.quiesce(source)
            self.recovery_phase("retire_source")
        if self.attempt["phase"] == "retire_source":
            if not gpu(target):
                self.host.retire_gpu_configuration(source)
            self.recovery_phase("drain_target" if target and not gpu(target) else "reset_target")
        if self.attempt["phase"] == "stop_target":
            self.host.database.thaw(target["database"])
            self.pause(target)
            self.host.gpu_worker_runtime(target, "create")
            self.host.gpu_worker_runtime(target, "stop")
            self.recovery_phase("recover_target")
        if self.attempt["phase"] == "drain_target":
            self.inactive()
            self.cpu_drain(target)
            self.recovery_phase("reset_target")
        if self.attempt["phase"] == "reset_target":
            if target is None:
                self.inactive()
                self.attempt["phase"] = "finished"
                self.save("failed_initial")
                return
            self.host.stop(target)
            if not gpu(target):
                self.host.database.freeze(target["database"])
            self.host.database.thaw(target["database"])
            self.host.database.invalidate_health(target["database"])
            self.pause(target)
            self.host.replace_web(target)
            self.recovery_phase("prepare_target")
        if self.attempt["phase"] in {"prepare_target", "recover_target"}:
            try:
                if gpu(target):
                    self.host.prepare_candidate(
                        target,
                        self.archive(target),
                        recover=self.attempt["phase"] == "recover_target",
                    )
                else:
                    self.inactive()
                    self.host.service(target, "worker")
                    self.host.check(target, self.record["operation"])
            except Exception:
                if gpu(target):
                    self.recovery_phase("recover_target")
                raise
            self.recovery_phase("expose_target")
        if self.attempt["phase"] == "expose_target":
            completion = "rolled_back" if target == self.record["previous"] else "active"
            self.expose(target, completion)
            self.attempt["phase"] = "finished"
            self.save("active", public_phase=completion)
