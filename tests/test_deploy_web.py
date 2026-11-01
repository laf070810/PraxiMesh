"""Web replacement crash/ownership checks; real Docker/HTTP is checked separately."""

import copy
import json
from types import SimpleNamespace

import pytest

from scripts import deploy, deploy_web

IMAGE = "sha256:" + "a" * 64


class Crash(BaseException):
    pass


class Host(deploy.Deployment):
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "deployment.json"
        self.config = {"networks": {"frontend": "owned-ingress"}}
        self.state = {"site_id": "owned-site", "operation": {"phase": "recovering_current"}}
        self.proxy = SimpleNamespace(probe=lambda: {"port": None})
        self.generation = {
            "id": "original-generation",
            "database": "keep-this-database",
            "web": "owned-web",
            "worker": "owned-worker",
            "manifest": {"images": {"web": IMAGE, "worker": "sha256:" + "b" * 64}},
        }
        self.image_config = {
            "Cmd": ["gunicorn", "praximesh.wsgi:application"],
            "User": "65532:65532",
            "WorkingDir": "/app",
            "Env": ["PATH=/bin"],
        }
        self.next_id = 1
        self.calls = []
        self.hook = lambda event: None
        self.container = self.container_info()
        self.state["current"] = copy.deepcopy(self.generation)
        self.save()

    def container_info(self, token=None):
        identifier = f"{self.next_id:064x}"
        self.next_id += 1
        labels = {
            "org.praximesh.site": self.state["site_id"],
            "org.praximesh.generation": self.generation["id"],
            "org.praximesh.role": "web",
        }
        if token:
            labels[deploy_web.LABEL] = token
        return {
            "Id": identifier,
            "Name": "/owned-web",
            "Image": IMAGE,
            "Config": dict(
                copy.deepcopy(self.image_config),
                Hostname="owned-web",
                Labels=labels,
                Env=["PATH=/bin", "DATABASE_URL=private-original-database"],
            ),
            "HostConfig": {
                "ReadonlyRootfs": True,
                "Init": True,
                "Privileged": False,
                "Memory": 512 * 1024 * 1024,
                "PidsLimit": 64,
                "NetworkMode": "owned-ingress",
                "CapDrop": ["ALL"],
                "SecurityOpt": ["no-new-privileges:true"],
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=128m,uid=65532,gid=65532,mode=700"},
                "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
                "PortBindings": {"8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": ""}]},
                "IpcMode": "private",
            },
            "Mounts": [],
            "State": {"Running": False},
            "NetworkSettings": {"Networks": {"owned-ingress": {}}, "Ports": {}},
        }

    def save(self, phase=None):
        super().save(phase)
        value = self.state.get("web_replacements", {}).get(self.generation["id"])
        self.hook("save-" + (value["phase"] if value else "initial"))

    def environment(self, generation, role):
        assert generation["database"] == "keep-this-database" and role == "web"
        return {"DATABASE_URL": "private-original-database"}

    def inspect(self, name):
        if name == self.generation["worker"]:
            return None
        assert name == self.generation["web"]
        return copy.deepcopy(self.container)

    def run(self, *args, **options):
        if args[:2] == ("image", "inspect"):
            return json.dumps([{"Id": IMAGE, "Config": self.image_config}]).encode()
        self.calls.append(args)
        self.hook("before-" + args[0])
        durable = json.loads(self.path.read_text())["web_replacements"][self.generation["id"]]
        if args[0] == "rm":
            assert args == ("rm", self.container["Id"])
            assert durable["old_id"] == self.container["Id"]
            assert not self.container["State"]["Running"]
            self.container = None
        elif args[0] == "create":
            assert self.container is None and durable["phase"] == "removed"
            assert "--publish" in args and "127.0.0.1::8000" in args
            assert deploy_web.LABEL + "=" + durable["token"] in args
            self.container = self.container_info(durable["token"])
            self.hook("after-create")
            return self.container["Id"].encode()
        elif args[0] == "start":
            assert args == ("start", self.container["Id"])
            assert durable["new_id"] == self.container["Id"] and durable["phase"] == "created"
            self.container["State"]["Running"] = True
            self.container["NetworkSettings"]["Ports"] = {
                "8000/tcp": [{"HostIp": "127.0.0.1", "HostPort": "23001"}]
            }
        elif args[0] == "stop":
            assert args[-1] == self.container["Id"]
            self.container["State"]["Running"] = False
        else:
            raise AssertionError(args[0])
        self.hook("after-" + args[0])
        return b""


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


def test_replacement_preserves_generation_database_and_pins_new_identity(host):
    original = copy.deepcopy(host.generation)
    old_id = host.container["Id"]
    host.replace_web(host.generation)
    value = deploy_web.record(host, host.generation)
    assert value["old_id"] == old_id and value["new_id"] != old_id
    assert value["phase"] == "started" and host.port(host.generation) == 23001
    assert host.state["current"] == host.generation == original
    assert host.state["operation"]["phase"] == "recovering_current"


