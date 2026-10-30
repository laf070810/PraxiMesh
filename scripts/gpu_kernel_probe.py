"""Pinned artifacts and controller checks for reviewed CUDA probe fixtures only."""

import hashlib
import json
import re
import shutil
import tarfile
from pathlib import Path

from judge.gpu_contract import case_request, compile_request, output_matches, reference_values
from judge.gpu_probes import correctness_fixtures, fault_fixtures

IMAGE = "docker.io/library/python:3.11.15-slim-bookworm@sha256:77923445c077d8eb971b14b2b114a1d9cd4a87edb4c75654820ca4832ee8cb15"
NVRTC_ARCHIVE_SHA256 = "86077f16674ccea3e621ade9678887a37aca8f7500091aee4cabb1915e0c527b"
NVRTC_DIRECTORY = "cuda_nvrtc-linux-x86_64-13.1.115-archive"
LIBRARIES = {
    "libnvrtc.so.13.1.115": "f0ca45d23836e8028d51a60db9d825540e9e6cfe7491507c804ad8b9beb368a7",
    "libnvrtc-builtins.so.13.1.115": "b38e97128bba09c2bfd7641179f5f5211ab6a8fc18bb497d89ea02cb6806a5b0",
}
ALIASES = {
    "libnvrtc.so": "libnvrtc.so.13",
    "libnvrtc.so.13": "libnvrtc.so.13.1.115",
    "libnvrtc-builtins.so": "libnvrtc-builtins.so.13.1",
    "libnvrtc-builtins.so.13.1": "libnvrtc-builtins.so.13.1.115",
}
HARNESS = ("__init__.py", "gpu_contract.py", "gpu_runtime.py", "results.py")
COMPARISON = {"kind": "float", "absolute": 1e-5, "relative": 1e-5}


def verify_image_archive(archive):
    """Validate the complete pinned OCI graph without extracting image layers."""
    if archive.stat().st_size > 128 * 1024 * 1024:
        raise ValueError("GPU image archive exceeds its size limit")
    with tarfile.open(archive, "r:") as bundle:
        files, names = {}, set()
        for member in bundle:
            name = member.name.rstrip("/")
            if len(names) >= 12:
                raise ValueError("Too many OCI archive members")
            if name in names:
                raise ValueError("Duplicate OCI archive member")
            names.add(name)
            if member.isdir() and name in {"blobs", "blobs/sha256"}:
                continue
            if not member.isfile() or not (
                name in {"index.json", "oci-layout"}
                or re.fullmatch(r"blobs/sha256/[0-9a-f]{64}", name)
            ):
                raise ValueError("Unexpected OCI archive member")
            files[name] = member
        if sum(member.size for member in files.values()) > 128 * 1024 * 1024:
            raise ValueError("GPU image archive contents exceed their size limit")

        def read_json(name):
            if name not in files or files[name].size > 64 * 1024:
                raise ValueError("Missing or oversized OCI metadata")
            return json.load(bundle.extractfile(files[name]))

        if read_json("oci-layout") != {"imageLayoutVersion": "1.0.0"}:
            raise ValueError("Unexpected OCI layout version")
        index = read_json("index.json")
        pinned = IMAGE.split("@", 1)[1]
        if (
            index.get("schemaVersion") != 2
            or type(index.get("manifests")) is not list
            or len(index["manifests"]) != 1
            or index["manifests"][0].get("digest") != pinned
        ):
            raise ValueError("OCI archive must contain the pinned GPU image manifest")
        manifest_name = "blobs/" + pinned.replace(":", "/")
        manifest = read_json(manifest_name)
        descriptors = [index["manifests"][0], manifest["config"], *manifest["layers"]]
        referenced = {"index.json", "oci-layout"}
        for descriptor in descriptors:
            digest = descriptor["digest"]
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise ValueError("Unsupported OCI blob digest")
            name = "blobs/" + digest.replace(":", "/")
            referenced.add(name)
            if name not in files or files[name].size != descriptor["size"]:
                raise ValueError("Missing or resized OCI blob")
            with bundle.extractfile(files[name]) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != digest.split(":", 1)[1]:
                raise ValueError("OCI blob digest mismatch")
        if set(files) != referenced:
            raise ValueError("Unreferenced OCI archive content")
        image_id = manifest["config"]["digest"]
        config = read_json("blobs/" + image_id.replace(":", "/"))
        if config.get("os") != "linux" or config.get("architecture") != "amd64":
            raise ValueError("GPU probe requires the pinned Linux amd64 image")
    return {
        "image_archive_sha256": sha256(archive),
        "image_manifest": pinned,
        "image_id": image_id,
    }


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prepare_artifacts(base, archive, root):
    """Verify before extraction; mount neither fixtures nor expected values."""
    if sha256(archive) != NVRTC_ARCHIVE_SHA256:
        raise ValueError("NVRTC archive must match the pinned 13.1.115 SHA-256")
    extracted = base / "nvrtc-extracted"
    extracted.mkdir(mode=0o700)
    try:
        with tarfile.open(archive) as bundle:
            bundle.extractall(extracted, filter="data")
        source = extracted / NVRTC_DIRECTORY
        destination = base / "nvrtc"
        destination.mkdir(mode=0o755)
        for filename, expected in LIBRARIES.items():
            path = source / "lib" / filename
            if sha256(path) != expected:
                raise ValueError("NVRTC library digest mismatch")
            shutil.copyfile(path, destination / filename)
        for filename, target in ALIASES.items():
            (destination / filename).symlink_to(target)
        shutil.copyfile(source / "LICENSE", destination / "LICENSE")
    finally:
        shutil.rmtree(extracted)
    harness = base / "harness/judge"
    harness.mkdir(mode=0o755, parents=True)
    for filename in HARNESS:
        shutil.copyfile(root / "judge" / filename, harness / filename)
    return {
        "nvrtc_archive_sha256": NVRTC_ARCHIVE_SHA256,
        "nvrtc_libraries": dict(LIBRARIES),
        "harness_sha256": {filename: sha256(harness / filename) for filename in HARNESS},
        "fixture_sha256": sha256(root / "judge/gpu_probes.py"),
        "probe_sha256": sha256(Path(__file__)),
    }


