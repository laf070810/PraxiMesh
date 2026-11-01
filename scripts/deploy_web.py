"""Journalled replacement of stopped, stateless deployment Web containers.

Only the coordinator calls this, under its file/DB locks and maintenance route.
It does not stop workers, change databases, or decide whether to expose a site.
"""

import hashlib
import json
import re
import uuid

LABEL = "org.praximesh.web-intent"
ID = re.compile(r"[a-f0-9]{64}")
TOKEN = re.compile(r"[a-f0-9]{32}")


class WebRecoveryError(RuntimeError):
    pass


def fingerprint(info):
    return hashlib.sha256(
        json.dumps(
            {key: info[key] for key in ("Id", "Name", "Image", "Config", "HostConfig", "Mounts")},
            sort_keys=True,
        ).encode()
    ).hexdigest()


def identity(host, generation):
    return {
        "generation": generation["id"],
        "database": generation["database"],
        "name": generation["web"],
        "image": generation["manifest"]["images"]["web"],
    }


def record(host, generation):
    records = host.state.get("web_replacements", {})
    if not isinstance(records, dict):
        raise WebRecoveryError("Invalid Web replacement journal")
    value = records.get(generation["id"])
    if value is None:
        return None
    try:
        valid = (
            set(value)
            == set(identity(host, generation))
            | {"format", "token", "old_id", "old_digest", "new_id", "phase"}
            and type(value["format"]) is int
            and value["format"] == 1
            and all(value[key] == expected for key, expected in identity(host, generation).items())
            and TOKEN.fullmatch(value["token"])
            and value["phase"] in {"prepared", "removed", "created", "started"}
            and (
                (value["old_id"] is None and value["old_digest"] is None)
                or (ID.fullmatch(value["old_id"]) and ID.fullmatch(value["old_digest"]))
            )
            and (value["new_id"] is None or ID.fullmatch(value["new_id"]))
            and (value["phase"] not in {"created", "started"} or value["new_id"] is not None)
        )
    except (TypeError, KeyError):
        valid = False
    if not valid:
        raise WebRecoveryError("Web replacement journal does not match this generation")
    return value


def check_reference(host, generation, info):
    """Never stop/start a same-name substitution after an ID has been recorded."""
    value = record(host, generation)
    if value is None or info is None:
        return
    old = value["phase"] == "prepared" and info["Id"] == value["old_id"]
    new = (
        info["Config"].get("Labels", {}).get(LABEL) == value["token"]
        and (value["new_id"] is None or info["Id"] == value["new_id"])
        and info["Id"] != value["old_id"]
    )
    if not old and not new:
        raise WebRecoveryError("Web container ID differs from the replacement journal")
    if old and fingerprint(info) != value["old_digest"]:
        raise WebRecoveryError("Original Web container configuration changed")


def environment_values(values):
    result = {}
    for item in values or []:
        key, separator, value = item.partition("=")
        if not separator or not key or key in result:
            raise WebRecoveryError("Ambiguous Web container environment")
        result[key] = value
    return result


