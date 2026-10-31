#!/usr/bin/env python3
"""Validate candidate CUDA programs through an isolated site's normal API/worker.

No host compilation, local execution, receipt creation or direct database access.
The operator supplies a disposable test site/account; this creates real jobs.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from judge.gpu_contract import EVALUATOR, compile_request  # noqa: E402
from judge.gpu_execution_contract import validate_result  # noqa: E402
from scripts.practice import (  # noqa: E402
    TOKEN,
    Client,
    PracticeError,
    read_json,
)
from scripts.practice import (
    validate_package as validate_public_package,
)


def require(condition):
    if not condition:
        raise PracticeError("GPU content acceptance evidence is incomplete or mismatched.")


def sha256(source):
    return hashlib.sha256(source.encode()).hexdigest()


def review_inputs(package, mistakes_path):
    """Select all CUDA problems and require distinct, bounded reviewed wrong sources."""
    problems = [problem for problem in package["problems"] if problem["evaluator"] == EVALUATOR]
    path = Path(mistakes_path)
    require(path.stat().st_size <= 1_000_000)
    mistakes = read_json(path.read_bytes())
    require(bool(problems) and type(mistakes) is dict)
    require(set(mistakes) == {problem["id"] for problem in problems})
    for problem in problems:
        variants = mistakes[problem["id"]]
        require(type(variants) is list and 2 <= len(variants) <= 8)
        names, sources = set(), {problem["reference"]}
        for variant in variants:
            require(type(variant) is dict and set(variant) == {"mistake", "source"})
            name = variant["mistake"]
            require(type(name) is str and 1 <= len(name.strip()) <= 200 and name not in names)
            compile_request(variant["source"], problem["operation"])
            require(variant["source"] not in sources)
            names.add(name)
            sources.add(variant["source"])
    return problems, mistakes


def scenarios(problem, variants):
    yield "public-reference", problem["reference"], "run", "accepted"
    yield "formal-reference", problem["reference"], "submit", "accepted"
    for index, variant in enumerate(variants):
        yield f"incorrect-{index + 1}", variant["source"], "submit", "wrong_answer"
    yield "recovery-reference", problem["reference"], "submit", "accepted"


def verify_result(response, problem, version_id, item, fingerprint):
    """Check public worker evidence, never accept a client-provided runtime receipt."""
    from core.content import digest
    from core.resources import GPU_BUDGET_POLICY, GPU_SLOT_SECONDS

    require(type(response) is dict)
    for field, expected in {
        "id": item["submission_id"],
        "problem": problem["id"],
        "version_id": version_id,
        "revision": problem["revision"],
        "source_digest": digest(problem),
        "mode": item["mode"],
        "status": "finished",
        "verdict": item["expected_verdict"],
        "formal_result": item["mode"] == "submit",
    }.items():
        require(type(response.get(field)) is type(expected) and response[field] == expected)
    actual_fingerprint = response.get("evaluation_fingerprint")
    require(
        type(actual_fingerprint) is str
        and re.fullmatch(r"[a-f0-9]{64}", actual_fingerprint) is not None
        and (fingerprint is None or actual_fingerprint == fingerprint)
    )
    label = response.get("evaluation_environment")
    require(
        type(label) is str
        and label.startswith("CUDA / ")
        and label.endswith(" / sha256:" + actual_fingerprint)
        and len(label) <= 512
    )
    reservation = response.get("resource_reservation")
    require(
        type(reservation) is dict
        and reservation.get("policy") == GPU_BUDGET_POLICY
        and reservation.get("state") == "consumed"
        and type(reservation.get("units")) is int
        and reservation["units"] == GPU_SLOT_SECONDS[item["mode"]]
        and type(reservation.get("execution_started_at")) is str
        and bool(reservation["execution_started_at"])
    )
    result = response.get("result")
    validate_result(result, problem, item["mode"])
    require(result["verdict"] == item["expected_verdict"])
    return actual_fingerprint


def replay(client, token, problems, mistakes, report, save, *, timeout=600):
    """Journal before each POST. Never retry uncertain writes or bypass admission."""
    from core.content import digest

    fingerprint = None
    # Check every version before creating any job. Later publication changes are
    # still rejected by the normal API's version-pinned submission admission.
    versions = {}
    for problem in problems:
        public = validate_public_package(client.request(f"/api/v1/problems/{problem['id']}/"))
        require(
            public["source_digest"] == digest(problem)
            and public["problem"]["id"] == problem["id"]
            and public["problem"]["revision"] == problem["revision"]
        )
        versions[problem["id"]] = public["version_id"]
    for problem in problems:
        for name, source, mode, verdict in scenarios(problem, mistakes[problem["id"]]):
            item = {
                "problem": problem["id"],
                "revision": problem["revision"],
                "source_digest": digest(problem),
                "version_id": versions[problem["id"]],
                "scenario": name,
                "source_sha256": sha256(source),
                "mode": mode,
                "expected_verdict": verdict,
                "request_key": str(uuid.uuid4()),
                "submission_id": None,
                "state": "posting",
            }
            report["jobs"].append(item)
            save()
            posted = client.request(
                f"/api/v1/problems/{problem['id']}/submissions/",
                token=token,
                key=item["request_key"],
                payload={"version_id": item["version_id"], "source": source, "mode": mode},
            )
            require(type(posted) is dict and type(posted.get("id")) is str)
            identifier = uuid.UUID(posted["id"])
            require(str(identifier) == posted["id"] and posted.get("created") is True)
            item.update(submission_id=str(identifier), state="waiting")
            save()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                response = client.request(f"/api/v1/submissions/{identifier}/", token=token)
                require(type(response) is dict and response.get("id") == str(identifier))
                status = response.get("status")
                require(status in {"queued", "running", "finished"})
                if status == "finished":
                    fingerprint = verify_result(
                        response, problem, item["version_id"], item, fingerprint
                    )
                    item.update(
                        state="verified",
                        result=response["result"],
                        evaluation_fingerprint=fingerprint,
                        evaluation_environment=response["evaluation_environment"],
                        formal_result=response["formal_result"],
                    )
                    save()
                    break
                time.sleep(min(2, max(0, deadline - time.monotonic())))
            else:
                raise PracticeError("Observation timed out; the recorded job may still be running.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--incorrect", required=True, type=Path)
    parser.add_argument("--server", required=True)
    parser.add_argument("--ca-file")
    parser.add_argument("--allow-local-http", action="store_true")
    parser.add_argument(
        "--isolated-test-site",
        required=True,
        action="store_true",
        help="Confirm this is a disposable test site/account; real submissions consume its budget.",
    )
    parser.add_argument("--output", required=True, type=Path, help="New private report directory.")
    parser.add_argument(
        "--timeout", type=int, default=600, help="Observation seconds per job, 1-1800."
    )
    args = parser.parse_args(argv)
    if not 1 <= args.timeout <= 1800:
        parser.error("--timeout must be between 1 and 1800 seconds.")
    token = os.environ.get("PRAXIMESH_API_TOKEN", "")
    if not TOKEN.fullmatch(token):
        parser.error("Set a test account's PRAXIMESH_API_TOKEN with submit and result scopes.")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
    import django

    django.setup()
    from core.content import digest, load_package
    from core.resources import GPU_SLOT_SECONDS

    # All local and origin checks precede report creation and network activity.
    try:
        package = load_package(args.package)
        problems, mistakes = review_inputs(package, args.incorrect)
        client = Client(args.server, allow_local_http=args.allow_local_http, ca_file=args.ca_file)
        args.output.mkdir(mode=0o700)
    except (ValueError, OSError, RecursionError) as error:
        print("Acceptance preparation failed: " + type(error).__name__, file=sys.stderr)
        return 1
    report = {
        "format": 1,
        "kind": "cuda-content-api-acceptance",
        "status": "running",
        "started_at": datetime.now(UTC).isoformat(),
        "server": client.origin,
        "package_digest": digest(package),
        "incorrect_digest": digest(mistakes),
        "reservation_seconds": sum(
            GPU_SLOT_SECONDS[mode]
            for problem in problems
            for _, _, mode, _ in scenarios(problem, mistakes[problem["id"]])
        ),
        "jobs": [],
    }

    def save():
        temporary = args.output / ".result.json.tmp"
        # The newly owned directory is private; journal replacements are atomic.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as target:
            json.dump(report, target, ensure_ascii=False, indent=2, allow_nan=False)
            target.write("\n")
            target.flush()
            os.fsync(target.fileno())
        temporary.replace(args.output / "result.json")

    save()
    try:
        replay(client, token, problems, mistakes, report, save, timeout=args.timeout)
    except (Exception, KeyboardInterrupt) as error:
        report.update(status="failed", failure=type(error).__name__)
    else:
        report["status"] = "passed"
    report["finished_at"] = datetime.now(UTC).isoformat()
    save()
    print(f"CUDA content acceptance {report['status']}: {args.output / 'result.json'}")
    if report["status"] != "passed":
        print(
            "Keep the test site and journal; recorded jobs may still be running.", file=sys.stderr
        )
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
