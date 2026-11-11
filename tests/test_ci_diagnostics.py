"""CI annotations expose committed test identities, not runtime report payloads."""

import subprocess
import xml.etree.ElementTree as ET

import pytest

from scripts import ci_diagnostics


def git(root, *arguments):
    return subprocess.check_output(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "user.name=Acceptance fixture",
            "-c",
            "user.email=fixture@example.test",
            *arguments,
        ],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "tests").mkdir()
    (root / "test-results").mkdir()
    (root / "tests/test_public.py").write_text(
        "# This committed source is parsed, never imported.\n"
        "raise RuntimeError('source must never execute')\n\n"
        "def test_public():\n    pass\n\n"
        "class TestGroup:\n    def test_member(self):\n        pass\n"
    )
    git(root, "init", "--quiet")
    git(root, "add", "--", "tests/test_public.py")
    git(root, "commit", "--quiet", "-m", "Public fixture")
    return root, git(root, "rev-parse", "HEAD")


def report(root, cases):
    suite = ET.Element("testsuite", tests=str(len(cases)))
    for name, classname, outcome in cases:
        case = ET.SubElement(suite, "testcase", name=name, classname=classname)
        if outcome:
            ET.SubElement(
                case, outcome, message="PRIVATE_EXCEPTION_VALUE"
            ).text = "PRIVATE_TRACEBACK"
        ET.SubElement(case, "system-out").text = "PRIVATE_OUTPUT"
    path = root / "test-results/pytest.xml"
    ET.ElementTree(suite).write(path)
    return path


def test_failure_locations_come_from_the_commit_and_strip_parameter_values(repository, capsys):
    root, revision = repository
    path = report(
        root,
        [
            ("test_public[PRIVATE_PARAMETER]", "tests.test_public", "failure"),
            ("test_member[OTHER_SECRET]", "tests.test_public.TestGroup", "error"),
        ],
    )
    assert ci_diagnostics.report_failures(path, root, revision) == 1
    output = capsys.readouterr().out
    assert "file=tests/test_public.py,line=4" in output and "test_public failed" in output
    assert "file=tests/test_public.py,line=8" in output and "test_member failed" in output
    assert "PRIVATE" not in output and "OTHER_SECRET" not in output


def test_worktree_and_new_head_cannot_supply_private_case_names(repository, capsys):
    root, revision = repository
    (root / "tests/test_public.py").write_text("def test_PRIVATE_DYNAMIC_NAME():\n    pass\n")
    git(root, "add", "--", "tests/test_public.py")
    git(root, "commit", "--quiet", "-m", "Changed fixture")
    path = report(root, [("test_PRIVATE_DYNAMIC_NAME", "tests.test_public", "failure")])
    assert ci_diagnostics.report_failures(path, root, revision) == 1
    output = capsys.readouterr().out
    assert "file=tests/test_public.py,line=1" in output
    assert "PRIVATE" not in output and "committed module failed" in output


def test_git_replacement_cannot_supply_private_case_names(repository, capsys):
    root, revision = repository
    (root / "tests/test_public.py").write_text("def test_PRIVATE_REPLACEMENT():\n    pass\n")
    git(root, "add", "--", "tests/test_public.py")
    git(root, "commit", "--quiet", "-m", "Replacement fixture")
    git(root, "replace", revision, git(root, "rev-parse", "HEAD"))
    path = report(root, [("test_PRIVATE_REPLACEMENT", "tests.test_public", "failure")])
    assert ci_diagnostics.report_failures(path, root, revision) == 1
    output = capsys.readouterr().out
    assert "file=tests/test_public.py,line=1" in output
    assert "PRIVATE" not in output and "committed module failed" in output


@pytest.mark.parametrize(
    "name,classname",
    [
        ("test_public[SECRET%0A::warning::injected]", "tests.test_public"),
        ("PRIVATE_NAME", "tests.missing_PRIVATE_MODULE"),
        ("PRIVATE_NAME", "../../PRIVATE_PATH"),
        ("PRIVATE_NAME", "tests.test_public%0A::warning::PRIVATE"),
    ],
)
def test_report_metadata_cannot_inject_commands_or_private_values(
    repository, capsys, name, classname
):
    root, revision = repository
    path = report(root, [(name, classname, "failure")])
    assert ci_diagnostics.report_failures(path, root, revision) == 1
    output = capsys.readouterr().out
    assert "PRIVATE" not in output and "SECRET" not in output and "::warning" not in output


def test_passes_and_skips_do_not_publish_report_contents(repository, capsys):
    root, revision = repository
    path = report(
        root,
        [
            ("test_public[PRIVATE_PARAMETER]", "tests.test_public", None),
            ("test_public[PRIVATE_PARAMETER]", "tests.test_public", "skipped"),
        ],
    )
    assert ci_diagnostics.report_failures(path, root, revision) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "outside",
        "symlink",
        "oversized",
        "entity",
        "encoded-entity",
        "invalid",
        "revision",
    ],
)
def test_invalid_reports_fail_without_printing_their_contents(
    repository, capsys, monkeypatch, change
):
    root, revision = repository
    path = report(root, [("test_public", "tests.test_public", "failure")])
    if change == "missing":
        path.unlink()
    elif change == "outside":
        target = root / "PRIVATE.xml"
        path.rename(target)
        path = target
    elif change == "symlink":
        target = root / "PRIVATE.xml"
        path.rename(target)
        path.symlink_to(target)
    elif change == "oversized":
        monkeypatch.setattr(ci_diagnostics, "MAX_REPORT_BYTES", 4)
    elif change == "entity":
        path.write_text('<!DOCTYPE testsuite [<!ENTITY secret "PRIVATE">]><testsuite/>')
    elif change == "encoded-entity":
        path.write_bytes(
            '<!DOCTYPE testsuite [<!ENTITY secret "PRIVATE">]><testsuite/>'.encode("utf-16")
        )
    elif change == "invalid":
        path.write_text("PRIVATE_INVALID_XML")
    else:
        revision = "PRIVATE_REVISION"
    assert ci_diagnostics.report_failures(path, root, revision) == 2
    assert "PRIVATE" not in capsys.readouterr().out


def test_parameter_failures_are_deduplicated(repository, capsys):
    root, revision = repository
    path = report(
        root, [(f"test_public[secret-{i}]", "tests.test_public", "failure") for i in range(30)]
    )
    assert ci_diagnostics.report_failures(path, root, revision) == 1
    assert capsys.readouterr().out.count("::error") == 1
