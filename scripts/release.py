"""Build and verify a private, immutable Web/WASI-worker release.

This prepares and imports deployment artifacts. It never starts a service,
migrates a database, enables evaluation or publishes to a registry.
"""

import argparse
import gzip
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from judge.sandbox import policy_digest  # noqa: E402
from judge.wasi import WasiSandbox  # noqa: E402
from judge.wasi_policy import TARGET  # noqa: E402

SOURCE_DIRS = {
    "challenges",
    "core",
    "learning",
    "community",
    "practice_api",
    "showcase",
    "localization",
    "projects",
    "organizations",
    "coaching",
    "locale",
    "judge",
    "praximesh",
    "content",
    "static",
    "templates",
    "scripts",
    "deploy",
    "docs",
    "tests",
    "vendor",
    ".github",
}
SOURCE_FILES = {
    "PRD.md",
    "README.md",
    "LICENSES.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CHANGELOG.md",
    "Dockerfile",
    "requirements.lock",
    "requirements-judge.lock",
    "uv.lock",
    "pyproject.toml",
    "manage.py",
    "gunicorn.conf.py",
    "compose.yaml",
    ".dockerignore",
    ".gitignore",
    ".python-version",
}
MAX_SOURCE_BYTES = 40 * 1024 * 1024
MAX_SOURCE_ARCHIVE_BYTES = MAX_SOURCE_BYTES + 4 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024 * 1024
HEX = re.compile(r"[a-f0-9]{64}")
IMAGE = re.compile(r"sha256:[a-f0-9]{64}")


class ReleaseError(ValueError):
    pass


