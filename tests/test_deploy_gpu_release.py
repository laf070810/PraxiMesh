"""Whole-flow ordering with durable files/CPU crashes and explicit service models.

Component suites separately exercise real source archives, configuration files
and original device locks. This model does not claim Docker, PG or GPU success.
"""

import copy
import json
import os
import signal
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import deploy
from scripts import deploy_gpu_release as module
from scripts.deploy_database import DeploymentDatabaseError
from scripts.deploy_gpu_config import context
from scripts.deploy_gpu_release import GPURelease, GPUReleaseError, gpu

ZERO = "0001-01-01T00:00:00Z"


def generation(identifier, kind):
    prefix = "px-example-" + identifier
    manifest = {
        "images": {"web": "sha256:" + "a" * 64, "worker": "sha256:" + "b" * 64},
        "judge": {"bundle_sha256": "c" * 64, "policy_sha256": "d" * 64},
    }
    if kind == "gpu":
        manifest["gpu"] = {"model_only": True}
    value = {
        "id": identifier,
        "database": "px_example_" + identifier,
        "web": prefix + "-web",
        "worker": prefix + "-worker",
        "manifest": manifest,
        "release_sha256": "e" * 64,
    }
    if kind == "gpu":
        value["gpu_worker"] = prefix + "-gpu-worker"
    return value


class Database:
    def __init__(self, host):
        self.host = host

    def _require_operation_lock(self):
        self.host.require_locked()  # PG operation lock is an explicit model here.

    def name(self, identifier):
        return "px_example_" + identifier

    def ensure_roles(self):
        self.host.event("roles")

    def owned_exists(self, name):
        data = self.host.read()
        if name in data["unowned"]:
            raise DeploymentDatabaseError("modeled existing unowned database")
        return name in data["rows"]

    def _require_frozen(self, name):
        assert self.owned_exists(name) and name in self.host.read()["frozen"]

    def freeze(self, name):
        assert self.owned_exists(name)
        self.host.change(
            lambda data: data["frozen"].append(name) if name not in data["frozen"] else None
        )
        self.host.event("freeze", name)

    def thaw(self, name):
        assert self.owned_exists(name)
        self.host.change(
            lambda data: data["frozen"].remove(name) if name in data["frozen"] else None
        )
        self.host.event("thaw", name)

    def create(self, name):
        assert not self.owned_exists(name)
        self.host.change(lambda data: data["rows"].update({name: []}))
        self.freeze(name)
        self.host.event("create", name)

    def backup_restore(self, source, target, path):
        self._require_frozen(source)
        assert self.host.proxy.probe()["port"] is None
        self.create(target)
        self.host.change(
            lambda data: data["rows"].update({target: copy.deepcopy(data["rows"][source])})
        )
        self.host.event("copy", source, target)
        return {"model_only": True}

    def grant(self, name):
        self._require_frozen(name)
        self.thaw(name)
        self.host.event("grant", name)

    def invalidate_health(self, name):
        self.host.event("invalidate", name)


