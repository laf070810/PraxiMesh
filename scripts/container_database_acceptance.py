"""Actual PostgreSQL and fixed CPU containers for database-bound recovery; no GPU."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from judge.container_runtime_service import SOURCES, contract_digest  # noqa: E402
from judge.container_service import LocalEngine  # noqa: E402
from judge.gpu_policy import execution_digest  # noqa: E402
from judge.sandbox import policy_digest  # noqa: E402
from scripts.container_journal_acceptance import save  # noqa: E402
from scripts.container_runtime_acceptance import build_image  # noqa: E402

EXTRA = (
    "core/gpu_container_recovery.py",
    "core/gpu_identity.py",
    "core/gpu_startup.py",
    "core/gpu_readiness.py",
    "core/gpu_worker.py",
    "core/management/commands/gpu_worker.py",
    "scripts/container_database_acceptance.py",
    "tests/test_gpu_container_database_integration.py",
    "tests/test_gpu_container_recovery.py",
    "tests/test_container_journal.py",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker-socket", required=True)
    parser.add_argument("--runtime-archive", required=True, type=Path)
    options = parser.parse_args()
    engine = LocalEngine(options.docker_socket)
    engine.verify()
    output = ROOT / "test-results" / ("px-container-database-" + uuid.uuid4().hex[:12])
    output.mkdir(mode=0o700)
    probes = output / "scenarios"
    probes.mkdir(mode=0o700)
    report = {
        "passed": False,
        "gpu_executed": False,
        "runtime_contract": contract_digest(),
        "gpu_execution_digest": execution_digest(),
        "cpu_policy_digest": policy_digest(),
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (*SOURCES, *EXTRA)
        },
    }
    report_path = output / "result.json"
    save(report_path, report)
    print(json.dumps({"report": str(report_path)}), flush=True)
    image = None
    try:
        image = build_image(engine, options.runtime_archive, output, output.name)
        report["image"] = image
        save(report_path, report)
        environment = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C.UTF-8",
            "DOCKER_HOST": "unix://" + str(engine.path),
            "DOCKER_CONFIG": str(output / "docker-config"),
            "RUN_CONTAINER_DB_TESTS": "1",
            "PX_CONTAINER_DB_IMAGE": image,
            "PX_CONTAINER_DB_SOCKET": str(engine.path),
            "PX_CONTAINER_DB_REPORTS": str(probes),
        }
        tests = [
            "tests/test_gpu_container_database_integration.py",
            "tests/test_gpu_container_recovery.py",
            "tests/test_gpu_control.py",
            "tests/test_gpu_resources.py",
            "tests/test_gpu_startup.py",
            "tests/test_gpu_readiness.py",
            "tests/test_gpu_worker.py",
            "tests/test_gpu_worker_postgres.py",
            "tests/test_gpu_execution.py",
            "tests/test_gpu_admission.py",
        ]
        with (output / "postgres.log").open("w") as log:
            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/postgres_tests.py",
                    "-q",
                    *tests,
                    "--junitxml=" + str(output / "postgres.xml"),
                ],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=1200,
            )
        report["postgres_returncode"] = result.returncode
        records = [json.loads(path.read_text()) for path in sorted(probes.glob("*/result.json"))]
        report["scenarios"] = records
        report["postgres_resources_removed"] = (
            "Temporary PostgreSQL container and volume removed"
            in (output / "postgres.log").read_text()
        )
        suites = ET.parse(output / "postgres.xml").getroot().findall("testsuite")
        report["tests"] = {
            key: sum(int(suite.attrib.get(key, 0)) for suite in suites)
            for key in ("tests", "failures", "errors", "skipped")
        }
        assert result.returncode == 0 and report["postgres_resources_removed"]
        assert len(records) == 9 and all(
            item["passed"]
            and item["container_removed"]
            and item["process_tree_gone"]
            and item["storage_removed"]
            for item in records
        )
        assert all(report["tests"][key] == 0 for key in ("failures", "errors", "skipped"))
        assert (
            execution_digest() == report["gpu_execution_digest"]
            and contract_digest() == report["runtime_contract"]
        )
        assert all(
            hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == value
            for name, value in report["sources"].items()
        )
        report["passed"] = True
    except BaseException as error:
        report["failure"] = type(error).__name__ + ": " + str(error)
        raise
    finally:
        if image is not None:
            report["image_tag_removed"] = (
                engine.run("image", "rm", output.name, check=False).returncode == 0
            )
            if not report["image_tag_removed"]:
                report["passed"] = False
        save(report_path, report)
    assert report["passed"]
    print(
        json.dumps({"passed": True, "report": str(report_path), "tests": report["tests"]}),
        flush=True,
    )


if __name__ == "__main__":
    main()
