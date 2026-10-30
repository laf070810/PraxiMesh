"""Real bounded pipes for fixed control programs; no learner or GPU execution."""

import os
import sys
import time

import pytest

from judge.gpu_command import CommandFailure, run_command


def command(program, **options):
    return run_command(
        [sys.executable, "-I", "-c", program],
        env={},
        deadline=options.pop("deadline", time.monotonic() + 3),
        on_tick=options.pop("on_tick", lambda: None),
        **options,
    )


def test_large_input_and_output_are_drained_without_pipe_deadlock():
    payload = b"x" * 300000
    result = command("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())", data=payload)
    assert result.returncode == 0 and result.stdout == payload


def test_nonzero_exit_diagnostics_do_not_become_the_public_error():
    with pytest.raises(CommandFailure) as failure:
        command("import sys; print('private-test-marker', file=sys.stderr); sys.exit(9)")
    assert failure.value.returncode == 9
    assert b"private-test-marker" in failure.value.diagnostics
    assert "private-test-marker" not in str(failure.value)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_unbounded_output_stops_the_control_program(stream):
    with pytest.raises(CommandFailure, match="output budget"):
        command(f"import sys\nwhile True: sys.{stream}.write('x' * 8192); sys.{stream}.flush()")


def test_deadline_reaps_the_owned_cli(tmp_path):
    marker = tmp_path / "pid"
    program = f"import os,time; open({str(marker)!r},'w').write(str(os.getpid())); time.sleep(30)"
    with pytest.raises(CommandFailure, match="deadline"):
        command(program, seconds=0.3)
    pid = int(marker.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancellation_is_polled_while_the_command_is_blocked():
    class Cancelled(Exception):
        pass

    calls = []

    def cancel():
        calls.append(True)
        if len(calls) == 2:
            raise Cancelled()

    with pytest.raises(Cancelled):
        command("import time; time.sleep(30)", on_tick=cancel)
    assert len(calls) == 2


def test_callback_wait_cannot_extend_deadline_or_launch_work(tmp_path):
    marker = tmp_path / "must-not-exist"
    with pytest.raises(CommandFailure, match="deadline"):
        command(
            f"open({str(marker)!r},'w').close()",
            deadline=time.monotonic() + 0.01,
            on_tick=lambda: time.sleep(0.03),
        )
    assert not marker.exists()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 0, -1, 91])
def test_invalid_command_budget_is_rejected(value):
    with pytest.raises(ValueError, match="budget"):
        command("raise AssertionError('must not run')", seconds=value)
