"""Protocol doubles test the remote acceptance tool, never CUDA correctness."""

import copy
import json
import stat
import uuid
from types import SimpleNamespace

import pytest
from test_cuda_content import ROOT
from test_cuda_content import complete_cuda_package as complete_cuda_package
from test_cuda_content import small_cuda_package as small_cuda_package

from core.content import digest, public_problem
from judge.gpu_policy import PROFILE, profile_digest
from judge.results import LABELS
from judge.sandbox import SandboxUnavailable
from scripts import gpu_content_acceptance as acceptance
from scripts.practice import PracticeError


class ProtocolFixture:
    origin = "https://gpu.example.test"

    def __init__(self, problems):
        self.problems = problems
        self.posts = []
        self.jobs = {}
        self.public_mutation = lambda response: None
        self.result_mutation = lambda response: None

    def request(self, path, *, payload=None, **kwargs):
        if path.startswith("/api/v1/problems/"):
            slug = path.split("/")[4]
            problem = next(p for p in self.problems if p["id"] == slug)
            version = self.problems.index(problem) + 1
            if payload is None:
                response = {
                    "format": 1,
                    "version_id": version,
                    "source_digest": digest(problem),
                    "problem": public_problem(SimpleNamespace(data=problem)),
                    "formal_result": False,
                    "public_tests": [
                        {k: v for k, v in case.items() if k != "public"}
                        for case in problem["tests"]
                        if case["public"]
                    ],
                }
                self.public_mutation(response)
                return response
            assert path == f"/api/v1/problems/{slug}/submissions/"
            assert payload["version_id"] == version
            assert kwargs["key"]
            self.posts.append(copy.deepcopy(payload))
            mode = payload["mode"]
            verdict = "accepted" if payload["source"] == problem["reference"] else "wrong_answer"
            total = sum(mode == "submit" or case["public"] for case in problem["tests"])
            result = {
                "verdict": verdict,
                "label": LABELS[verdict],
                "total": total,
                "passed": total if verdict == "accepted" else 0,
                "public_cases": [
                    {"name": case["name"], "verdict": verdict, "label": LABELS[verdict]}
                    for case in problem["tests"]
                    if case["public"]
                ],
                "compile_initialization_ms": 1,
            }
            if mode == "submit" and verdict == "accepted":
                result["performance"] = {
                    "profile": PROFILE["id"],
                    "profile_digest": profile_digest(PROFILE),
                    **{
                        key: PROFILE[key]
                        for key in (
                            "measurement",
                            "warmup_batches",
                            "sample_batches",
                            "batch_repetitions",
                        )
                    },
                    "samples_ms": [1.0] * PROFILE["sample_batches"],
                    "median_batch_ms": 1.0,
                }
            identifier = str(uuid.uuid4())
            self.jobs[identifier] = {
                "id": identifier,
                "problem": slug,
                "version_id": version,
                "revision": problem["revision"],
                "source_digest": digest(problem),
                "mode": mode,
                "status": "finished",
                "verdict": verdict,
                "result": result,
                "evaluation_fingerprint": "a" * 64,
                "evaluation_environment": "CUDA / Protocol fixture / 1 / profile / sha256:"
                + "a" * 64,
                "formal_result": mode == "submit",
                "resource_reservation": {
                    "policy": "gpu-slot-seconds-v1",
                    "state": "consumed",
                    "units": 90 if mode == "run" else 180,
                    "execution_started_at": "2026-10-31T00:00:00+00:00",
                },
            }
            return {"id": identifier, "created": True}
        response = copy.deepcopy(self.jobs[path.split("/")[4]])
        self.result_mutation(response)
        return response


@pytest.fixture
def replay_inputs(small_cuda_package):
    problems, mistakes = acceptance.review_inputs(
        small_cuda_package, ROOT / "tests/cuda_incorrect_solutions.json"
    )
    return problems, mistakes


