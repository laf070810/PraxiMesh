"""Explicit daemon model for build cache cleanup; no Docker or GPU is used."""

import json
from types import SimpleNamespace

import pytest

from scripts.release_gpu import cleanup_build_tag

TAG = "px-gpu-release-original"
IMAGE = "sha256:" + "a" * 64


class Engine:
    def __init__(self, *, busy=False, other_tag=False):
        self.tags = [TAG + ":latest"] + (["retained:latest"] if other_tag else [])
        self.references = ["b" * 64] if busy else []
        self.identifier = IMAGE
        self.calls = []
        self.race = False
        self.failure = False

    def run(self, *args, check=True):
        self.calls.append(args)
        output, status = b"", 0
        if args[:2] == ("image", "ls"):
            assert args[2:] == ("--quiet", "--no-trunc", "--filter", "reference=" + TAG + ":latest")
            output = (self.identifier if TAG + ":latest" in self.tags else "").encode()
        elif args[:2] == ("image", "inspect"):
            assert args[2:] == (TAG + ":latest",)
            output = json.dumps([{"Id": self.identifier, "RepoTags": self.tags}]).encode()
        elif args[:2] == ("container", "ls"):
            assert args[2:] == ("--all", "--quiet", "--no-trunc", "--filter", "ancestor=" + IMAGE)
            output = "\n".join(self.references).encode()
        elif args[:2] == ("image", "rm"):
            assert args[2:] == ("--no-prune", TAG) and check is False
            if self.race:
                self.references = ["c" * 64]
            if self.failure or (self.references and self.tags == [TAG + ":latest"]):
                status = 1
            else:
                self.tags.remove(TAG + ":latest")
        else:
            pytest.fail("Unexpected build cleanup mutation: " + repr(args))
        return SimpleNamespace(stdout=output, returncode=status)


def test_reused_last_tag_and_original_container_reference_are_retained():
    engine = Engine(busy=True)
    assert cleanup_build_tag(engine, TAG, IMAGE)["state"] == "retained-in-use"
    assert engine.tags == [TAG + ":latest"] and engine.references == ["b" * 64]
    assert not any(call[:2] == ("image", "rm") for call in engine.calls)


@pytest.mark.parametrize("busy,other_tag", [(False, False), (True, True), (False, True)])
def test_only_own_tag_is_removed_and_parent_pruning_stays_disabled(busy, other_tag):
    engine = Engine(busy=busy, other_tag=other_tag)
    references = list(engine.references)
    assert cleanup_build_tag(engine, TAG, IMAGE)["state"] == "removed"
    assert engine.tags == (["retained:latest"] if other_tag else [])
    assert engine.references == references
    assert ("image", "rm", "--no-prune", TAG) in engine.calls


def test_new_container_between_observation_and_removal_preserves_original_image():
    engine = Engine()
    engine.race = True
    assert cleanup_build_tag(engine, TAG, IMAGE)["state"] == "retained-in-use"
    assert engine.tags == [TAG + ":latest"] and engine.references == ["c" * 64]


def test_unrelated_daemon_failure_is_not_reported_as_cleanup_success():
    engine = Engine()
    engine.failure = True
    with pytest.raises(ValueError, match="could not be safely removed"):
        cleanup_build_tag(engine, TAG, IMAGE)
    assert engine.tags == [TAG + ":latest"]


def test_same_tag_replacement_is_not_deleted():
    engine = Engine()
    engine.identifier = "sha256:" + "d" * 64
    with pytest.raises(ValueError, match="original image"):
        cleanup_build_tag(engine, TAG, IMAGE)
    assert not any(call[:2] == ("image", "rm") for call in engine.calls)


@pytest.mark.parametrize(
    "references", [["partial"], ["c" * 64, "c" * 64], [str(i) for i in range(1025)]]
)
def test_incomplete_or_unbounded_reference_observation_refuses_cleanup(references):
    engine = Engine()
    engine.references = references
    with pytest.raises(ValueError, match="bounded"):
        cleanup_build_tag(engine, TAG, IMAGE)
    assert not any(call[:2] == ("image", "rm") for call in engine.calls)


def test_successful_empty_inventory_confirms_absent_original_tag():
    engine = Engine()
    engine.tags = []
    assert cleanup_build_tag(engine, TAG, IMAGE)["state"] == "absent"
    assert len(engine.calls) == 1
