"""Bounded agent turns, supervised with actual child CPU accounting."""

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from judge.agent_policy import PROFILE
from judge.sandbox import Execution, SandboxUnavailable
from judge.stdio import parse_output
from judge.wasi import WasiSandbox


@dataclass
class EpisodeBudget:
    started: float = field(default_factory=time.monotonic)
    turns: int = 0
    cpu_seconds: float = 0
    fuel_reserved: int = 0
    output_reserved: int = 0
    observed_stdout_bytes: int = 0
    last_child_pid: int | None = None
    children_reaped: int = 0
    exhaustion: str = ""

    @property
    def deadline(self):
        return self.started + PROFILE["episode_wall_seconds"]

    def reserve(self):
        if time.monotonic() >= self.deadline:
            self.exhaustion = "episode_wall"
            return "time_limit"
        if self.cpu_seconds >= PROFILE["episode_cpu_seconds"]:
            self.exhaustion = "episode_cpu"
            return "time_limit"
        if (
            self.turns >= PROFILE["max_turns"]
            or self.fuel_reserved + PROFILE["turn_fuel"] > PROFILE["episode_fuel_reserved"]
            or self.output_reserved + PROFILE["turn_output_bytes"]
            > PROFILE["episode_output_reserved"]
        ):
            self.exhaustion = "turn_budget"
            return "wrong_answer"
        self.turns += 1
        self.fuel_reserved += PROFILE["turn_fuel"]
        self.output_reserved += PROFILE["turn_output_bytes"]
        return ""


def _cpu_seconds(pid):
    # Only the PID returned by this controller's own Popen is read. The trusted
    # driver has no subprocesses; Linux process utime/stime include its threads.
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    except (OSError, IndexError, ValueError) as exc:
        raise SandboxUnavailable("Cannot account for the agent driver CPU.") from exc


class AgentRuntime:
    def __init__(self, sandbox):
        if type(sandbox) is not WasiSandbox:
            raise SandboxUnavailable("Agent tools require the reviewed WASI backend.")
        self.sandbox = sandbox

    def execute(self, source, observation, state, budget):
        verdict = budget.reserve()
        if verdict:
            return Execution(verdict)
        payload = json.dumps(
            {"source": source, "function": "act", "args": [observation, state]},
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
        if len(payload) > PROFILE["input_bytes"]:
            raise SandboxUnavailable("Agent request exceeds the fixed protocol limit.")
        self.sandbox.verify_bundle()
        if not hasattr(self.sandbox, "runtime_digest"):
            raise SandboxUnavailable("WASI preflight is required before agent execution.")
        with self.sandbox.runtime_file.open("rb") as library:
            if hashlib.file_digest(library, "sha256").hexdigest() != self.sandbox.runtime_digest:
                raise SandboxUnavailable("Wasmtime changed while the worker was running.")
        started = time.monotonic()
        deadline = min(budget.deadline, started + PROFILE["turn_wall_seconds"])
        if started >= deadline:
            budget.exhaustion = "episode_wall"
            return Execution("time_limit")
        output = bytearray()
        ready = False
        sent = transport = 0
        verdict = ""
        usage = None
        with tempfile.TemporaryDirectory(prefix="praximesh-agent-") as directory:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    str(Path(__file__).with_name("agent_driver.py")),
                    str(self.sandbox.bundle),
                    str(os.getpid()),
                    "python",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=directory,
                env={"LANG": "C.UTF-8", "TMPDIR": directory},
                start_new_session=True,
                close_fds=True,
            )
            budget.last_child_pid = process.pid

            def reap():
                nonlocal usage
                if process.returncode is None:
                    child, status, measured = os.wait4(process.pid, os.WNOHANG)
                    if child:
                        # Never call Popen.poll/wait before wait4: they would
                        # consume the only exact per-child rusage record.
                        process.returncode = os.waitstatus_to_exitcode(status)
                        usage = measured
                        budget.children_reaped += 1

            def terminate():
                if process.returncode is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    until = time.monotonic() + 3
                    while process.returncode is None and time.monotonic() < until:
                        reap()
                        if process.returncode is None:
                            time.sleep(0.005)
                    if process.returncode is None:
                        raise SandboxUnavailable("Agent driver did not terminate.")

            try:
                with selectors.DefaultSelector() as selector:
                    for stream, kind in (
                        (process.stdin, "in"),
                        (process.stdout, "out"),
                        (process.stderr, "err"),
                    ):
                        os.set_blocking(stream.fileno(), False)
                        selector.register(
                            stream,
                            selectors.EVENT_WRITE if kind == "in" else selectors.EVENT_READ,
                            kind,
                        )
                    while selector.get_map() or process.returncode is None:
                        reap()
                        now = time.monotonic()
                        if now >= deadline:
                            budget.exhaustion = (
                                "episode_wall" if now >= budget.deadline else "turn_wall"
                            )
                            verdict = "time_limit"
                            break
                        if process.returncode is None and (
                            budget.cpu_seconds + _cpu_seconds(process.pid)
                            >= PROFILE["episode_cpu_seconds"]
                        ):
                            budget.exhaustion = "episode_cpu"
                            verdict = "time_limit"
                            break
                        for key, _ in selector.select(timeout=PROFILE["cpu_sample_seconds"]):
                            if key.data == "in":
                                try:
                                    sent += os.write(key.fd, payload[sent : sent + 4096])
                                except BrokenPipeError:
                                    sent = len(payload)
                                if sent == len(payload):
                                    selector.unregister(key.fileobj)
                                    key.fileobj.close()
                                continue
                            chunk = os.read(key.fd, 4096)
                            if not chunk:
                                selector.unregister(key.fileobj)
                                continue
                            transport += len(chunk)
                            if transport > PROFILE["turn_output_bytes"] + 1024:
                                raise SandboxUnavailable("Agent driver transport exceeded limit.")
                            if key.data == "out":
                                output.extend(chunk)
                            elif chunk == b"R" and not ready:
                                ready = True
                            else:
                                raise SandboxUnavailable("Agent driver transport failed.")
                terminate()
            finally:
                try:
                    terminate()
                finally:
                    for stream in (process.stdin, process.stdout, process.stderr):
                        if stream and not stream.closed:
                            stream.close()
                    if usage is not None:
                        budget.cpu_seconds += usage.ru_utime + usage.ru_stime
        elapsed = int((time.monotonic() - started) * 1000)
        budget.observed_stdout_bytes += len(output)
        if budget.cpu_seconds >= PROFILE["episode_cpu_seconds"]:
            budget.exhaustion = "episode_cpu"
            verdict = "time_limit"
        if verdict:
            return Execution(verdict, elapsed_ms=elapsed)
        if not ready:
            raise SandboxUnavailable("Agent driver initialization failed.")
        verdict = {
            71: "time_limit",
            72: "output_limit",
            73: "syntax_error",
            74: "memory_limit",
            75: "runtime_error",
            -signal.SIGXCPU: "time_limit",
            -signal.SIGALRM: "time_limit",
        }.get(process.returncode, "")
        if verdict:
            return Execution(verdict, elapsed_ms=elapsed)
        if process.returncode != 0:
            raise SandboxUnavailable("Agent driver failed; execution is unavailable.")
        try:
            value = parse_output(output, envelope=True)
        except (ValueError, RecursionError, OverflowError):
            return Execution("runtime_error", elapsed_ms=elapsed)
        return Execution("ok", value, elapsed)
