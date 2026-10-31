"""Modeled startup reports and sessions, never CUDA or readiness evidence."""

import copy
from types import SimpleNamespace

import pytest

from judge import container_kernel_checks as checks
from judge.container_kernel_protocol import (
    PROTOCOL,
    checked_completion,
    expected_cleanup,
    validate_request,
)
from judge.gpu_kernel_checks import CHECKS, check_request
from judge.sandbox import SandboxUnavailable
from tests.test_gpu_kernel_checks import check_report as host_report

REQUEST = check_request("public-run", "a" * 32)


def check_report(request=REQUEST):
    value = host_report(request)
    value["scope"] = checks.SCOPE
    value["cleanup"].update(scope="runsc-guest-v1", id=checks.LABEL, removed=True)
    if request["name"] in checks.INTERRUPTIONS:
        value["interruption_observation"] = {
            "scope": "runsc-guest-v1",
            "id": checks.LABEL,
            "status": "running",
            "attach_alive": True,
        }
    return value


@pytest.mark.parametrize("name", CHECKS)
def test_fixed_cases_keep_outcomes_but_require_nested_cleanup(name):
    request = check_request(name, "b" * 32)
    assert validate_request(request) == request
    checks.validate_check_report(check_report(request), request)
    with pytest.raises(SandboxUnavailable):
        checks.validate_check_report(host_report(request), request)


@pytest.mark.parametrize(
    "defect",
    [
        "scope",
        "challenge",
        "extra",
        "id",
        "missing",
        "running",
        "exit",
        "removed",
        "stopped",
        "compile",
    ],
)
def test_incomplete_or_unmatched_startup_reports_fail_closed(defect):
    value = check_report()
    if defect == "scope":
        value["scope"] = "gpu-startup-v1"
    elif defect == "challenge":
        value["challenge"] = "b" * 32
    elif defect == "extra":
        value["ready"] = True
    elif defect == "id":
        value["cleanup"]["id"] = "device-recovery"
    elif defect == "missing":
        value["cleanup"].pop("scope")
    elif defect == "running":
        value["cleanup"]["running_before_cleanup"] = 1
    elif defect == "exit":
        value["cleanup"]["exit_code"] = False
    elif defect in {"removed", "stopped"}:
        value["cleanup"][defect] = 1
    else:
        value["result"].pop("compile_initialization_ms")
    with pytest.raises(SandboxUnavailable):
        checks.validate_check_report(value, REQUEST)


@pytest.mark.parametrize("name", ["cancel", "attach-death", "module-memory-limit"])
def test_interruption_and_driver_memory_require_the_original_observation(name):
    request = check_request(name, "a" * 32)
    report = check_report(request)
    if name == "module-memory-limit":
        report["cleanup"]["exit_code"] = 137
    else:
        report["interruption_observation"]["status"] = "stopped"
    with pytest.raises(SandboxUnavailable):
        checks.validate_check_report(report, request)


@pytest.mark.parametrize("field", ["scope", "id", "attach_alive", "missing"])
def test_already_stopped_guest_still_requires_valid_pre_interruption_observation(field):
    request = check_request("cancel", "a" * 32)
    report = check_report(request)
    report["cleanup"]["running_before_cleanup"] = False
    checks.validate_check_report(report, request)
    if field == "missing":
        report.pop("interruption_observation")
    else:
        report["interruption_observation"][field] = 1 if field == "attach_alive" else "foreign"
    with pytest.raises(SandboxUnavailable):
        checks.validate_check_report(report, request)


@pytest.mark.parametrize("field", ["source", "problem", "ready"])
def test_named_maintenance_cannot_accept_caller_programs_or_readiness(field):
    request = {**REQUEST, field: "injected"}
    with pytest.raises(ValueError):
        validate_request(request)