@pytest.mark.parametrize(
    "point",
    [
        "save-prepared",
        "before-rm",
        "after-rm",
        "save-removed",
        "before-create",
        "after-create",
        "save-created",
        "before-start",
        "after-start",
        "save-started",
    ],
)
def test_each_interruption_reloads_durable_intent_and_recovers_same_database(host, point):
    def crash(event):
        if event == point:
            raise Crash()

    host.hook = crash
    with pytest.raises(Crash):
        host.replace_web(host.generation)
    host.hook = lambda event: None
    host.state = json.loads(host.path.read_text())
    host.stop(host.generation)
    host.replace_web(host.generation)
    assert host.port(host.generation) == 23001
    assert host.state["current"]["database"] == "keep-this-database"
    assert deploy_web.record(host, host.generation)["phase"] == "started"


@pytest.mark.parametrize(
    "path,value",
    [
        (("Image",), "sha256:" + "c" * 64),
        (("Name",), "/somebody-else"),
        (("Config", "Labels", "org.praximesh.site"), "another-site"),
        (("Config", "Labels", "org.praximesh.generation"), "another-generation"),
        (("Config", "Cmd"), ["unreviewed-command"]),
        (("Config", "Env"), ["PATH=/bin", "DATABASE_URL=other-database"]),
        (("Config", "Env"), ["PATH=/bin", "PATH=/extra", "DATABASE_URL=private-original-database"]),
        (("Config", "Volumes"), {"/data": {}}),
        (("Mounts",), [{"Type": "volume", "Destination": "/data"}]),
        (("HostConfig", "Binds"), ["/private:/secrets"]),
        (("HostConfig", "Devices"), [{"PathOnHost": "/dev/something"}]),
        (("HostConfig", "Privileged"), True),
        (("HostConfig", "ReadonlyRootfs"), False),
        (("HostConfig", "CapAdd"), ["SYS_ADMIN"]),
        (("HostConfig", "SecurityOpt"), ["seccomp=unconfined"]),
        (("HostConfig", "PidMode"), "host"),
        (("HostConfig", "Memory"), 0),
        (("HostConfig", "NetworkMode"), "other-network"),
        (("NetworkSettings", "Networks"), {"owned-ingress": {}, "other-network": {}}),
        (("HostConfig", "PortBindings"), {"8000/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8000"}]}),
    ],
)
def test_unexpected_or_persistent_container_is_never_adopted_or_removed(host, path, value):
    parent = host.container
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    with pytest.raises(deploy.DeploymentError):
        host.replace_web(host.generation)
    assert host.calls == [] and "web_replacements" not in host.state


@pytest.mark.parametrize("state", ["active-proxy", "running-web"])
def test_only_maintenance_and_stopped_web_permit_replacement(host, state):
    if state == "active-proxy":
        host.proxy = SimpleNamespace(probe=lambda: {"port": 23001})
    else:
        host.container["State"]["Running"] = True
    with pytest.raises(deploy.DeploymentError):
        host.replace_web(host.generation)
    assert not host.calls


def test_start_failure_is_not_retried_on_same_container(host):
    def fail(event):
        if event == "before-start":
            raise deploy.DeploymentError("private network failure")

    host.hook = fail
    with pytest.raises(deploy.DeploymentError, match="private network failure"):
        host.replace_web(host.generation)
    failed_id = host.container["Id"]
    host.hook = lambda event: None
    host.state = json.loads(host.path.read_text())
    host.replace_web(host.generation)
    assert host.container["Id"] != failed_id
    assert sum(call == ("start", failed_id) for call in host.calls) == 1


def test_running_without_actual_mapping_does_not_finish_replacement(host):
    def corrupt(event):
        if event == "after-start":
            host.container["NetworkSettings"]["Ports"] = {}

    host.hook = corrupt
    with pytest.raises(deploy.DeploymentError, match="loopback-only"):
        host.replace_web(host.generation)
    assert deploy_web.record(host, host.generation)["phase"] == "created"


def test_same_name_and_labels_cannot_replace_a_saved_id_for_stop_start_or_remove(host):
    host.replace_web(host.generation)
    host.calls.clear()
    host.container["Id"] = "e" * 64
    for action in (
        lambda: host.stop(host.generation),
        lambda: host.service(host.generation, "web"),
        lambda: host.replace_web(host.generation),
    ):
        with pytest.raises(deploy.DeploymentError, match="ID differs"):
            action()
    assert not host.calls


def test_config_change_after_saved_intent_is_not_removed(host):
    def change(event):
        if event == "save-prepared":
            host.container["HostConfig"]["PidsLimit"] = 65

    host.hook = change
    with pytest.raises(deploy.DeploymentError, match="changed before removal"):
        host.replace_web(host.generation)
    assert not host.calls


@pytest.mark.parametrize(
    "field,value",
    [
        ("database", "different-database"),
        ("image", "sha256:" + "c" * 64),
        ("name", "different-name"),
        ("new_id", "short"),
        ("token", "bad-token"),
        ("phase", "made-up-phase"),
        ("format", True),
    ],
)
def test_invalid_or_wrong_generation_journal_cannot_control_container(host, field, value):
    host.replace_web(host.generation)
    host.calls.clear()
    host.state["web_replacements"][host.generation["id"]][field] = value
    with pytest.raises(deploy.DeploymentError, match="journal"):
        host.replace_web(host.generation)
    assert not host.calls


def test_missing_original_is_created_under_durable_intent(host):
    host.container = None
    host.replace_web(host.generation)
    value = deploy_web.record(host, host.generation)
    assert value["old_id"] is None and value["new_id"] == host.container["Id"]
    assert host.port(host.generation) == 23001
