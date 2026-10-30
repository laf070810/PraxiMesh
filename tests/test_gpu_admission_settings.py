import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "value,valid",
    [
        ("", True),
        ("GPU-AAAAAAAA-1111-2222-3333-444444444444", True),
        ("0", False),
        ("all", False),
        ("GPU-*", False),
        ("GPU-11111111-2222-3333-4444-555555555555," * 2, False),
        (
            "GPU-AAAAAAAA-1111-2222-3333-444444444444,GPU-aaaaaaaa-1111-2222-3333-444444444444",
            False,
        ),
    ],
)
def test_explicit_device_configuration_is_bounded_and_canonical(value, valid):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from praximesh.settings import JUDGE_GPU_DEVICES; print(json.dumps(JUDGE_GPU_DEVICES))",
        ],
        env={**os.environ, "PRAXIMESH_MODE": "local", "JUDGE_GPU_DEVICES": value},
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode == 0) is valid
    if valid and value:
        assert json.loads(result.stdout) == ["GPU-aaaaaaaa-1111-2222-3333-444444444444"]
    elif not valid:
        assert "JUDGE_GPU_DEVICES must list" in result.stderr
