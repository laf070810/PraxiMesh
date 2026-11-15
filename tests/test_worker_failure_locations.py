"""Only trusted synthetic exceptions; no learner source is executed."""

import pytest

from core.management.commands.container_gpu_worker import ROOT, failure_locations


@pytest.mark.parametrize(
    "filename,expected",
    [
        (str(ROOT / "judge/container_lifecycle.py"), "judge/container_lifecycle.py:2"),
        (str(ROOT / "private/secret-name.py"), "unavailable"),
        ("/private/secret-directory/config.py", "unavailable"),
        (str(ROOT / "judge/../private/secret-name.py"), "unavailable"),
    ],
)
def test_failure_location_does_not_disclose_exception_payload_or_untrusted_paths(
    filename, expected
):
    # Fixed test-only code with a synthetic password and source-like local value.
    program = compile(
        "secret = 'synthetic-password-and-source'\nraise ValueError(secret)\n", filename, "exec"
    )
    try:
        exec(program, {})
    except ValueError as error:
        result = failure_locations(error)
    assert result == expected
    assert "synthetic" not in result and "private" not in result and "secret" not in result
