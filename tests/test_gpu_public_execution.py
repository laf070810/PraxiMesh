"""Public-only GPU data and trusted comparison; sessions are protocol models."""

import copy

import pytest

from judge import container_kernel_flow, gpu
from judge import gpu_task_protocol as protocol
from judge.container_kernel_protocol import checked_completion, validate_request
from judge.gpu_contract import project_public_problem
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable
from tests.test_container_adapter import configured as configured
from tests.test_container_driver import bundle as bundle
from tests.test_container_kernel_flow import flow as flow
from tests.test_gpu_adapter import problem


@pytest.fixture
def public_problem():
    return project_public_problem(problem())


def test_downloaded_public_cases_have_a_separate_execution_request(public_problem):
    with pytest.raises(ValueError):
        protocol.task_request(public_problem, "opaque source", "run")
    with pytest.raises(ValueError):
        protocol.task_request(public_problem, "opaque source", "submit")
    request = protocol.public_task_request(public_problem, "opaque source")
    assert request["event"] == "execute-public" and request["mode"] == "run"
    assert validate_request(request) == request
    request["problem"]["tests"][0]["inputs"]["a"][0] = 200
    assert public_problem["tests"][0]["inputs"]["a"][0] != 200


@pytest.mark.parametrize("change", ["protected", "reference", "command", "empty", "false-expected"])
def test_public_request_rejects_private_or_repurposed_payload(public_problem, change):
    if change == "protected":
        public_problem["tests"].append(copy.deepcopy(problem()["tests"][-1]))
    elif change == "empty":
        public_problem["tests"] = []
    elif change == "false-expected":
        public_problem["tests"][0]["expected"] = [1234.0] * len(
            public_problem["tests"][0]["expected"]
        )
    else:
        public_problem[change] = "private fixture"
    with pytest.raises(ValueError):
        protocol.public_task_request(public_problem, "opaque source")


@pytest.mark.parametrize("mode", ["submit", "public", "", None])
def test_public_protocol_cannot_select_formal_mode(public_problem, mode):
    request = protocol.public_task_request(public_problem, "opaque source")
    request["mode"] = mode
    with pytest.raises(ValueError):
        validate_request(request)


@pytest.mark.parametrize("correct", [True, False])
def test_public_comparison_runs_only_public_inputs_without_performance(public_problem, correct):
    calls = []

    class Session:
        mode = "run"

        def compile(self, source, operation):
            calls.append(("compile", source, operation))
            return 1.0

        def execute(self, case, *, batch=False):
            assert batch is False and case["public"] is True
            calls.append(("execute", case["name"]))
            return copy.deepcopy(case["expected"]) if correct else [999.0] * len(
                case["expected"]
            ), 0.1

        def finish(self):
            calls.append(("finish",))

    result = gpu.evaluate_public_kernel(public_problem, "opaque source", Session())
    assert result["total"] == len(public_problem["tests"])
    assert result["verdict"] == ("accepted" if correct else "wrong_answer")
    assert "performance" not in result
    assert len([c for c in calls if c[0] == "execute"]) == result["total"]
    assert calls[-1] == ("finish",)


def public_completion(request):
    names = [case["name"] for case in request["problem"]["tests"]]
    return {
        "event": "kernel-completed",
        "protocol": "praximesh-container-kernel-v1",
        "recovered": True,
        "ready": False,
        "formal_result": False,
        "cleanup": [
            {"id": name, "stopped": True, "removed": True} for name in ("task", "device-recovery")
        ],
        "result": {
            "verdict": "accepted",
            "label": LABELS["accepted"],
            "passed": len(names),
            "total": len(names),
            "compile_initialization_ms": 1.0,
            "public_cases": [
                {"name": name, "verdict": "accepted", "label": LABELS["accepted"]} for name in names
            ],
        },
    }


def test_public_completion_is_not_a_server_task_completion(public_problem):
    request = protocol.public_task_request(public_problem, "opaque source")
    completed = public_completion(request)
    assert checked_completion(completed, request)["formal_result"] is False
    with pytest.raises(SandboxUnavailable):
        checked_completion(completed, protocol.task_request(problem(), "opaque source", "run"))
    completed.pop("formal_result")
    with pytest.raises(SandboxUnavailable):
        checked_completion(completed, request)


@pytest.mark.parametrize(
    "change", ["formal", "zero", "performance", "incomplete-cleanup", "false-count"]
)
def test_public_completion_requires_unscored_result_and_verified_cleanup(public_problem, change):
    request = protocol.public_task_request(public_problem, "opaque source")
    completed = public_completion(request)
    if change == "formal":
        completed["formal_result"] = True
    elif change == "zero":
        completed["formal_result"] = 0
    elif change == "performance":
        completed["result"]["performance"] = {"median_batch_ms": 0.001}
    elif change == "false-count":
        completed["result"]["passed"] = 0
    else:
        completed["cleanup"].pop()
    with pytest.raises(SandboxUnavailable):
        checked_completion(completed, request)


@pytest.mark.parametrize("recovery_failure", [False, True])
def test_public_flow_withholds_result_until_original_guest_and_recovery_finish(
    flow, public_problem, monkeypatch, recovery_failure
):
    request = protocol.public_task_request(public_problem, "opaque source")
    flow.incoming[0] = request

    def evaluate(problem, source, session):
        assert problem == public_problem and source == request["source"]
        flow.events.append("public-compared")
        return public_completion(request)["result"]

    monkeypatch.setattr(container_kernel_flow, "evaluate_public_kernel", evaluate)
    if recovery_failure:

        def fail(_runtime):
            raise SandboxUnavailable("fixed original recovery failure")

        monkeypatch.setattr(container_kernel_flow, "recover_device", fail)
        with pytest.raises(SandboxUnavailable):
            flow.run()
        assert not any(row["event"] == "kernel-completed" for row in flow.sent)
        assert "task-stopped" in flow.events and "runtime-closed" in flow.events
    else:
        flow.run()
        assert flow.sent[-1]["formal_result"] is False
        events = flow.events
        assert events.index("public-compared") < events.index("task-stopped")
        assert events.index("task-stopped") < events.index("recovered")
        assert events.index("recovered") < events.index("send:kernel-completed")


@pytest.mark.parametrize("scope_marker", [False, True, None, 0])
def test_public_adapter_preserves_local_scope_after_original_task_cleanup(
    configured, public_problem, scope_marker
):
    adapter = configured.adapter
    original = configured.run

    def run(task, request, **kwargs):
        assert request["event"] == "execute-public" and request["mode"] == "run"
        completed = original(task, request, **kwargs)
        completed["formal_result"] = scope_marker
        completed["result"] = public_completion(request)["result"]
        return completed

    configured.run = run
    task = adapter.task(configured.control.guard, "job", "reservation")

    def evaluate():
        return adapter.evaluate_public(
            task,
            deadline=100.0,
            problem=public_problem,
            source="opaque source",
            on_tick=lambda: None,
            on_begin=lambda: configured.events.append("began"),
        )

    if scope_marker is False:
        result = evaluate()
        assert result["formal_result"] is False and "performance" not in result["result"]
        assert result["environment"] == adapter.environment
        assert configured.events.index("began") < configured.events.index(
            "runtime-recovery-and-cleanup"
        )
    else:
        with pytest.raises(SandboxUnavailable):
            evaluate()