def validate(host, generation, info, environment):
    """Legacy containers must prove the stateless contract before ID adoption."""
    image = generation["manifest"]["images"]["web"]
    image_info = json.loads(host.run("image", "inspect", image))[0]
    expected = image_info["Config"]
    config, runtime = info["Config"], info["HostConfig"]
    labels = config.get("Labels") or {}
    network = host.config["networks"]["frontend"]
    ports = runtime.get("PortBindings") or {}
    bindings = ports.get("8000/tcp") or []
    valid_port = (
        set(ports) == {"8000/tcp"}
        and len(bindings) == 1
        and bindings[0].get("HostIp") == "127.0.0.1"
        and (
            bindings[0].get("HostPort") == ""
            or (
                str(bindings[0].get("HostPort", "")).isdigit()
                and 0 < int(bindings[0]["HostPort"]) < 65536
            )
        )
    )
    required = {
        "ReadonlyRootfs": True,
        "Init": True,
        "Privileged": False,
        "Memory": 512 * 1024 * 1024,
        "PidsLimit": 64,
        "NetworkMode": network,
        "CapDrop": ["ALL"],
        "SecurityOpt": ["no-new-privileges:true"],
        "Tmpfs": {"/tmp": "rw,noexec,nosuid,size=128m,uid=65532,gid=65532,mode=700"},
        "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
    }
    safe = (
        ID.fullmatch(info["Id"])
        and info["Name"] == "/" + generation["web"]
        and info["Image"] == image_info["Id"] == image
        and labels.get("org.praximesh.site") == host.state["site_id"]
        and labels.get("org.praximesh.generation") == generation["id"]
        and labels.get("org.praximesh.role") == "web"
        and all(
            config.get(key) == expected.get(key)
            for key in ("Entrypoint", "Cmd", "User", "WorkingDir", "Healthcheck", "StopSignal")
        )
        and config.get("Hostname") == generation["web"]
        and not config.get("Volumes")
        and not info["Mounts"]
        and environment_values(config.get("Env"))
        == (environment_values(expected.get("Env")) | environment)
        and all(runtime.get(key) == value for key, value in required.items())
        and not any(
            runtime.get(key)
            for key in (
                "Binds",
                "Mounts",
                "VolumesFrom",
                "Devices",
                "DeviceRequests",
                "CapAdd",
                "DeviceCgroupRules",
                "PidMode",
                "UTSMode",
                "UsernsMode",
                "Links",
            )
        )
        and runtime.get("IpcMode") == "private"
        and set(info["NetworkSettings"].get("Networks", {})) <= {network}
        and valid_port
    )
    if not safe:
        raise WebRecoveryError("Web container is not the expected stateless deployment service")
    check_reference(host, generation, info)


def replace(host, generation):
    if host.proxy.probe()["port"] is not None:
        raise WebRecoveryError("Web replacement requires the actual maintenance route")
    value = record(host, generation)
    environment = host.environment(generation, "web")
    info = host.inspect(generation["web"])
    if info:
        validate(host, generation, info, environment)
        if info["State"]["Running"]:
            raise WebRecoveryError("Stop the owned Web before replacing it")
    # A pending original can be removed using the saved intent. A new object
    # found after interruption gets a fresh intent, never a blind second start.
    if value is None or (info and info["Id"] != value["old_id"]) or value["new_id"]:
        value = {
            **identity(host, generation),
            "format": 1,
            "token": uuid.uuid4().hex,
            "old_id": info["Id"] if info else None,
            "old_digest": fingerprint(info) if info else None,
            "new_id": None,
            "phase": "prepared",
        }
        host.state.setdefault("web_replacements", {})[generation["id"]] = value
        host.save()
    if info:
        # Recheck after the durable intent; operate on the full ID, not the name.
        current = host.inspect(generation["web"])
        if (
            not current
            or current["Id"] != value["old_id"]
            or fingerprint(current) != value["old_digest"]
            or current["State"]["Running"]
        ):
            raise WebRecoveryError("Web identity or stopped state changed before removal")
        host.run("rm", current["Id"], diagnostic="container-remove")
    value["phase"] = "removed"
    host.save()
    args = host.runtime(generation, "web", generation["web"]) + [
        "--restart=unless-stopped",
        "--publish",
        "127.0.0.1::8000",
        "--label",
        LABEL + "=" + value["token"],
    ]
    for key in environment:
        args += ["--env", key]
    identifier = (
        host.run(
            "create",
            *args,
            value["image"],
            environment=environment,
            diagnostic="container-create",
        )
        .decode()
        .strip()
    )
    if not ID.fullmatch(identifier):
        raise WebRecoveryError("Docker did not return a complete Web container ID")
    info = host.inspect(generation["web"])
    if not info or info["Id"] != identifier:
        raise WebRecoveryError("Created Web container identity changed")
    validate(host, generation, info, environment)
    value["new_id"], value["phase"] = identifier, "created"
    host.save()
    host.run("start", identifier, diagnostic="container-start")
    info = host.inspect(generation["web"])
    if not info or info["Id"] != identifier:
        raise WebRecoveryError("Started Web container identity changed")
    validate(host, generation, info, environment)
    host.port(generation)  # Actual published mapping, never HostConfig intent.
    value["phase"] = "started"
    host.save()
