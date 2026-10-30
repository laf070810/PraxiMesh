"""Synthetic persisted records for disposable DB/UI tests, NEVER GPU acceptance."""

import copy
import hashlib
import uuid
from datetime import timedelta

from django.utils import timezone

from core.content import digest
from core.models import Problem, ProblemVersion, Submission
from core.resources import GPU_BUDGET_POLICY, reservation_units, utc_day
from judge.gpu_execution_contract import canonical_digest, environment_identity
from judge.gpu_policy import PROFILE, profile_digest
from judge.results import LABELS


def version_fixture(slug="gpu-result-fixture"):
    problem = Problem.objects.create(id=slug)
    data = {
        "id": slug,
        "revision": 1,
        "title": "合成 GPU 成绩页面",
        "summary": "Synthetic rendering/evidence fixture; no GPU execution.",
        "statement": "该题仅用于一次性测试数据库的界面检查。",
        "constraints": "合成记录，不代表实际 GPU 评测。",
        "outcomes": ["核对成绩展示"],
        "concepts": [],
        "difficulty": "基础",
        "tags": ["synthetic"],
        "starter": "// Synthetic UI fixture; not executed.",
        "reference": "PRIVATE_REFERENCE_SENTINEL",
        "hints": ["Synthetic hint."],
        "changelog": "Synthetic fixture only.",
        "license": "test-fixture",
        "attribution": [],
        "evaluator": "cuda-kernel-v1",
        "language": "cuda",
        "operation": "vector-add-f32-v1",
        "entrypoint": "solve",
        "comparison": {"kind": "float", "absolute": 1e-5, "relative": 1e-5},
        "tests": [
            {"name": "public", "public": True, "inputs": {"a": [1], "b": [2]}, "expected": [3]},
            {
                "name": "PROTECTED_CASE_SENTINEL",
                "public": False,
                "inputs": {"a": [2], "b": [4]},
                "expected": [6],
            },
        ],
    }
    version = ProblemVersion.objects.create(
        problem=problem, revision=1, digest=digest(data), data=data
    )
    problem.current = version
    problem.save(update_fields=["current"])
    return version


def environment_fixture():
    target = {
        "index": 0,
        "uuid": "GPU-11111111-2222-3333-4444-555555555555",
        "model": "Synthetic device; no hardware acceptance",
        "driver": "590.48.01",
        "architecture": [8, 6],
    }
    # Deliberately synthetic artifact hashes; never a startup receipt or proof.
    identity = {
        "image_archive_sha256": "a" * 64,
        "image_manifest": "sha256:" + "b" * 64,
        "image_id": "sha256:" + "c" * 64,
        "runsc_sha256": "d" * 64,
        "runsc_release": "release-20261026.0",
        "podman_version": "5.8.2",
        "nvrtc_archive_sha256": "e" * 64,
        "nvrtc_libraries": {
            "libnvrtc.so.13.1.115": "a" * 64,
            "libnvrtc-builtins.so.13.1.115": "b" * 64,
        },
        "harness_sha256": {
            name: "c" * 64
            for name in ("__init__.py", "gpu_contract.py", "gpu_runtime.py", "results.py")
        },
    }
    return environment_identity(target, identity)


def result_fixture(mode="submit", verdict="accepted", *, batch_failed=False):
    total = 2 if mode == "submit" else 1
    result = {
        "verdict": verdict,
        "label": LABELS[verdict],
        "total": total,
        "passed": total if verdict == "accepted" or batch_failed else 1,
        "public_cases": [{"name": "public", "verdict": "accepted", "label": LABELS["accepted"]}],
        "compile_initialization_ms": 12.5,
    }
    if mode == "submit" and verdict == "accepted":
        result["performance"] = {
            "profile": PROFILE["id"],
            "profile_digest": profile_digest(),
            "measurement": PROFILE["measurement"],
            "warmup_batches": PROFILE["warmup_batches"],
            "sample_batches": PROFILE["sample_batches"],
            "batch_repetitions": PROFILE["batch_repetitions"],
            "samples_ms": [1.0, 2.0, 3.0, 4.0, 5.0],
            "median_batch_ms": 3.0,
        }
    return result


def job_fixture(user, version, *, mode="submit", verdict="accepted", batch_failed=False):
    environment = environment_fixture()
    fingerprint = canonical_digest(environment)
    environment.update(language="cuda", evaluator="cuda-kernel-v1")
    source = "PRIVATE_LEARNER_SOURCE_SENTINEL (synthetic; never executed)"
    now = timezone.now()
    job = Submission.objects.create(
        user=user,
        version=version,
        source=source,
        source_digest=hashlib.sha256(source.encode()).hexdigest(),
        mode=mode,
        status="finished",
        verdict=verdict,
        environment=environment,
        evaluation_fingerprint=fingerprint,
        result=result_fixture(mode, verdict, batch_failed=batch_failed),
        started_at=now - timedelta(milliseconds=500),
        execution_started_at=now - timedelta(milliseconds=400),
        finished_at=now,
        lease_token=uuid.uuid4(),
        budget_policy=GPU_BUDGET_POLICY,
        budget_state="consumed",
        budget_units=reservation_units(version, mode),
        budget_day=utc_day(now),
    )
    Submission.objects.filter(pk=job.pk).update(created_at=now - timedelta(seconds=1))
    job.refresh_from_db()
    return job


def rebind(job):
    """Synthetic record mutation for consistency tests, not runtime attestation."""
    environment = copy.deepcopy(job.environment)
    environment.pop("language")
    environment.pop("evaluator")
    job.evaluation_fingerprint = canonical_digest(environment)