class Host(deploy.Deployment):
    def __init__(self, directory, previous=None):
        self.directory = directory
        self.path = directory / "deployment.json"
        self.model_path = directory / "model.json"
        self.config = {"instance": "example", "gpu": {"model_only": True}}
        self.config_digest = "model-config"
        self._lock_descriptor = None
        self._database = Database(self)
        self.hook = lambda event: None
        self.state = None
        self.previous_release = directory / "previous-release"
        self.target_release = directory / "target-release"
        self.proxy = SimpleNamespace(
            ensure_directory=lambda: None,  # Owned temporary directory already exists.
            probe=lambda: {"port": self.read()["port"]},
            maintenance=self.maintenance,
            activate=self.activate,
        )
        if self.path.exists():
            return
        directory.mkdir(mode=0o700)
        self.previous_release.mkdir()
        self.target_release.mkdir()
        old = generation("111111111111", previous) if previous else None
        for path, row in (
            (self.previous_release, old or generation("111111111111", "cpu")),
            (self.target_release, generation("222222222222", "gpu")),
        ):
            (path / "manifest.json").write_text(json.dumps(row["manifest"]))
        self.state = {
            "format": 1,
            "site_id": "a" * 32,
            "instance": "example",
            "config_digest": self.config_digest,
            "current": old,
            "operation": {"id": str(uuid.uuid4()), "phase": "active"} if old else None,
            "gpu_attachment": {"phase": "attached"},
        }
        data = {
            "events": [],
            "rows": {old["database"]: ["original-user-write"]} if old else {},
            "frozen": [],
            "unowned": [],
            "pause": {},
            "port": 21000 if old else None,
            "services": [[old["id"], role] for role in ("web", "worker")] if old else [],
            "gpu": {},
            "gpu_work": {},
            "serial": 1,
        }
        if gpu(old):
            self.state["gpu_configuration"] = {
                "schema": 1,
                "active": {"generation": context(old), "enabled": True, "metadata": {}},
                "transition": None,
            }
            data["gpu"][old["id"]] = {
                "running": True,
                "started": "2026-11-02T00:00:01Z",
                "recovered": None,
            }
        deploy.atomic_json(self.model_path, data)
        deploy.atomic_json(self.path, self.state)

    def read(self):
        return json.loads(self.model_path.read_text())

    def change(self, mutation):
        data = self.read()
        mutation(data)
        deploy.atomic_json(self.model_path, data)

    def event(self, *event):
        self.change(lambda data: data["events"].append(list(event)))
        self.hook(event)

    def check_gpu_attachment(self):
        assert self.state["gpu_attachment"]["phase"] == "attached"

    def save(self, phase=None):
        super().save(phase)
        self.event("save", self.state["operation"]["phase"] if self.state["operation"] else None)

    def run(self, *args, **kwargs):
        assert args[:2] == ("image", "load")
        self.event("image_load")
        return b""

    def networks(self):
        self.event("networks")

    def cleanup_controls(self):
        self.event("controls_cleaned")

    def maintenance(self):
        self.change(lambda data: data.update(port=None))
        self.event("maintenance")

    def activate(self, port):
        current = self.state["current"]
        durable = json.loads(self.path.read_text())
        assert durable["operation"]["exposure_target"] == current["id"]
        assert durable["current"] == current
        self.change(lambda data: data.update(port=port))
        self.change(lambda data: data["rows"][current["database"]].append("write-after-exposure"))
        self.event("activate", current["id"])

    def control(self, generation, *command, **kwargs):
        name = generation["database"]
        assert self.database.owned_exists(name)
        if command[:2] in (("judge_admission", "pause"), ("judge_admission", "drain")):
            assert command[2:4] == ("--token", self.state["operation"]["id"])
            self.change(lambda data: data["pause"].update({name: self.state["operation"]["id"]}))
        elif command[:2] == ("judge_admission", "resume"):
            assert self.state["operation"]["exposure_target"] == generation["id"]
            self.change(lambda data: data["pause"].pop(name, None))
        self.event("control", generation["id"], *command)
        return b"{}"

    def stop(self, generation):
        if generation:
            self.change(
                lambda data: data.update(
                    services=[row for row in data["services"] if row[0] != generation["id"]]
                )
            )
            self.event("stop", generation["id"])

    def service(self, generation, role):
        assert generation["database"] not in self.read()["frozen"]
        row = [generation["id"], role]
        self.change(
            lambda data: data["services"].append(row) if row not in data["services"] else None
        )
        self.event("service", *row)

    def replace_web(self, generation):
        assert [generation["id"], "web"] not in self.read()["services"]
        self.event("replace_web", generation["id"])
        self.service(generation, "web")

    def ready(self, generation):
        return [generation["id"], "worker"] in self.read()["services"]

    def wait_ready(self, generation):
        assert self.ready(generation)

    def port(self, generation):
        return 21000 if generation["id"] == "111111111111" else 21001

    def check(self, generation, token):
        assert self.ready(generation) and self.read()["port"] is None
        self.event("check_cpu", generation["id"])

    def health(self, generation, *, external):
        assert self.ready(generation)
        self.event("health", generation["id"])

    def publish_gpu_configuration(self, generation, *, enabled):
        assert not any(row["running"] for row in self.read()["gpu"].values())
        self.state["gpu_configuration"] = {
            "schema": 1,
            "active": {"generation": context(generation), "enabled": enabled, "metadata": {}},
            "transition": None,
        }
        self.save()
        self.event("publish_gpu", generation["id"])

    def gpu_worker_runtime(self, generation, action, *, mode="worker"):
        assert self.state["gpu_configuration"]["active"]["generation"] == context(generation)
        key = generation["id"] + ("-check" if mode == "check" else "")
        if action == "create":
            self.change(
                lambda data: data["gpu"].setdefault(
                    key, {"running": False, "started": ZERO, "recovered": None}
                )
            )
        elif action == "stop":
            self.change(lambda data: data["gpu"][key].update(running=False))
        else:
            pytest.fail("Unexpected runtime action")
        self.event("runtime", generation["id"], mode, action)
        return {"Id": "a" * 64, "State": {"StartedAt": self.read()["gpu"][key]["started"]}}

    def start_gpu(self, generation):
        def start(data):
            data["serial"] += 1
            data["gpu"].setdefault(generation["id"], {})
            data["gpu"][generation["id"]].update(
                running=True, started=f"2026-11-02T00:00:{data['serial']:02d}Z"
            )

        self.change(start)
        self.event("start_gpu", generation["id"])

    def recover_gpu_worker(self, generation, release):
        row = self.read()["gpu"][generation["id"]]
        if not row["running"] or row.get("recovered") != row["started"]:
            self.start_gpu(generation)
            self.change(
                lambda data: data["gpu"][generation["id"]].update(
                    recovered=data["gpu"][generation["id"]]["started"]
                )
            )
        self.event("recover_gpu", generation["id"])

    def prepare_candidate(self, generation, release, *, recover=False):
        self.event("prepare", generation["id"], recover)
        if gpu(generation):
            active = (self.state.get("gpu_configuration") or {}).get("active")
            if active is None or active["generation"] != context(generation):
                self.publish_gpu_configuration(generation, enabled=True)
            self.gpu_worker_runtime(generation, "create")
            self.event("before_gpu_start", generation["id"])
            if recover:
                self.recover_gpu_worker(generation, release)
                self.stop(generation)
            elif not self.read()["gpu"][generation["id"]]["running"]:
                self.start_gpu(generation)
        self.service(generation, "web")
        self.service(generation, "worker")
        self.check(generation, self.state["operation"]["id"])
        if gpu(generation):
            self.change(lambda data: data["gpu_work"].update({generation["id"]: True}))
            self.event("check_gpu", generation["id"])
            self.change(lambda data: data["gpu_work"].update({generation["id"]: False}))

    def quiesce(self, generation):
        if generation:
            if gpu(generation):
                assert not self.read()["gpu_work"].get(generation["id"], False)
                self.gpu_worker_runtime(generation, "stop")
            self.stop(generation)
            self.database.freeze(generation["database"])
            self.event("quiesce", generation["id"])

    def retire_gpu_configuration(self, generation):
        self.database._require_frozen(generation["database"])
        assert not self.state["operation"]["exposure_target"]
        assert not any(row["running"] for row in self.read()["gpu"].values())
        old = self.state["gpu_configuration"]["active"]
        key = self.state["operation"]["id"] + ":" + generation["gpu_worker"]
        if old is not None:
            self.state.setdefault("gpu_retirements", {})[key] = {
                "schema": 1,
                "operation": self.state["operation"]["id"],
                "generation": generation,
                "binding_sha256": "a" * 64,
                "from": old,
                "phase": "retired",
            }
        self.state["gpu_configuration"] = {"schema": 1, "active": None, "transition": None}
        self.save()
        self.event("retire_gpu", generation["id"])

    def expose_gpu(self, generation, release, phase="active"):
        assert self.state["gpu_configuration"]["active"]["generation"] == context(generation)
        self.state["current"] = copy.deepcopy(generation)
        self.state["operation"]["exposure_target"] = generation["id"]
        self.save("exposing")
        self.control(
            generation, "judge_admission", "resume", "--token", self.state["operation"]["id"]
        )
        self.proxy.activate(self.port(generation))
        self.health(generation, external=True)
        self.save(phase)


