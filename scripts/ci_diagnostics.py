"""Publish only committed pytest locations, never failure text or parameter values."""

import argparse
import ast
import functools
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
COMMIT = re.compile(r"[a-f0-9]{40}")
MAX_REPORT_BYTES = 16 * 1024 * 1024
MAX_SOURCE_BYTES = 1024 * 1024
MAX_CASES = 100
MAX_ANNOTATIONS = 10


@functools.lru_cache(maxsize=64)
def committed_source(root, revision, filename):
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_NO_REPLACE_OBJECTS="1"
    )
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "cat-file", "blob", f"{revision}:{filename}"],
            env=environment,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode or len(result.stdout) > MAX_SOURCE_BYTES:
        return None
    return result.stdout


def case_location(root, revision, case):
    parts = case.get("classname", "").split(".")
    if (
        not 2 <= len(parts) <= 8
        or parts[0] != "tests"
        or not all(len(part) <= 100 and IDENTIFIER.fullmatch(part) for part in parts)
    ):
        return None
    name = case.get("name", "").partition("[")[0]
    for end in range(len(parts), 1, -1):
        if not parts[end - 1].startswith("test_"):
            continue
        filename = "/".join(parts[:end]) + ".py"
        source = committed_source(root, revision, filename)
        if source is None:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            return filename, 1, None
        definitions = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        ]
        if IDENTIFIER.fullmatch(name) and len(definitions) == 1:
            return filename, definitions[0].lineno, name
        # The module is public in this commit; an unrecognised case name is not.
        return filename, 1, None
    return None


def failure_annotations(path, root, revision):
    root = root.resolve()
    if not COMMIT.fullmatch(revision):
        raise ValueError("Expected an immutable Git commit")
    if (
        path.is_symlink()
        or not path.resolve().is_relative_to(root / "test-results")
        or not path.is_file()
        or path.stat().st_size > MAX_REPORT_BYTES
    ):
        raise ValueError("Expected a bounded local pytest report")
    data = path.read_bytes()
    text = data.decode("utf-8")
    if len(data) > MAX_REPORT_BYTES or "\x00" in text or "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise ValueError("Unsupported pytest report")
    document = ET.fromstring(text)
    if document.tag not in {"testsuite", "testsuites"}:
        raise ValueError("Unsupported pytest report")
    cases = [
        case
        for case in document.iter("testcase")
        if case.find("failure") is not None or case.find("error") is not None
    ]
    declared = max(
        (
            int(suite.get("failures", "0")) + int(suite.get("errors", "0"))
            for suite in document.iter("testsuite")
        ),
        default=0,
    )
    annotations = []
    seen = set()
    unmapped = declared > 0 and not cases
    for case in cases[:MAX_CASES]:
        location = case_location(root, revision, case)
        if location is None:
            unmapped = True
            continue
        if location in seen:
            continue
        seen.add(location)
        filename, line, name = location
        message = f"{name} failed." if name else "A test in this committed module failed."
        annotations.append(
            f"::error file={filename},line={line},title=Pytest failure::"
            f"{message} See the retained JUnit report for details."
        )
        if len(annotations) == MAX_ANNOTATIONS:
            break
    if unmapped or len(cases) > MAX_CASES:
        annotations.append(
            "::error title=Pytest failure::Additional failures could not be mapped to "
            "committed test definitions. See the retained JUnit report."
        )
    return annotations, bool(cases or declared)


def report_failures(path, root, revision):
    try:
        annotations, failed = failure_annotations(path, root, revision)
    except (OSError, ValueError, ET.ParseError):
        print("::error title=Pytest report::The expected pytest report could not be verified.")
        return 2
    for annotation in annotations:
        print(annotation)
    return int(failed)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--revision", required=True, help="The workflow's immutable github.sha")
    options = parser.parse_args()
    return report_failures(options.report, ROOT, options.revision)


if __name__ == "__main__":
    raise SystemExit(main())
