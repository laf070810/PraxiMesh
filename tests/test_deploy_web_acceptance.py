"""Protect the acceptance fixture's cleanup boundary without a Docker daemon."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from scripts import deploy_web_acceptance as acceptance

BASE = "sha256:" + "a" * 64
DERIVED = "sha256:" + "b" * 64


def test_failed_fixture_preparation_preserves_untagged_base_image(monkeypatch, tmp_path):
    state = {"base": True, "container": None, "network": None, "derived": False}
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            if args[2] == BASE:
                assert state["base"], "cleanup pruned the untagged original image"
                return json.dumps([{"Id": BASE, "Config": {"User": "65532:65532"}}]).encode()
            assert state["derived"] and args[2] == state["tag"]
            return json.dumps(
                [
                    {
                        "Id": DERIVED,
                        "Config": {"Labels": {"org.praximesh.web-acceptance": state["owner"]}},
                    }
                ]
            ).encode()
        if args[0] == "create":
            state["name"] = args[args.index("--name") + 1]
            label = args[args.index("--label") + 1]
            state["owner"] = label.split("=", 1)[1]
            state["container"] = "c" * 64
            return state["container"].encode()
        if args[0] == "commit":
            state["derived"], state["tag"] = True, args[-1]
            return DERIVED.encode()
        if args[:2] == ("network", "create"):
            state["network"] = "owned-network"
            return b"owned-network"
        if args[:2] == ("container", "ls"):
            return (state["container"] or "").encode()
        if args[:2] == ("container", "inspect"):
            return json.dumps(
                [
                    {
                        "Id": state["container"],
                        "Name": "/" + state["name"],
                        "Image": BASE,
                        "Config": {"Labels": {"org.praximesh.web-acceptance": state["owner"]}},
                    }
                ]
            ).encode()
        if args[0] == "rm":
            assert args == ("rm", "--force", state["container"])
            state["container"] = None
            return b""
        if args[:2] == ("network", "ls"):
            return (state["network"] or "").encode()
        if args[:2] == ("network", "rm"):
            state["network"] = None
            return b""
        if args[:2] == ("image", "ls"):
            return DERIVED.encode() if state["derived"] else b""
        if args[:2] == ("image", "rm"):
            assert args[-1] == state["tag"]
            state["derived"] = False
            # Model Docker's default removal of untagged parent images.
            if "--no-prune" not in args:
                state["base"] = False
            return b""
        raise AssertionError(args)

    def fail_before_start(directory):
        raise RuntimeError("fixture initialization failed before any service ran")

    monkeypatch.setattr(acceptance, "docker", docker)
    monkeypatch.setattr(acceptance, "load", fail_before_start)
    result = {"passed": False, "checks": []}
    with pytest.raises(RuntimeError, match="fixture initialization failed"):
        acceptance.run(SimpleNamespace(image=BASE), tmp_path, result)
    assert result["owned_resources_removed"] and result["base_image_preserved"]
    assert state["base"] and not state["derived"] and state["container"] is None
    assert not any(call[0] == "start" for call in calls)


def test_missing_base_image_does_not_create_or_delete_resources(monkeypatch, tmp_path):
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("image", "inspect"):
            raise subprocess.CalledProcessError(1, ["docker", *args], stderr=b"No such image")
        if args[:2] in {("container", "ls"), ("network", "ls"), ("image", "ls")}:
            return b""
        raise AssertionError("unexpected mutation")

    monkeypatch.setattr(acceptance, "docker", docker)
    result = {"passed": False, "checks": []}
    with pytest.raises(subprocess.CalledProcessError):
        acceptance.run(SimpleNamespace(image=BASE), tmp_path, result)
    assert not result["passed"] and result["owned_resources_removed"]
    assert "base_image_preserved" not in result
    assert sum(call[:2] == ("image", "inspect") for call in calls) == 1
