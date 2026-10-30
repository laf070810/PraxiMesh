"""Real pipes for protocol tests; these fixtures do not compile or run CUDA.

The fixed helper emits predetermined frames. It never executes incoming source;
its success is not GPU, isolation, correctness or performance acceptance evidence.
"""

import json
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

from judge.gpu import evaluate_kernel
from judge.gpu_policy import PROFILE
from judge.gpu_session import GPUExecutionError, GPUSession
from judge.sandbox import SandboxUnavailable

HELPER = r"""
import json,os,sys,time
scenario, record = sys.argv[1:]
with open(record,'w') as captured:
    request = sys.stdin.readline()
    captured.write(request); captured.flush()
    if scenario.startswith('exit-'): sys.exit(int(scenario.split('-')[1]))
    if scenario == 'compile-hang': time.sleep(60)
    if scenario == 'duplicate':
        print('{"event":"compiled","event":"compiled","architecture":[8,6]}',flush=True)
    else:
        print(json.dumps({'event':'compiled','architecture':[9,0] if scenario=='architecture' else [8,6]}),flush=True)
    for request in sys.stdin:
        captured.write(request); captured.flush()
        data=json.loads(request)
        if scenario=='hang': time.sleep(60)
        if scenario=='stderr': os.write(2,b'x'*100000)
        if scenario=='frame':
            os.write(1,b'x'*400000)
            time.sleep(60)
        result={'values':[4,6]}
        if scenario=='forged': result['median_batch_ms']=0.001
        if scenario=='wrong-batch' and 'repetitions' in data: result={'values':[999,999]}
        if scenario=='wrong': result={'values':[999,999]}
        print(json.dumps(result),flush=True)
    if scenario=='trailing': print('{"values":[4,6]}',flush=True)
"""


@pytest.fixture
def harness(tmp_path):
    sequence = [0]

    @contextmanager
    def create(
        scenario="correct",
        *,
        tick=lambda: None,
        cleanup_failure=False,
        mode="submit",
        exit_verdict=None,
    ):
        sequence[0] += 1
        captured = tmp_path / str(sequence[0])
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", HELPER, scenario, str(captured)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        cleaned = []

        def cleanup():
            cleaned.append(True)
            if process.poll() is None:
                process.kill()
            if cleanup_failure:
                raise SandboxUnavailable("Cleanup could not verify physical termination")

        try:
            with GPUSession(
                process,
                cleanup,
                architecture=[8, 6],
                mode=mode,
                on_tick=tick,
                exit_verdict=exit_verdict,
            ) as session:
                yield session, captured
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            assert cleaned == [True]

    return create


def problem():
    return {
        "evaluator": "cuda-kernel-v1",
        "language": "cuda",
        "entrypoint": "solve",
        "operation": "vector-add-f32-v1",
        "comparison": {"kind": "float", "absolute": 1e-5, "relative": 1e-5},
        "tests": [
            {
                "name": "public",
                "public": True,
                "inputs": {"a": [1, 2], "b": [3, 4]},
                "expected": [4, 6],
            },
            {
                "name": "protected-canary",
                "public": False,
                "inputs": {"a": [1, 2], "b": [3, 4]},
                "expected": [4, 6],
            },
        ],
    }


@pytest.mark.parametrize("operation", ["vector-add-f32-v1", "matmul-f32-v1"])
def test_maximum_valid_numeric_request_fits_both_protocol_endpoints(harness, operation):
    from judge.gpu_runtime import MAX_MESSAGE

    inputs = {"a": [-1.234567890123456e-123] * 4096, "b": [1.234567890123456e-123] * 4096}
    if operation == "matmul-f32-v1":
        inputs.update(m=64, n=64, k=64)
    with harness() as (session, captured):
        session.compile("transport marker", operation)
        session.execute({"inputs": inputs}, batch=True)
        session.finish()
    # The helper only echoes a fixed response; this verifies input transport,
    # not the result of computing with these arrays.
    request = captured.read_bytes().splitlines()[1]
    assert 128 * 1024 < len(request) + 1 <= MAX_MESSAGE
    assert json.loads(request)["inputs"] == inputs


@pytest.mark.parametrize("mode", ["run", "submit"])
def test_interactive_protocol_keeps_answers_private_and_only_controller_times_batches(
    harness, mode
):
    with harness(mode=mode) as (session, captured):
        result = evaluate_kernel(problem(), "fixed transport marker, never compiled", mode, session)
    requests = [json.loads(line) for line in captured.read_text().splitlines()]
    assert all("expected" not in row and "name" not in row for row in requests)
    assert "protected-canary" not in json.dumps(result)
    assert result["verdict"] == "accepted"
    if mode == "run":
        assert len(requests) == 2 and result["total"] == 1 and "performance" not in result
    else:
        performance = result["performance"]
        assert len(performance["samples_ms"]) == 5
        assert performance["median_batch_ms"] > 0
        assert len([row for row in requests if row.get("repetitions") == 16]) == 7


