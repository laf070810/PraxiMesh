"""Complete-program contracts; only marked tests execute source, exclusively in WASI."""

import io
import json
import os
from pathlib import Path

import pytest
from django.core.management import call_command

from core.management.commands.sandbox_check import check_receipt
from judge.backends import configured_sandbox
from judge.evaluator import evaluate
from judge.sandbox import DockerSandbox, SandboxUnavailable
from judge.stdio import parse_output, validate_request
from judge.stdio_probes import PROBES
from judge.wasi_policy import INPUT, OUTPUT
from judge.wasi_probes import PROBES as FUNCTION_PROBES

REAL = pytest.mark.skipif(
    os.environ.get("RUN_SANDBOX_TESTS") != "1" or os.environ.get("JUDGE_BACKEND") != "wasi",
    reason="Requires actual pinned WASI artifacts; skipped is not evidence",
)


@pytest.mark.parametrize(
    "output",
    [
        b"",
        b"42 extra",
        b"null null",
        b"NaN",
        b"Infinity",
        b"1e999",
        b'{"a":1,"a":2}',
        b'{"a":{"x":1,"x":2}}',
        b"\xff",
        b"[" * 34 + b"0" + b"]" * 34,
        b" " * (OUTPUT + 1),
    ],
)
def test_program_output_is_exactly_one_strict_bounded_json_value(output):
    with pytest.raises((ValueError, UnicodeError)):
        parse_output(output)


def test_program_output_keeps_json_types_and_whitespace():
    assert parse_output(b' \n {"a":[true,null,1,1.25]} \t') == {"a": [True, None, 1, 1.25]}
    assert parse_output(b"null") is None
    assert parse_output(b"0") == 0
    with pytest.raises(UnicodeError):
        validate_request("print(1)", "\ud800")


@pytest.mark.parametrize(
    "output",
    [
        b'{"value":{"weights":[1],"weights":[2]}}',
        b'{"value":1,"value":2}',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":{"weights":[1e999]}}',
        b'{"value":' + b"[" * 33 + b"0" + b"]" * 33 + b"}",
        b'{"value":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
        b'{"value":1,"score":100}',
        b"[]",
    ],
)
def test_function_envelopes_use_the_same_strict_decoder(output):
    with pytest.raises(ValueError):
        parse_output(output, envelope=True)


def test_function_envelope_does_not_consume_value_depth_budget():
    output = b"[" * 32 + b"0" + b"]" * 32
    assert parse_output(output) == parse_output(b'{"value":' + output + b"}", envelope=True)


def test_json_node_budget_is_independent_of_transport_limit(monkeypatch):
    # Pure decoder check, not sandbox execution evidence. The production byte
    # limit normally rejects this large flat document even earlier.
    monkeypatch.setattr("judge.stdio.OUTPUT", 100000)
    with pytest.raises(ValueError, match="structural"):
        parse_output(b"[" + b"0," * 20000 + b"0]")


@REAL
@pytest.mark.sandbox
def test_actual_function_cannot_forge_ambiguous_or_nonfinite_json():
    sandbox = configured_sandbox()
    sandbox.preflight()
    for name, source, verdict, _ in FUNCTION_PROBES:
        if name.startswith("strict-json/"):
            assert sandbox.execute(source, "solve", []).verdict == verdict, name


def test_gvisor_rejects_programs_before_container_control(monkeypatch):
    sandbox = DockerSandbox("python@sha256:" + "a" * 64)

    def forbidden(*args, **kwargs):
        pytest.fail("Unsupported program mode must never launch a container")

    monkeypatch.setattr(sandbox, "command", forbidden)
    with pytest.raises(SandboxUnavailable, match="Complete programs.*WASI"):
        sandbox.execute_program("print(1)", "")


@REAL
@pytest.mark.sandbox
def test_actual_complete_program_exit_io_and_isolation_probes():
    sandbox = configured_sandbox()
    identity = sandbox.preflight()
    assert "python-stdio-v1" in identity["evaluators"]
    for name, source, stdin, verdict, expected in PROBES:
        result = sandbox.execute_program(source, stdin)
        assert result.verdict == verdict, (name, result)
        if verdict == "ok":
            assert type(result.value) is type(expected) and result.value == expected, name