def initial_content_commands(manifest):
    """Explicit demo-catalog installation, compatible with frozen older images."""
    files = manifest["source_files"]
    content = ["content_import"]
    if "content/en.json" in files:
        if "localization/management/commands/translation_import.py" not in files:
            raise ReleaseError("Translation content requires its reviewed importer")
        content += ["--translations", "/app/content/en.json"]
    commands = [content]
    if "content/projects.json" in files:
        if "projects/management/commands/project_import.py" not in files:
            raise ReleaseError("Project content requires its reviewed importer")
        commands.append(["project_import", "/app/content/projects.json"])
    if "content/challenges.json" in files:
        if "challenges/management/commands/challenge_import.py" not in files:
            raise ReleaseError("Challenge content requires its reviewed importer")
        commands.append(["challenge_import", "/app/content/challenges.json"])
    return commands


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(files):
    return hashlib.sha256(
        json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def allowed_source(name):
    path = PurePosixPath(name)
    return (
        not path.is_absolute()
        and str(path) == name
        and ".." not in path.parts
        and (name in SOURCE_FILES or (len(path.parts) > 1 and path.parts[0] in SOURCE_DIRS))
        and not any(
            part.startswith(".env") or part in {"__pycache__", ".git"} for part in path.parts
        )
        and path.suffix not in {".pyc", ".pyo", ".key", ".pem", ".sqlite3", ".db"}
        and (path.suffix != ".env" or name == "deploy/images.env")
        and ".sqlite3" not in path.name
    )


def snapshot(source, destination):
    """Copy once, then hash the captured bytes; never build from live directories."""
    files = {}
    candidates = [source / name for name in SOURCE_FILES]
    for name in SOURCE_DIRS:
        directory = source / name
        if directory.is_symlink():
            raise ReleaseError("Source directory symlinks are forbidden")
        candidates.extend(directory.rglob("*"))
    for path in sorted(candidates):
        relative = path.relative_to(source).as_posix()
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            raise ReleaseError(f"Source symlink forbidden: {relative}")
        if path.is_dir():
            continue
        if not path.is_file() or not allowed_source(relative):
            raise ReleaseError(f"Unsupported source entry: {relative}")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ReleaseError("Source file exceeds release size limit")
        data = path.read_bytes()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(0o644)
        files[relative] = hashlib.sha256(data).hexdigest()
    if not SOURCE_FILES <= files.keys():
        raise ReleaseError("Required release source files are missing")
    if sum((destination / name).stat().st_size for name in files) > MAX_SOURCE_BYTES:
        raise ReleaseError("Source snapshot exceeds release size limit")
    return files


def source_archive(snapshot_root, files, output):
    with (
        output.open("xb") as raw,
        gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed,
    ):
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for name in sorted(files):
                data = (snapshot_root / name).read_bytes()
                member = tarfile.TarInfo(name)
                member.size = len(data)
                member.mode = 0o644
                archive.addfile(member, io.BytesIO(data))


def docker(*args, **kwargs):
    return subprocess.run(["docker", *args], check=True, timeout=900, **kwargs)


def inspect_images(manifest):
    for role, expected in manifest["images"].items():
        output = docker("image", "inspect", expected, capture_output=True).stdout
        info = json.loads(output)[0]
        labels = info["Config"].get("Labels") or {}
        required = {
            "org.praximesh.source": manifest["source_id"],
            "org.praximesh.role": role,
            "org.praximesh.wasi": manifest["judge"]["bundle_sha256"],
            "org.praximesh.policy": manifest["judge"]["policy_sha256"],
        }
        if (
            info["Id"] != expected
            or info["Architecture"] != "amd64"
            or info["Os"] != "linux"
            or info["Config"]["User"] != "65532:65532"
            or any(labels.get(key) != value for key, value in required.items())
        ):
            raise ReleaseError("Imported image identity, role or nonroot configuration mismatch")
        environment = dict(item.split("=", 1) for item in info["Config"].get("Env", []))
        if environment.get("JUDGE_WASI_SHA256") != manifest["judge"]["bundle_sha256"]:
            raise ReleaseError("Image evaluation artifact configuration mismatch")


def build(bundle, bundle_sha256, output):
    if output.exists():
        raise ReleaseError("Refusing to overwrite a release")
    bundle_info = WasiSandbox(bundle, bundle_sha256).verify_bundle()
    output.parent.mkdir(parents=True, exist_ok=True)
    logs = ROOT / "test-results"
    logs.mkdir(exist_ok=True)
    log_path = logs / ("release-build-" + uuid.uuid4().hex[:12] + ".log")
    with tempfile.TemporaryDirectory(prefix=".praximesh-release-", dir=output.parent) as temporary:
        work = Path(temporary)
        context, package = work / "source", work / "release"
        context.mkdir()
        package.mkdir(mode=0o700)
        files = snapshot(ROOT, context)
        if files["judge/wasi_policy.py"] != bundle_info["engine_policy"]:
            raise ReleaseError("Interpreter does not match the captured engine policy")
        shutil.copytree(bundle, context / "runtime/wasi")
        WasiSandbox(context / "runtime/wasi", bundle_sha256).verify_bundle()
        source_id = identity(files)
        policy = policy_digest(context)
        images = {}
        with log_path.open("wb") as log:
            for role in ["web", "worker"]:
                print(f"Building {role} from captured source {source_id[:16]}", flush=True)
                iidfile = work / (role + ".iid")
                docker(
                    "build",
                    "--target",
                    role,
                    "--iidfile",
                    str(iidfile),
                    "--build-arg",
                    "WASI_SHA256=" + bundle_sha256,
                    "--label",
                    "org.praximesh.source=" + source_id,
                    "--label",
                    "org.praximesh.role=" + role,
                    "--label",
                    "org.praximesh.wasi=" + bundle_sha256,
                    "--label",
                    "org.praximesh.policy=" + policy,
                    str(context),
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                images[role] = iidfile.read_text().strip()
        source_archive(context, files, package / "source.tar.gz")
        docker("image", "save", "--output", str(package / "images.tar"), *images.values())
        manifest = {
            "format": 1,
            "platform": "linux/amd64",
            "source_id": source_id,
            "source_files": files,
            "images": images,
            "judge": {
                "backend": "wasi",
                "target": TARGET,
                "bundle_sha256": bundle_sha256,
                "policy_sha256": policy,
            },
            "payloads": {
                name: {"sha256": sha256(package / name), "bytes": (package / name).stat().st_size}
                for name in ["source.tar.gz", "images.tar"]
            },
        }
        (package / "release.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        digest = sha256(package / "release.json")
        verify(package, digest)
        inspect_images(manifest)
        package.rename(output)
    print(f"Release directory: {output}")
    print(f"RELEASE_SHA256={digest}")
    print(f"Build log: {log_path}")
    return digest


def verify_source(path, files):
    seen, size = set(), 0
    # Bound decompression before tarfile parses extended/PAX headers as well.
    with gzip.open(path, "rb") as compressed:
        expanded = compressed.read(MAX_SOURCE_ARCHIVE_BYTES + 1)
    if len(expanded) > MAX_SOURCE_ARCHIVE_BYTES:
        raise ReleaseError("Source archive expansion exceeds release size limit")
    with tarfile.open(fileobj=io.BytesIO(expanded), mode="r:") as archive:
        for member in archive:
            if (
                not member.isfile()
                or not allowed_source(member.name)
                or member.name in seen
                or member.name not in files
                or not 0 <= member.size <= 10 * 1024 * 1024
            ):
                raise ReleaseError("Unsafe or unexpected source archive entry")
            size += member.size
            if size > MAX_SOURCE_BYTES:
                raise ReleaseError("Source archive exceeds release size limit")
            with archive.extractfile(member) as data:
                if hashlib.file_digest(data, "sha256").hexdigest() != files[member.name]:
                    raise ReleaseError("Archived source content mismatch")
            seen.add(member.name)
    if seen != set(files):
        raise ReleaseError("Source archive is incomplete")


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("Duplicate JSON field")
        result[key] = value
    return result


def verify_image_archive(path, images):
    """Check configs and layers without relying on an existing daemon layer cache."""
    with tarfile.open(path, "r:") as archive:
        member = archive.getmember("manifest.json")
        if not member.isfile() or member.size > 100000:
            raise ReleaseError("Invalid Docker archive manifest")
        with archive.extractfile(member) as stream:
            entries = json.load(stream, object_pairs_hook=unique_fields)
        if not isinstance(entries, list) or len(entries) != 2:
            raise ReleaseError("Expected exactly the Web and worker images")
        found = set()
        layer_hashes = {}
        for entry in entries:
            name = entry["Config"]
            if PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts:
                raise ReleaseError("Unsafe Docker image config path")
            member = archive.getmember(name)
            if not member.isfile() or member.size > 1024 * 1024:
                raise ReleaseError("Invalid Docker image config")
            with archive.extractfile(member) as stream:
                raw = stream.read()
            found.add("sha256:" + hashlib.sha256(raw).hexdigest())
            config = json.loads(raw, object_pairs_hook=unique_fields)
            if config["rootfs"]["type"] != "layers" or len(entry["Layers"]) != len(
                config["rootfs"]["diff_ids"]
            ):
                raise ReleaseError("Docker image layer inventory mismatch")
            for layer, expected in zip(entry["Layers"], config["rootfs"]["diff_ids"], strict=True):
                if PurePosixPath(layer).is_absolute() or ".." in PurePosixPath(layer).parts:
                    raise ReleaseError("Unsafe Docker layer path")
                if layer not in layer_hashes:
                    member = archive.getmember(layer)
                    if not member.isfile() or member.size > MAX_IMAGE_BYTES:
                        raise ReleaseError("Invalid Docker layer")
                    with archive.extractfile(member) as stream:
                        layer_hashes[layer] = (
                            "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
                        )
                if layer_hashes[layer] != expected:
                    raise ReleaseError("Docker layer content differs from its image config")
        if found != set(images.values()) or len(found) != 2:
            raise ReleaseError("Docker archive image identities differ from the release")


def verify(directory, expected_sha256):
    """No extraction or Docker actions occur before the trusted digest is checked."""
    if not HEX.fullmatch(expected_sha256):
        raise ReleaseError("An independently trusted manifest SHA-256 is required")
    names = {"release.json", "source.tar.gz", "images.tar"}
    if (
        directory.is_symlink()
        or not directory.is_dir()
        or {p.name for p in directory.iterdir()} != names
    ):
        raise ReleaseError("Unexpected release directory contents")
    for name in names:
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ReleaseError("Release payloads must be regular files")
    path = directory / "release.json"
    if path.stat().st_size > 1024 * 1024 or sha256(path) != expected_sha256:
        raise ReleaseError("Release manifest digest mismatch")
    try:
        manifest = json.loads(path.read_text(), object_pairs_hook=unique_fields)
        if (
            set(manifest)
            != {"format", "platform", "source_id", "source_files", "images", "judge", "payloads"}
            or manifest["format"] != 1
            or manifest["platform"] != "linux/amd64"
        ):
            raise ReleaseError("Unsupported release format/platform")
        files = manifest["source_files"]
        if (
            not isinstance(files, dict)
            or not SOURCE_FILES <= files.keys()
            or len(files) > 2000
            or any(
                not allowed_source(name) or not HEX.fullmatch(digest)
                for name, digest in files.items()
            )
            or identity(files) != manifest["source_id"]
        ):
            raise ReleaseError("Invalid source inventory")
        if set(manifest["images"]) != {"web", "worker"} or any(
            not IMAGE.fullmatch(image) for image in manifest["images"].values()
        ):
            raise ReleaseError("Images must be pinned by immutable image ID")
        judge = manifest["judge"]
        if (
            set(judge) != {"backend", "target", "bundle_sha256", "policy_sha256"}
            or judge["backend"] != "wasi"
            or judge["target"] != TARGET
            or not HEX.fullmatch(judge["bundle_sha256"])
            or not HEX.fullmatch(judge["policy_sha256"])
        ):
            raise ReleaseError("Unsupported evaluation identity")
        if set(manifest["payloads"]) != names - {"release.json"}:
            raise ReleaseError("Unexpected payload inventory")
        for name, info in manifest["payloads"].items():
            payload = directory / name
            maximum = MAX_IMAGE_BYTES if name == "images.tar" else MAX_SOURCE_ARCHIVE_BYTES
            if (
                set(info) != {"bytes", "sha256"}
                or not 0 < info["bytes"] <= maximum
                or payload.stat().st_size != info["bytes"]
                or not HEX.fullmatch(info["sha256"])
                or sha256(payload) != info["sha256"]
            ):
                raise ReleaseError("Release payload digest/size mismatch")
        verify_source(directory / "source.tar.gz", files)
        verify_image_archive(directory / "images.tar", manifest["images"])
    except (KeyError, TypeError, OSError, tarfile.TarError, json.JSONDecodeError) as error:
        raise ReleaseError("Malformed release artifact") from error
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--bundle", type=Path, required=True)
    builder.add_argument("--bundle-sha256", required=True)
    builder.add_argument("--output", type=Path, required=True)
    for action in ["verify", "load"]:
        command = commands.add_parser(action)
        command.add_argument("--release", type=Path, required=True)
        command.add_argument("--sha256", required=True)
    options = parser.parse_args()
    try:
        if options.action == "build":
            build(options.bundle.resolve(), options.bundle_sha256, options.output.resolve())
        else:
            manifest = verify(options.release, options.sha256)
            if options.action == "load":
                docker("image", "load", "--input", str(options.release / "images.tar"))
                inspect_images(manifest)
            print("Verified release " + options.sha256)
            print(
                json.dumps(
                    {"source_id": manifest["source_id"], "images": manifest["images"]}, indent=2
                )
            )
    except (ReleaseError, OSError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Release operation failed: {error}\n")


if __name__ == "__main__":
    main()