@pytest.fixture
def model(tmp_path, monkeypatch):
    def inactive(flow, *, maintenance=True):
        flow.original()
        if maintenance:
            assert flow.host.read()["port"] is None
        state = flow.host.state.get("gpu_configuration")
        assert state is None or state == {"schema": 1, "active": None, "transition": None}
        assert not any(row["running"] for row in flow.host.read()["gpu"].values())
        flow.host.event("inactive")

    def previous_configuration(flow, generation):
        assert flow.host.state["gpu_configuration"]["active"]["generation"] == context(generation)

    def cleanup(flow, generation):
        flow.host.change(lambda data: data["gpu_work"].update({generation["id"]: False}))
        flow.host.event("cleanup_gpu", generation["id"])

    monkeypatch.setattr(GPURelease, "inactive", inactive)
    monkeypatch.setattr(GPURelease, "previous_configuration", previous_configuration)
    monkeypatch.setattr(
        GPURelease,
        "exposure_worker_stopped",
        lambda flow, gen: (
            not flow.host.read()["gpu"][gen["id"]]["running"]
            if gpu(gen)
            else not flow.host.ready(gen)
        ),
    )
    monkeypatch.setattr(
        GPURelease,
        "prior_archive",
        lambda flow, gen: flow.verify_archive(gen, flow.host.previous_release),
    )
    monkeypatch.setattr(GPURelease, "cleanup_rounds", cleanup)
    monkeypatch.setattr(
        module, "verify", lambda path, sha: json.loads((Path(path) / "manifest.json").read_text())
    )
    monkeypatch.setattr(module, "require_protocol", lambda generation, release: None)
    monkeypatch.setattr(module, "inspect_images", lambda manifest: None)
    monkeypatch.setattr(module, "initial_content_commands", lambda manifest: [("content_import",)])

    def create(previous="gpu", target="gpu"):
        host = Host(tmp_path / "site", previous)
        (host.target_release / "manifest.json").write_text(
            json.dumps(generation("222222222222", target)["manifest"])
        )
        return host

    return create