@REAL
@pytest.mark.sandbox
def test_actual_program_evaluator_owns_verdicts_and_hides_private_inputs():
    sandbox = configured_sandbox()
    sandbox.preflight()
    problem = {
        "language": "python",
        "evaluator": "python-stdio-v1",
        "output_format": "json",
        "comparison": {"kind": "numeric", "absolute": 1e-9, "relative": 1e-9},
        "tests": [
            {"name": "public", "public": True, "stdin": '{"x":4}', "expected": {"y": 5}},
            {
                "name": "private-input-canary",
                "public": False,
                "stdin": '{"x":-4}',
                "expected": {"y": -3},
            },
        ],
    }
    source = 'import json,sys\nx=json.load(sys.stdin)["x"]\nprint(json.dumps({"y":x+1}))'
    result = evaluate(problem, source, "submit", sandbox)
    assert result["verdict"] == "accepted" and result["passed"] == 2
    assert "private-input-canary" not in json.dumps(result)
    assert evaluate(problem, source, "run", sandbox)["total"] == 1
    assert evaluate(problem, 'print("null")', "submit", sandbox)["verdict"] == "wrong_answer"
    assert (
        evaluate(problem, 'print(\'{"verdict":"accepted","score":100}\')', "submit", sandbox)[
            "verdict"
        ]
        == "wrong_answer"
    )
    assert (
        evaluate(problem, "import sys\nprint('{\"y\":5}')\nsys.exit(4)", "submit", sandbox)[
            "verdict"
        ]
        == "runtime_error"
    )


@REAL
@pytest.mark.sandbox
def test_actual_program_boundary_bytes_and_host_files(tmp_path):
    sandbox = configured_sandbox()
    sandbox.preflight()
    source = "import json,sys\nprint(json.dumps(len(sys.stdin.buffer.read())))"
    payload = json.dumps({"source": source, "stdin": ""}, ensure_ascii=False).encode()
    stdin = "x" * (INPUT - len(payload))
    result = sandbox.execute_program(source, stdin)
    assert result.verdict == "ok" and result.value == len(stdin)
    with pytest.raises(SandboxUnavailable, match="Input exceeds"):
        sandbox.execute_program(source, stdin + "x")
    # Exactly OUTPUT bytes, including JSON quotes; no trailing newline.
    result = sandbox.execute_program(
        f'import sys\nsys.stdout.write("\\""+"x"*{OUTPUT - 2}+"\\"")', ""
    )
    assert result.verdict == "ok" and len(result.value) == OUTPUT - 2
    result = sandbox.execute_program(
        f'import sys\nsys.stdout.write("\\""+"x"*{OUTPUT - 1}+"\\"")', ""
    )
    assert result.verdict == "output_limit"
    result = sandbox.execute_program(
        f'import os,sys\nos.write(2,b"x")\nsys.stdout.write("\\""+"x"*{OUTPUT - 2}+"\\"")',
        "",
    )
    assert result.verdict == "output_limit"
    canary = tmp_path / "private-canary"
    canary.write_text("not sent to guest")
    source = 'import sys,json\npath=sys.stdin.read()\ntry:\n open(path).read()\n print("false")\nexcept OSError:\n print("true")'
    result = sandbox.execute_program(source, str(canary))
    assert result.verdict == "ok" and result.value is True
    assert canary.read_text() == "not sent to guest"


@REAL
@pytest.mark.sandbox
def test_actual_receipt_covers_function_and_program_modes(settings, tmp_path):
    settings.JUDGE_RECEIPT = str(tmp_path / "receipt.json")
    call_command("sandbox_check", stdout=io.StringIO())
    identity = configured_sandbox().preflight()
    check_receipt(identity)
    passed = json.loads(Path(settings.JUDGE_RECEIPT).read_text())["passed"]
    assert {name.split("/")[0] for name in passed} == {"python", "javascript", "python-stdio"}