def container_options(base, *, diagnostics=True):
    options = [
        "--volume=" + str(base / "harness") + ":/opt/praximesh:ro",
        "--volume=" + str(base / "nvrtc") + ":/opt/nvrtc:ro",
        "--workdir=/opt/praximesh",
        "--env=PYTHONDONTWRITEBYTECODE=1",
        "--env=LD_LIBRARY_PATH=/opt/nvrtc:/usr/lib64",
    ]
    if diagnostics:
        # Only reviewed synthetic fixtures run here. Diagnostics remain in the
        # private probe directory, never a learner response or formal evidence.
        options.append("--env=PRAXIMESH_GPU_DIAGNOSTICS=1")
    return options


def fixture_request(fixture):
    requests = [compile_request(fixture["source"], fixture["operation"])]
    requests += [
        case_request(fixture["operation"], {"inputs": inputs}) for inputs in fixture["inputs"]
    ]
    return "".join(json.dumps(request, allow_nan=False) + "\n" for request in requests)


def valid_compiled_frame(frame):
    return (
        type(frame) is dict
        and set(frame) == {"event", "architecture"}
        and frame["event"] == "compiled"
        and type(frame["architecture"]) is list
        and len(frame["architecture"]) == 2
        and all(type(value) is int and value >= 0 for value in frame["architecture"])
    )


def verify_output(fixture, output):
    frames = [json.loads(line) for line in output.splitlines()]
    if fixture["exit_code"]:
        if fixture.get("compiled"):
            if len(frames) != 1 or not valid_compiled_frame(frames[0]):
                raise ValueError("Runtime fault must follow compilation and emit no result")
        elif frames:
            raise ValueError("Failed compilation or entrypoint must not emit result frames")
        return
    if len(frames) != len(fixture["inputs"]) + 1 or not valid_compiled_frame(frames[0]):
        raise ValueError("Invalid CUDA compilation/result frames")
    correct = []
    for inputs, frame in zip(fixture["inputs"], frames[1:], strict=True):
        if type(frame) is not dict or set(frame) != {"values"}:
            raise ValueError("Only output values are accepted from the GPU harness")
        expected = reference_values(fixture["operation"], inputs)
        correct.append(output_matches(frame["values"], expected, COMPARISON))
    expected_results = all(correct) if fixture["correct"] else not any(correct)
    if not correct or not expected_results:
        raise ValueError("Reference or incorrect fixture did not match controller expectation")


def verify_fixtures(run, report, save, *, faults=False):
    report["kernel_probes"] = []
    for fixture in fault_fixtures() if faults else correctness_fixtures():
        output = run(
            fixture["name"],
            "python",
            ["-B", "-m", "judge.gpu_runtime"],
            stdin=fixture_request(fixture),
            expected_exit=fixture["exit_code"],
            timeout=fixture.get("timeout", 60),
            expected_timeout=fixture.get("expected_timeout", False),
        )
        verify_output(fixture, output)
        report["kernel_probes"].append({"name": fixture["name"], "passed": True})
        save()
        print(json.dumps({"phase": "kernel_probe_passed", "name": fixture["name"]}), flush=True)