def apply(host, **kwargs):
    with host.locked():
        return host.apply_coordinated(host.target_release, "e" * 64, **kwargs)


def recover(host):
    with host.locked():
        return host.recover_coordinated()


def once(host, predicate):
    def hook(event):
        if predicate(event):
            host.hook = lambda event: None
            raise RuntimeError("modeled deployment failure")

    host.hook = hook


@pytest.mark.parametrize(
    "previous,target", [(None, "gpu"), ("cpu", "gpu"), ("gpu", "gpu"), ("gpu", "cpu")]
)
def test_installation_and_all_gpu_transitions_preserve_data_and_ordered_exposure(
    model, previous, target
):
    host = model(previous, target)
    apply(host, initial_content=previous is None)
    row = host.state["current"]
    assert host.state["operation"]["phase"] == "active"
    assert gpu(row) == (target == "gpu")
    values = host.read()
    if previous:
        assert values["rows"][row["database"]][0] == "original-user-write"
        assert "px_example_111111111111" in values["frozen"]
    else:
        assert any(
            event[:3] == ["control", row["id"], "content_import"] for event in values["events"]
        )
    if previous == "gpu" and target == "cpu":
        retirement = next(i for i, row in enumerate(values["events"]) if row[0] == "retire_gpu")
        exposure = next(i for i, row in enumerate(values["events"]) if row[0] == "activate")
        assert retirement < exposure
        assert host.state["gpu_configuration"]["active"] is None


@pytest.mark.parametrize(
    "previous,target", [(None, "gpu"), ("cpu", "gpu"), ("gpu", "gpu"), ("gpu", "cpu")]
)
def test_candidate_failure_recovers_original_work_before_returning_to_previous(
    model, previous, target
):
    host = model(previous, target)
    once(
        host,
        lambda event: (
            event[0] == "check_gpu"
            if target == "gpu"
            else event[0] == "check_cpu" and event[1] != "111111111111"
        ),
    )
    with pytest.raises(GPUReleaseError):
        apply(host)
    assert host.state["operation"]["phase"] == ("rolled_back" if previous else "failed_initial")
    assert (
        host.state["current"]["id"] == "111111111111" if previous else host.state["current"] is None
    )
    candidate = host.state["operation"]["candidate"]
    if target == "gpu":
        events = host.read()["events"]
        clean = next(
            i for i, row in enumerate(events) if row[:2] == ["cleanup_gpu", candidate["id"]]
        )
        frozen = next(i for i, row in enumerate(events) if row[:2] == ["quiesce", candidate["id"]])
        assert clean < frozen
    assert candidate["database"] in host.read()["rows"]


def test_unused_gpu_candidate_can_be_retired_without_starting_gpu_work(model):
    host = model("cpu", "gpu")
    once(host, lambda event: event[0] == "before_gpu_start")
    with pytest.raises(GPUReleaseError):
        apply(host)
    assert host.state["operation"]["phase"] == "rolled_back"
    assert not any(event[0] == "start_gpu" for event in host.read()["events"])


