"""Reusable C-stage browser journeys for an explicitly disposable test database."""

import runpy
import subprocess
import sys
from pathlib import Path

from scripts.browser_acceptance import bootstrap_moderator, cleanup_moderator

ROOT = Path(__file__).resolve().parent.parent


def run_advanced_browsers(environment, owner_run, artifacts, *, include_challenges=False):
    """Use real browser actions; the trusted runner only manages test fixtures.

    Each independent functional journey gets a fresh rate-limit window in this
    owned synthetic database. No counters are reset within a journey or within
    the separate rate-limit/performance acceptance. Product policy is unchanged.
    """
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    # Invalidate all prior cohort receipts before any fixture or subprocess can fail.
    for filename in (
        "specialist-browser.json",
        "blogs-browser.json",
        "organizations-browser.json",
        "coaching-browser.json",
        "challenges-browser.json",
    ):
        (artifacts / filename).unlink(missing_ok=True)
    checked = []

    def cohort():
        owner_run(
            "manage.py",
            "shell",
            "-c",
            "from core.models import RateBucket; RateBucket.objects.all().delete()",
            env=environment,
        )

    def browser(script, env):
        with (artifacts / (script + ".log")).open("wb") as log:
            subprocess.run(
                [sys.executable, str(ROOT / "tests" / (script + ".py"))],
                cwd=ROOT,
                env=dict(env, BROWSER_ARTIFACTS=str(artifacts.resolve())),
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
                timeout=600,
            )
        checked.append(script)

    cohort()
    if environment.get("BROWSER_JUDGE") == "1":
        browser("browser_specialists", environment)
    cohort()
    credentials = bootstrap_moderator(owner_run, environment)
    try:
        browser("browser_blogs", dict(environment, **credentials))
    finally:
        cleanup_moderator(owner_run, environment, credentials)
    cohort()
    # The browser module receives the same constrained management runner as the
    # launcher. Neither database credentials nor an owner API enter any page.
    previous_path = sys.path[:]
    sys.path.insert(0, str(ROOT / "tests"))
    try:
        module = runpy.run_path(str(ROOT / "scripts/browser_organizations.py"))
        module["run_journey"](environment, owner_run, artifacts)
    finally:
        sys.path[:] = previous_path
    checked.append("browser_organizations")
    cohort()
    browser("browser_coaching", environment)
    if include_challenges:
        from scripts.browser_challenges import run_journey

        cohort()
        run_journey(environment, owner_run, artifacts)
        checked.append("browser_challenges")
    return {
        "journeys": checked,
        "independent_functional_rate_windows": True,
        "counter_reset_within_journey": False,
        "ai_enabled": environment.get("BROWSER_COACHING") == "1",
    }