def test_completion_has_check_evidence_and_independent_recovery_not_submission_result():
    value = {
        "event": "kernel-completed",
        "protocol": PROTOCOL,
        "recovered": True,
        "ready": False,
        "cleanup": expected_cleanup(REQUEST),
        "report": check_report(),
    }
    assert checked_completion(value, REQUEST) == value
    value["cleanup"].pop()
    with pytest.raises(SandboxUnavailable, match="cleanup"):
        checked_completion(value, REQUEST)
    value["cleanup"] = expected_cleanup(REQUEST)
    value["result"] = value["report"]["result"]
    with pytest.raises(SandboxUnavailable):
        checked_completion(value, REQUEST)


@pytest.fixture
def runtime(monkeypatch):
    events = []
    original = {}
    state = SimpleNamespace(
        error=None, stop=True, evidence=check_report()["cleanup"], returncode=None
    )

    class Session:
        process = SimpleNamespace(poll=lambda: state.returncode)

        def __enter__(self):
            return self

        def cleanup(self):
            events.append("cleanup")
            original.update(closed=state.stop, cleanup_evidence=copy.deepcopy(state.evidence))
            return {"id": checks.LABEL, "stopped": True, "removed": True}

        def __exit__(self, *_error):
            self.cleanup()

    def evaluate(problem, source, mode, _session):
        case = checks.case_for("public-run")
        assert (problem, source, mode) == (case["problem"], case["source"], case["mode"])
        events.append("evaluate")
        if state.error:
            raise state.error
        return check_report()["result"]

    value = SimpleNamespace(
        tick=lambda: events.append("tick"),
        kernel_session=lambda *_a, **_kw: Session(),
        containers={checks.LABEL: original},
    )
    monkeypatch.setattr(checks, "evaluate_kernel", evaluate)
    return value, state, events


@pytest.mark.parametrize("defect", [None, "stopped", "attach-dead", "replaced"])
def test_interruption_observes_original_live_guest_before_trigger(runtime, monkeypatch, defect):
    value, state, events = runtime
    request = check_request("cancel", "a" * 32)
    value.inspect = lambda label: {"status": "stopped" if defect == "stopped" else "running"}
    if defect == "attach-dead":
        state.returncode = 0

    def interrupt(_runtime, _session, name, *, before_interrupt):
        if defect == "replaced":
            value.containers[checks.LABEL] = {}
        before_interrupt()
        events.append("trigger")
        state.evidence["running_before_cleanup"] = False
        return name

    monkeypatch.setattr(checks, "interrupt_session", interrupt)
    if defect:
        with pytest.raises(SandboxUnavailable):
            checks.check_kernel(value, request)
        assert "trigger" not in events
    else:
        report = checks.check_kernel(value, request)
        assert report["interruption_observation"]["status"] == "running"
        assert report["cleanup"]["running_before_cleanup"] is False
        assert events.index("trigger") < events.index("cleanup")


def test_report_is_derived_after_same_guest_cleanup(runtime):
    value, state, events = runtime
    report = checks.check_kernel(value, REQUEST)
    assert report == check_report()
    assert events.index("evaluate") < events.index("cleanup") < len(events) - 1
    state.evidence["removed"] = False
    assert report["cleanup"]["removed"] is True


@pytest.mark.parametrize(
    "defect", ["not-closed", "missing-evidence", "foreign-evidence", "platform-error"]
)
def test_failed_cleanup_or_execution_never_returns_a_check(runtime, defect):
    value, state, events = runtime
    if defect == "not-closed":
        state.stop = False
    elif defect == "missing-evidence":
        state.evidence = None
    elif defect == "foreign-evidence":
        state.evidence["id"] = "foreign"
    else:
        state.error = SandboxUnavailable("fixed platform error")
    with pytest.raises(SandboxUnavailable):
        checks.check_kernel(value, REQUEST)
    assert "cleanup" in events


def test_replaced_guest_record_is_not_claimed_for_cleanup(runtime, monkeypatch):
    value, _state, events = runtime

    def replaced(*_args):
        value.containers[checks.LABEL] = {}
        return check_report()["result"]

    monkeypatch.setattr(checks, "evaluate_kernel", replaced)
    with pytest.raises(SandboxUnavailable, match="identity changed"):
        checks.check_kernel(value, REQUEST)
    assert "cleanup" not in events
