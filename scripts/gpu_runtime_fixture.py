"""Trusted CPU-only controller for offline runsc infrastructure acceptance."""

import hashlib
import shutil
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.gpu_environment import PrivateRuntime  # noqa: E402
from judge.gpu_session import GPUExecutionError, GPUSession  # noqa: E402
from judge.gpu_transport import Channel  # noqa: E402


class CancelFixture(Exception):
    pass


def cpu_stats():
    return {
        key: int(value)
        for key, value in (
            line.split()
            for line in Path("/sys/fs/cgroup/workloads/cpu.stat").read_text().splitlines()
        )
    }


def main():
    base, socket_path, runtime_archive, image_archive, deadline = sys.argv[1:]
    base = Path(base)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(socket_path)
    channel = Channel(sock, deadline=float(deadline), on_tick=lambda: None)
    runtime = None
    phase = "prepare"
    record = {"passed": False, "checks": [], "cleanup": []}
    try:
        channel.send({"event": "connected"})
        runtime = PrivateRuntime(base, deadline=float(deadline), on_tick=channel.connected)
        record["identity"] = runtime.prepare(Path(runtime_archive), Path(image_archive))
        fixture = base / "cpu-guest.py"
        shutil.copyfile(ROOT / "scripts/gpu_runtime_cpu_guest.py", fixture)
        fixture.chmod(0o444)
        protected = base / "protected-canary"
        protected.write_text("fixed controller-private infrastructure canary")
        channel.send({"event": "ready", "identity": record["identity"]})
        request = channel.receive()
        if set(request) != {"canary", "port"}:
            raise ValueError("Unexpected infrastructure acceptance request")
        identity_request = dict(request, operation="identity", protected_path=str(protected))

        def session(label):
            runtime.create(
                label,
                "/usr/local/bin/python",
                ["-I", "/opt/fixture.py"],
                mounts=[(fixture, "/opt/fixture.py")],
            )

            def cleanup():
                record["cleanup"].append(dict(name=label, **runtime.stop_container(label)))

            # Raw exchange exercises the same bounded guest pipes. This never
            # emits a fake CUDA compiled frame or calls compile/evaluate_kernel.
            return GPUSession(
                runtime.attach(label),
                cleanup,
                architecture=[],
                mode="run",
                on_tick=runtime.tick,
            )

        def verify_identity(active):
            values, _elapsed = active.exchange(identity_request, seconds=10)
            assert values["event"] == "identity"
            assert values["uid"] == 65534
            assert not any(
                values[key]
                for key in ("root_write", "private_file_visible", "host_loopback_reachable")
            )
            assert not values["gpu_nodes"] and int(values["capabilities"], 16) == 0
            assert not {"DATABASE_URL", "CONTAINER_HOST", "HTTP_PROXY"}.intersection(
                values["environment_names"]
            )
            assert values["sha256"] == hashlib.sha256(request["canary"].encode()).hexdigest()
            return values

        for scenario in (
            "identity",
            "output",
            "timeout",
            "cancel",
            "client-exit",
            "cpu-limit",
            "storage-limit",
        ):
            phase = scenario
            result = {"name": scenario, "passed": False}
            record["checks"].append(result)
            with session(scenario) as active:
                identity = verify_identity(active)
                if scenario == "identity":
                    result["guest"] = identity
                    active.finish()
                elif scenario in {"output", "timeout"}:
                    try:
                        active.exchange(
                            {"operation": "output" if scenario == "output" else "hang"}, seconds=1
                        )
                    except GPUExecutionError as error:
                        assert error.verdict == (
                            "output_limit" if scenario == "output" else "time_limit"
                        )
                        result["verdict"] = error.verdict
                    else:
                        raise RuntimeError("Fixed failing guest unexpectedly returned a result")
                elif scenario == "cancel":
                    until = time.monotonic() + 0.5

                    def cancel(until=until):
                        runtime.tick()
                        if time.monotonic() >= until:
                            raise CancelFixture()

                    active.on_tick = cancel
                    try:
                        active.exchange({"operation": "hang"}, seconds=3)
                    except CancelFixture:
                        result["cancelled"] = True
                    else:
                        raise RuntimeError("Fixed guest did not observe cancellation")
                elif scenario == "client-exit":
                    values, _elapsed = active.exchange({"operation": "started-hang"}, seconds=2)
                    assert values == {"event": "busy"}
                    active.process.kill()
                    active.process.wait(timeout=2)
                    assert runtime.inspect(scenario)["State"]["Running"]
                    result["container_outlived_client"] = True
                elif scenario == "cpu-limit":
                    before = cpu_stats()
                    started = time.monotonic()
                    values, _elapsed = active.exchange({"operation": "busy"}, seconds=6)
                    elapsed = time.monotonic() - started
                    after = cpu_stats()
                    cpu = (after["usage_usec"] - before["usage_usec"]) / 1e6
                    periods = after["nr_throttled"] - before["nr_throttled"]
                    assert values == {"event": "busy-finished"}
                    assert elapsed >= 3 and cpu >= 1 and cpu / elapsed <= 0.65 and periods > 0
                    result.update(
                        elapsed_seconds=elapsed, cpu_seconds=cpu, throttled_periods=periods
                    )
                    active.finish()
                else:
                    values, _elapsed = active.exchange(
                        {"operation": "temporary-storage"}, seconds=5
                    )
                    assert (
                        values["event"] == "storage" and values["limited"] and values["mib"] <= 32
                    )
                    result["storage"] = values
                    active.finish()
            # A fresh isolated process must still exchange the correct canary
            # after each termination/failure; this is CPU recovery, not CUDA.
            with session(scenario + "-recovery") as recovered:
                verify_identity(recovered)
                recovered.finish()
            result.update(passed=True, cpu_recovery=True)
        phase = "store-cleanup"
        runtime.close()
        record.update(passed=True, private_store_removed=True)
    except BaseException as error:
        record.update(phase=phase, error=type(error).__name__ + ": " + str(error))
        if getattr(error, "diagnostics", None):
            # Only reviewed CPU infrastructure and fake data run in this script.
            record["diagnostics"] = error.diagnostics.decode(errors="replace")[-4096:]
        if runtime is not None:
            try:
                runtime.close()
                record["private_store_removed"] = True
            except BaseException as cleanup_error:
                record["cleanup_error"] = type(cleanup_error).__name__ + ": " + str(cleanup_error)
    finally:
        channel.send({"event": "finished", "result": record})
        if channel.receive() != {"event": "finish"}:
            raise ValueError("Unexpected completion acknowledgement")
        channel.close()
    return 0 if record["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
