"""Exercise the local CUDA CLI using real device-free compilation and recovery."""

import argparse
import hashlib
import json
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_compiler import compiler_digest  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from scripts import practice  # noqa: E402
from scripts.container_supervision_acceptance import birth, gone  # noqa: E402
from scripts.practice_cuda import CompilerWorkspace  # noqa: E402

SOURCES = (
    "scripts/practice.py",
    "scripts/practice_cuda.py",
    "scripts/build_practice_compiler.py",
    "scripts/practice_compiler_acceptance.py",
    "content/candidates/cuda.json",
    "tests/cuda_incorrect_solutions.json",
)


def sources():
    return {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in SOURCES}


class Acceptance:
    def __init__(self, options):
        self.options = options
        self.output = options.output
        if not self.output.is_absolute() or self.output.resolve() != self.output:
            raise ValueError("Use a new absolute output directory without symlinks")
        self.output.mkdir(mode=0o700)
        self.state = self.output / "state"
        self.exercise = self.output / "exercise"
        self.exercise.mkdir(mode=0o700)
        self.engine = LocalEngine(options.docker_socket)
        self.engine.verify()
        self.report = {
            "passed": False,
            "scope": "Real NVRTC compilation, CLI errors, signals and original task cleanup only",
            "gpu_accessed": False,
            "formal_result": False,
            "image": options.compiler_image,
            "compiler_contract": compiler_digest(),
            "sources": sources(),
            "checks": [],
        }

    def save(self):
        (self.output / "result.json").write_text(json.dumps(self.report, indent=2) + "\n")

    def arguments(self, *, recover=False, image=None, target="8.6"):
        args = [sys.executable, str(ROOT / "scripts/practice.py")]
        if recover:
            args += ["recover-compiler"]
        else:
            args += [
                "compile",
                str(self.exercise),
                "--compiler-image",
                image or self.options.compiler_image,
                "--architecture",
                target,
            ]
        return [*args, "--state", str(self.state), "--docker-socket", self.options.docker_socket]

    def run(self, name, expected, **options):
        result = subprocess.run(
            self.arguments(**options), capture_output=True, timeout=210, cwd=ROOT
        )
        (self.output / (name + ".stdout")).write_bytes(result.stdout)
        (self.output / (name + ".stderr")).write_bytes(result.stderr)
        row = {"name": name, "exit": result.returncode}
        if result.stdout:
            row["result"] = json.loads(result.stdout)
            assert row["result"]["formal_result"] is False
            assert row["result"]["executed"] is False
            assert row["result"]["ready"] is False
        self.report["checks"].append(row)
        self.save()
        if isinstance(expected, str):
            code = (
                0
                if expected in {"compiled", "recovered", "idle"}
                else 2
                if expected == "platform_error"
                else 1
            )
            assert result.returncode == code, row
            assert row["result"]["status"] == expected, row
        else:
            assert result.returncode == expected, row
        return row

    def package(self, problem):
        package = {
            "format": 1,
            "version_id": 1,
            "source_digest": hashlib.sha256(
                json.dumps(problem, sort_keys=True).encode()
            ).hexdigest(),
            "formal_result": False,
            "problem": {
                key: value
                for key, value in problem.items()
                if key not in {"reference", "tests", "license", "attribution"}
            },
            "public_tests": [
                {key: value for key, value in case.items() if key != "public"}
                for case in problem["tests"]
                if case["public"]
            ],
        }
        practice.validate_package(package)
        document = {"server": "https://example.test", "package": package}
        (self.exercise / "exercise.json").write_text(json.dumps(document))
        return document

    def interrupt(self, number):
        name = signal.Signals(number).name.lower()
        processes = {}
        with (self.output / (name + ".stdout")).open("wb") as stdout:
            with (self.output / (name + ".stderr")).open("wb") as stderr:
                process = subprocess.Popen(self.arguments(), stdout=stdout, stderr=stderr, cwd=ROOT)
                try:
                    end = time.monotonic() + 30
                    record = None
                    while time.monotonic() < end:
                        assert process.poll() is None, "Compiler exited before interruption"
                        paths = list((self.state / "journal").glob("*.active.json"))
                        if paths:
                            assert len(paths) == 1
                            record = json.loads(paths[0].read_text())
                            if record["phase"] == "running":
                                break
                        time.sleep(0.02)
                    else:
                        raise RuntimeError("No running original task was observed")
                    # Pause only this owned CLI so inspection cannot race normal completion.
                    process.send_signal(signal.SIGSTOP)
                    until = time.monotonic() + 2
                    while time.monotonic() < until:
                        fields = (
                            Path(f"/proc/{process.pid}/stat").read_text().rsplit(")", 1)[1].split()
                        )
                        if fields[0] == "T":
                            break
                        time.sleep(0.01)
                    else:
                        raise RuntimeError("Owned CLI did not stop for signal observation")
                    actual = json.loads(
                        self.engine.run("container", "inspect", record["container_id"]).stdout
                    )[0]
                    assert actual["Id"] == record["container_id"] and actual["State"]["Running"]
                    assert actual["Image"] == self.options.compiler_image
                    assert not actual["HostConfig"].get("Devices")
                    assert not actual["HostConfig"].get("DeviceRequests")
                    rows = (
                        self.engine.run("container", "top", actual["Id"], "-eo", "pid")
                        .stdout.decode()
                        .splitlines()[1:]
                    )
                    processes = {int(row): birth(int(row)) for row in rows}
                    assert processes and all(processes.values())
                    process.send_signal(number)
                    if number != signal.SIGKILL:
                        process.send_signal(signal.SIGCONT)
                    code = process.wait(timeout=30)
                    assert code == (-signal.SIGKILL if number == signal.SIGKILL else 2), code
                    self.report["checks"].append(
                        {
                            "name": name,
                            "exit": code,
                            "container": actual["Id"],
                            "observed_running": True,
                            "processes": processes,
                        }
                    )
                    self.save()
                    if number == signal.SIGKILL:
                        self.run("pending-task-refused", 2)
                        self.run("original-task-recovery", "recovered", recover=True)
                    else:
                        self.run("cancel-cleanup-confirmed", "idle", recover=True)
                    gone(processes)
                    assert (
                        self.engine.run(
                            "container", "inspect", actual["Id"], check=False
                        ).returncode
                        != 0
                    )
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=10)
                    # Only this private installation's original journal can authorize cleanup.
                    self.run(name + "-final-recovery", 0, recover=True)
        self.run(name + "-fresh-compilation", "compiled")

    def execute(self):
        problems = json.loads((ROOT / "content/candidates/cuda.json").read_text())["problems"]
        wrong = json.loads((ROOT / "tests/cuda_incorrect_solutions.json").read_text())
        solution = self.exercise / "solution.cu"
        try:
            for problem in problems:
                self.package(problem)
                solution.write_text(problem["reference"])
                self.run(problem["id"] + "-reference", "compiled")
                for index, item in enumerate(wrong[problem["id"]]):
                    solution.write_text(item["source"])
                    self.run(problem["id"] + "-wrong-" + str(index), "compiled")
            document = self.package(problems[0])
            solution.write_text('extern "C" __global__ void solve( {')
            self.run("syntax-error", "syntax_error")
            solution.write_text(problems[0]["reference"])
            self.run("unsupported-architecture", "platform_error", target="99.9")
            self.run("unknown-image", 2, image="sha256:" + "0" * 64)
            with CompilerWorkspace(self.state, self.engine, create=False):
                self.run("concurrent-cli-refused", 2)
            document["package"]["problem"]["docker_socket"] = "/untrusted/socket"
            (self.exercise / "exercise.json").write_text(json.dumps(document))
            self.run("package-infrastructure-refused", 2)
            self.package(problems[0])
            self.interrupt(signal.SIGINT)
            self.interrupt(signal.SIGKILL)
            with CompilerWorkspace(self.state, self.engine, create=False) as workspace:
                assert workspace.record() is None
                histories = list((self.state / "journal").glob("*.done.json"))
                assert histories
                for path in histories:
                    record = json.loads(path.read_text())
                    assert record["version"] == 2 and record["phase"] == "removed"
                    assert (
                        self.engine.run(
                            "container", "inspect", record["container_id"], check=False
                        ).returncode
                        != 0
                    )
                self.report["removed_tasks"] = len(histories)
            assert self.report["sources"] == sources()
            assert self.report["compiler_contract"] == compiler_digest()
            self.report["passed"] = True
        finally:
            self.save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--compiler-image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    acceptance = Acceptance(parser.parse_args())
    print(json.dumps({"report": str(acceptance.output / "result.json")}), flush=True)
    acceptance.execute()
    print(json.dumps({"passed": True, "checks": len(acceptance.report["checks"])}))


if __name__ == "__main__":
    main()