@pytest.mark.parametrize("target", ["cpu", "gpu"])
def test_post_exposure_failure_and_repeated_recovery_keep_current_database(model, target):
    host = model("gpu", target)
    once(host, lambda event: event[0] == "health")
    with pytest.raises(GPUReleaseError):
        apply(host)
    current = copy.deepcopy(host.state["current"])
    writes = host.read()["rows"][current["database"]]
    assert host.state["operation"]["phase"] == "needs_recovery"
    recover(host)
    assert host.state["current"] == current
    assert host.read()["rows"][current["database"]][: len(writes)] == writes
    recover(host)
    assert host.state["current"] == current
    record = host.state["gpu_releases"][host.state["operation"]["id"]]
    assert len(record["recoveries"]) == 2 and all(
        row["phase"] == "finished" for row in record["recoveries"]
    )


def test_drain_failure_preserves_live_route_until_explicit_recovery(model):
    host = model("gpu", "gpu")
    once(host, lambda event: event[:4] == ("control", "111111111111", "judge_admission", "drain"))
    with pytest.raises(GPUReleaseError):
        apply(host)
    assert host.state["operation"]["phase"] == "drain_failed" and host.read()["port"] == 21000
    assert not any(row[0] in {"maintenance", "copy", "quiesce"} for row in host.read()["events"])
    recover(host)
    assert host.state["operation"]["phase"] == "rolled_back"


def test_failed_cleanup_keeps_original_candidate_database_and_running_worker(model):
    host = model("cpu", "gpu")

    def fail(event):
        if event[0] in {"check_gpu", "cleanup_gpu"}:
            raise RuntimeError("modeled unfinished original task cleanup")

    host.hook = fail
    with pytest.raises(RuntimeError, match="cleanup"):
        apply(host)
    candidate = host.state["operation"]["candidate"]
    assert host.state["operation"]["phase"] == "recovering"
    assert host.read()["port"] is None
    assert host.read()["gpu"][candidate["id"]]["running"]
    assert candidate["database"] not in host.read()["frozen"]
    assert host.state["gpu_configuration"]["active"]["generation"] == context(candidate)
    assert not any(row[0] == "retire_gpu" for row in host.read()["events"])
    host.hook = lambda event: None
    recover(host)
    assert host.state["operation"]["phase"] == "rolled_back"


def test_unmarked_candidate_database_is_not_treated_as_missing_during_recovery(model):
    host = model("gpu", "gpu")

    def fail(event):
        if event == ("save", "copying"):
            candidate = host.state["operation"]["candidate"]
            host.change(lambda data: data["unowned"].append(candidate["database"]))
            raise RuntimeError("modeled crash before ownership marker")

    host.hook = fail
    with pytest.raises(DeploymentDatabaseError, match="unowned"):
        apply(host)
    before = host.read()
    host.hook = lambda event: None
    with pytest.raises(DeploymentDatabaseError, match="unowned"):
        recover(host)
    assert host.read()["rows"] == before["rows"]
    assert host.read()["unowned"] == before["unowned"]
    assert host.read()["port"] is None
    assert not any(row[0] in {"copy", "activate", "create"} for row in host.read()["events"])


@pytest.mark.parametrize("defect", ["current", "generation", "archive", "history", "key"])
def test_recovery_rejects_modified_original_state_before_service_or_database_changes(model, defect):
    host = model("gpu", "gpu")
    apply(host)
    with host.locked():
        record = host.state["gpu_releases"][host.state["operation"]["id"]]
        if defect == "current":
            host.state["current"] = record["previous"]
        elif defect == "generation":
            record["candidate"]["database"] = "px_example_deadbeef0000"
        elif defect == "archive":
            (host.target_release / "manifest.json").write_text("{}")
        elif defect == "history":
            record["recoveries"] = [{}]
        else:
            host.state["gpu_releases"] = {str(uuid.uuid4()): record}
        host.save()
    before = host.read()
    with pytest.raises(GPUReleaseError):
        recover(host)
    assert host.read() == before


def test_existing_catalog_is_never_reimported_by_upgrade(model):
    host = model("cpu", "gpu")
    before = host.read()
    with pytest.raises(GPUReleaseError, match="first installation"):
        apply(host, initial_content=True)
    assert host.read() == before
    apply(host)
    assert not any(
        row[:1] == ["control"] and "content_import" in row for row in host.read()["events"]
    )