@pytest.mark.parametrize("scenario", ["wrong", "wrong-batch"])
def test_failed_correctness_or_warmup_has_no_performance_score(harness, scenario):
    with harness(scenario) as (session, _captured):
        result = evaluate_kernel(problem(), "transport marker", "submit", session)
    assert result["verdict"] == "wrong_answer" and "performance" not in result


@pytest.mark.parametrize("scenario", ["duplicate", "architecture", "forged", "trailing"])
def test_changed_identity_forged_timing_and_unsolicited_frames_fail_closed(harness, scenario):
    with pytest.raises(SandboxUnavailable), harness(scenario) as (session, _captured):
        evaluate_kernel(problem(), "transport marker", "submit", session)


@pytest.mark.parametrize(
    "code,verdict", [(2, "runtime_error"), (3, "syntax_error"), (4, "memory_limit")]
)
def test_driver_failure_classification_is_not_a_guest_claim(harness, code, verdict):
    with harness("exit-" + str(code)) as (session, _captured):
        result = evaluate_kernel(problem(), "transport marker", "submit", session)
    assert result["verdict"] == verdict and "performance" not in result


def test_platform_exit_and_failed_cleanup_do_not_become_wrong_answers(harness):
    with pytest.raises(SandboxUnavailable), harness("exit-70") as (session, _captured):
        evaluate_kernel(problem(), "transport marker", "submit", session)
    with (
        pytest.raises(SandboxUnavailable, match="Cleanup"),
        harness("exit-3", cleanup_failure=True) as (session, _captured),
    ):
        evaluate_kernel(problem(), "transport marker", "submit", session)


@pytest.mark.parametrize("scenario", ["stderr", "frame"])
def test_output_is_bounded_before_a_delimiter_or_process_exit(harness, scenario):
    with harness(scenario) as (session, _captured):
        result = evaluate_kernel(problem(), "transport marker", "submit", session)
    assert result["verdict"] == "output_limit"


def test_full_attach_pipe_is_closed_before_container_cleanup(harness):
    with harness("frame") as (session, _captured):
        cleanup = session.cleanup

        def require_detached_client():
            assert session.process.poll() is not None
            assert all(
                stream.closed
                for stream in (
                    session.process.stdin,
                    session.process.stdout,
                    session.process.stderr,
                )
            )
            cleanup()

        session.cleanup = require_detached_client
        result = evaluate_kernel(problem(), "transport marker", "submit", session)
        assert result["verdict"] == "output_limit"


def test_compilation_deadline_and_heartbeat_cancellation_cleanup_owned_process(
    harness, monkeypatch
):
    monkeypatch.setitem(PROFILE, "compile_seconds", 0.1)
    before = time.monotonic()
    with harness("compile-hang") as (session, _captured):
        result = evaluate_kernel(problem(), "transport marker", "submit", session)
    assert result["verdict"] == "time_limit" and time.monotonic() - before < 3

    class Cancelled(Exception):
        pass

    calls = []

    def tick():
        calls.append(True)
        if len(calls) >= 2:
            raise Cancelled

    monkeypatch.setitem(PROFILE, "compile_seconds", 5)
    with pytest.raises(Cancelled), harness("hang", tick=tick) as (session, _captured):
        evaluate_kernel(problem(), "transport marker", "submit", session)
    assert len(calls) == 2


def test_heartbeat_wait_cannot_overrun_deadline_then_send_more_work(harness):
    with harness(tick=lambda: time.sleep(0.03)) as (session, captured):
        with pytest.raises(GPUExecutionError) as error:
            session.exchange({"protocol": 1}, seconds=0.01)
    assert error.value.verdict == "time_limit"
    assert not captured.exists() or not captured.read_text()


def test_sigkill_without_trusted_oom_evidence_is_platform_failure(harness):
    with pytest.raises(SandboxUnavailable), harness("exit-137") as (session, _captured):
        evaluate_kernel(problem(), "transport marker", "submit", session)
    # Classification contract only; this is not a physical memory-limit probe.
    with harness("exit-137", exit_verdict=lambda _code: "memory_limit") as (session, _captured):
        result = evaluate_kernel(problem(), "transport marker", "submit", session)
    assert result["verdict"] == "memory_limit"


def test_run_cannot_borrow_submit_wall_budget(harness):
    with harness(mode="submit") as (session, captured):
        with pytest.raises(ValueError):
            evaluate_kernel(problem(), "transport marker", "run", session)
    assert not captured.exists() or not captured.read_text()