def test_protocol_replay_pins_every_job_and_records_no_sources_or_token(replay_inputs):
    problems, mistakes = replay_inputs
    client = ProtocolFixture(problems)
    report, snapshots = {"jobs": []}, []
    acceptance.replay(
        client,
        "private-token-fixture",
        problems,
        mistakes,
        report,
        lambda: snapshots.append(copy.deepcopy(report)),
    )
    assert len(client.posts) == len(report["jobs"]) == 12
    assert all(item["state"] == "verified" for item in report["jobs"])
    assert len({item["request_key"] for item in report["jobs"]}) == 12
    assert len({item["submission_id"] for item in report["jobs"]}) == 12
    assert all(item["formal_result"] is (item["mode"] == "submit") for item in report["jobs"])
    assert all(
        item["source_digest"] == digest(problems[index // 6])
        for index, item in enumerate(report["jobs"])
    )
    assert snapshots[0]["jobs"][0]["state"] == "posting"
    assert snapshots[0]["jobs"][0]["submission_id"] is None
    assert "private-token-fixture" not in json.dumps(snapshots)
    assert all('"source"' not in json.dumps(snapshot) for snapshot in snapshots)


def test_version_mismatch_before_any_submission(replay_inputs):
    problems, mistakes = replay_inputs
    client = ProtocolFixture(problems)
    client.public_mutation = lambda response: response.update(source_digest="b" * 64)
    with pytest.raises(PracticeError):
        acceptance.replay(client, "token", problems, mistakes, {"jobs": []}, lambda: None)
    assert not client.posts


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.update(version_id=True),
        lambda r: r.update(formal_result=True),
        lambda r: r.update(mode="submit"),
        lambda r: r.update(evaluation_fingerprint="unknown"),
        lambda r: r.update(evaluation_environment="CPU / WASI"),
        lambda r: r.update(status="cancelled"),
        lambda r: r["resource_reservation"].update(state="reserved"),
        lambda r: r["resource_reservation"].update(units=True),
        lambda r: r["result"].update(compile_initialization_ms=0),
        lambda r: r["result"].update(private_test="must not be journaled"),
        lambda r: r["result"].update(passed=0),
    ],
)
def test_invalid_terminal_evidence_stops_without_next_job(replay_inputs, mutate):
    problems, mistakes = replay_inputs
    client = ProtocolFixture(problems)
    client.result_mutation = mutate
    report = {"jobs": []}
    with pytest.raises((PracticeError, SandboxUnavailable)):
        acceptance.replay(client, "token", problems, mistakes, report, lambda: None)
    assert len(client.posts) == len(report["jobs"]) == 1
    assert report["jobs"][0]["state"] == "waiting"
    assert "result" not in report["jobs"][0]


def test_environment_change_between_jobs_is_not_comparable(replay_inputs):
    problems, mistakes = replay_inputs
    client = ProtocolFixture(problems)

    def changed(response):
        if len(client.posts) > 1:
            response["evaluation_fingerprint"] = "b" * 64
            response["evaluation_environment"] = response["evaluation_environment"].replace(
                "a" * 64, "b" * 64
            )

    client.result_mutation = changed
    report = {"jobs": []}
    with pytest.raises(PracticeError):
        acceptance.replay(client, "token", problems, mistakes, report, lambda: None)
    assert len(client.posts) == 2
    assert report["jobs"][0]["state"] == "verified"
    assert report["jobs"][1]["state"] == "waiting"


def test_uncertain_post_is_journaled_but_never_automatically_retried(replay_inputs):
    problems, mistakes = replay_inputs
    client = ProtocolFixture(problems)
    original = client.request

    def disconnected(path, **kwargs):
        response = original(path, **kwargs)
        if kwargs.get("payload") is not None:
            raise PracticeError("Connection lost after POST")
        return response

    client.request = disconnected
    report = {"jobs": []}
    with pytest.raises(PracticeError):
        acceptance.replay(client, "token", problems, mistakes, report, lambda: None)
    assert len(client.posts) == 1
    assert report["jobs"][0]["request_key"]
    assert report["jobs"][0]["submission_id"] is None


def test_observation_timeout_does_not_cancel_or_restart_job(replay_inputs, monkeypatch):
    problems, mistakes = replay_inputs
    client = ProtocolFixture(problems)
    client.result_mutation = lambda response: response.update(status="running")
    clock = iter([0, 0, 2, 2])
    monkeypatch.setattr(acceptance.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(acceptance.time, "sleep", lambda _: None)
    report = {"jobs": []}
    with pytest.raises(PracticeError, match="may still be running"):
        acceptance.replay(client, "token", problems, mistakes, report, lambda: None, timeout=1)
    assert len(client.posts) == len(client.jobs) == 1
    assert report["jobs"][0]["submission_id"]


def test_main_persists_private_failed_journal_without_token(
    replay_inputs, small_cuda_package, tmp_path, monkeypatch
):
    problems, _ = replay_inputs
    package_path = tmp_path / "package.json"
    package_path.write_text(json.dumps(small_cuda_package))
    client = ProtocolFixture(problems)
    client.result_mutation = lambda response: response.update(formal_result=True)
    monkeypatch.setattr(acceptance, "Client", lambda *args, **kwargs: client)
    token = "pm_" + "a" * 32 + "." + "b" * 43
    monkeypatch.setenv("PRAXIMESH_API_TOKEN", token)
    output = tmp_path / "report"
    result = acceptance.main(
        [
            "--package",
            str(package_path),
            "--incorrect",
            str(ROOT / "tests/cuda_incorrect_solutions.json"),
            "--server",
            client.origin,
            "--isolated-test-site",
            "--output",
            str(output),
        ]
    )
    assert result == 1
    report_path = output / "result.json"
    report = json.loads(report_path.read_text())
    assert report["status"] == "failed" and report["reservation_seconds"] == 1980
    assert len(report["jobs"]) == 1
    assert token not in report_path.read_text()
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
    assert not (output / ".result.json.tmp").exists()