def test_recovery_exposure_failure_can_recover_the_original_exited_worker(model):
    host = model("gpu", "gpu")
    apply(host)
    current = copy.deepcopy(host.state["current"])

    def fail(event):
        if event[0] == "activate":
            host.change(lambda data: data["gpu"][current["id"]].update(running=False))
            host.hook = lambda event: None
            raise RuntimeError("modeled worker exit during exposure")

    host.hook = fail
    with pytest.raises(RuntimeError, match="worker exit"):
        recover(host)
    writes = host.read()["rows"][current["database"]]
    assert not host.read()["gpu"][current["id"]]["running"]
    recover(host)
    assert host.read()["gpu"][current["id"]]["running"]
    assert host.state["current"] == current
    assert host.read()["rows"][current["database"]][: len(writes)] == writes


@pytest.mark.parametrize("target", ["cpu", "gpu"])
def test_lost_recovery_exposure_reply_does_not_restart_live_worker(model, target):
    host = model("gpu", target)
    apply(host)
    once(host, lambda event: event[0] == "activate")
    with pytest.raises(RuntimeError):
        recover(host)
    before = sum(row[0] == "start_gpu" for row in host.read()["events"])
    recover(host)
    assert sum(row[0] == "start_gpu" for row in host.read()["events"]) == before


def test_cpu_exposure_retry_refuses_an_unrelated_proxy_route(model):
    host = model("gpu", "cpu")
    apply(host)
    once(host, lambda event: event[0] == "activate")
    with pytest.raises(RuntimeError):
        recover(host)
    host.change(lambda data: data.update(port=29999))
    before = host.read()["rows"]
    with pytest.raises(GPUReleaseError, match="unrelated"):
        recover(host)
    assert host.read()["port"] == 29999 and host.read()["rows"] == before


@pytest.mark.parametrize(
    "point",
    [
        "freeze_source",
        "retire_source",
        "drain_target",
        "prepare_target",
        "expose_target",
        "finished",
    ],
)
def test_actual_cpu_sigkill_resumes_recovery_without_restarting_retired_candidate(model, point):
    host = model("cpu", "gpu")

    def fail(event):
        if event[0] in {"check_gpu", "recover_gpu"}:
            raise RuntimeError("modeled original recovery interrupted")

    host.hook = fail
    with pytest.raises(RuntimeError):
        apply(host)
    pid = os.fork()
    if pid == 0:

        def hook(event):
            if event[0] == "save":
                record = host.state["gpu_releases"][host.state["operation"]["id"]]
                if record["recoveries"][-1]["phase"] == point:
                    os.kill(os.getpid(), signal.SIGKILL)

        try:
            host.hook = hook
            recover(host)
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 25
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned recovery process missed its kill point")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        before = host.read()
        starts = sum(row[0] == "start_gpu" for row in before["events"])
        host.hook = lambda event: None
        recover(host)
        assert host.state["operation"]["phase"] == "rolled_back"
        assert host.state["current"]["id"] == "111111111111"
        assert sum(row[0] == "start_gpu" for row in host.read()["events"]) == starts
        assert set(host.read()["rows"]) == set(before["rows"])
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)


@pytest.mark.parametrize(
    "point", ["copying", "migrating", "preparing", "exposing", "after-exposure"]
)
def test_actual_cpu_sigkill_recovers_recorded_direction_without_copying_user_writes_back(
    model, point
):
    host = model("gpu", "gpu")
    pid = os.fork()
    if pid == 0:

        def hook(event):
            if event == ("save", point) or point == "after-exposure" and event[0] == "activate":
                os.kill(os.getpid(), signal.SIGKILL)

        try:
            host.hook = hook
            apply(host)
        except BaseException:
            os._exit(91)
        os._exit(92)
    try:
        deadline = time.monotonic() + 25
        while True:
            found, status = os.waitpid(pid, os.WNOHANG)
            if found:
                pid = None
                break
            if time.monotonic() >= deadline:
                pytest.fail("Owned whole-release process missed its kill point")
            time.sleep(0.02)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        before = json.loads(host.path.read_text())
        exposed = before["operation"]["exposure_target"]
        target = before["current"] if exposed else before["operation"]["previous"]
        writes = host.read()["rows"][target["database"]]
        copied = sum(row[0] == "copy" for row in host.read()["events"])
        recover(host)
        assert host.state["current"] == target
        assert host.read()["rows"][target["database"]][: len(writes)] == writes
        assert sum(row[0] == "copy" for row in host.read()["events"]) == copied
    finally:
        if pid is not None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
