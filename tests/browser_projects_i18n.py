"""Read-only English project pages and explicit source-language fallbacks.

Requires owner-imported project content. It neither changes project state nor
claims to validate judging or completion evidence. Uses the same TLS pin rules
as the other browser acceptance journeys.
"""

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from browser_community import launch_arguments
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ.get("BROWSER_URL", "http://127.0.0.1:8765").rstrip("/")
    slug = os.environ.get("BROWSER_PROJECT_SLUG", "sensor-analysis")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,79}", slug):
        raise SystemExit("Invalid project identifier.")
    arguments = launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
    artifacts = Path("test-results")
    artifacts.mkdir(exist_ok=True)
    receipt = {
        "passed": False,
        "https": base.startswith("https://"),
        "phase": "startup",
        "read_only_project_checks": True,
        "actual_judging_tested": False,
    }
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=arguments)
        context = browser.new_context(locale="en-US")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        try:
            receipt["phase"] = "english_project_directory"
            response = page.goto(base + "/projects/")
            assert response.status == 200
            if receipt["https"]:
                assert "max-age=" in response.headers["strict-transport-security"]
                assert page.evaluate("window.isSecureContext")
            expect(page.locator("html")).to_have_attribute("lang", "en")
            expect(
                page.get_by_role("heading", name="Multi-stage projects", exact=True)
            ).to_be_visible()
            expect(
                page.get_by_role("link", name="Engineering projects", exact=True)
            ).to_be_visible()
            source_notice = "Project plans and stage descriptions are currently available in the Chinese original. The English interface does not translate this content automatically."
            expect(page.get_by_text(source_notice, exact=True)).to_be_visible()
            page.locator(f'main a[href="/projects/{slug}/"]').click()
            expect(
                page.get_by_role("heading", name="What you will deliver", exact=True)
            ).to_be_visible()
            expect(
                page.get_by_role("link", name="Sign in to see your project progress", exact=True)
            ).to_be_visible()
            expect(page.get_by_text(source_notice, exact=True)).to_be_visible()
            links = page.get_by_role("link", name="Open this stage →", exact=True)
            stages = [links.nth(index).get_attribute("href") for index in range(links.count())]
            assert stages and all(
                path.startswith(f"/projects/{slug}/revisions/") for path in stages
            )
            receipt["phase"] = "english_pinned_stages"
            for path in stages:
                assert page.goto(base + path).status == 200
                expect(page.get_by_role("heading", name="Task", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="Public tests", exact=True)).to_be_visible()
                expect(
                    page.get_by_role("heading", name="Evaluation conditions", exact=True)
                ).to_be_visible()
                expect(page.get_by_text(source_notice, exact=True)).to_be_visible()
                language = page.locator("p.note-body[lang]").first.get_attribute("lang")
                assert language in {"zh-hans", "en"}
                if language == "zh-hans":
                    expect(
                        page.get_by_text(
                            "No English translation is available for this version. The Chinese original is shown below.",
                            exact=True,
                        )
                    ).to_be_visible()
            receipt["visible_stages_checked"] = len(stages)
            receipt["phase"] = "project_language_switch"
            page.locator('[name="language"]').select_option("zh-hans")
            page.get_by_role("button", name="Switch", exact=True).click()
            expect(page.locator("html")).to_have_attribute("lang", "zh-hans")
            expect(page.get_by_role("heading", name="任务", exact=True)).to_be_visible()
            expect(page.locator(".project-source-language")).to_contain_text("目前提供中文原文")
            page.locator('[name="language"]').select_option("en")
            page.get_by_role("button", name="切换", exact=True).click()
            expect(page.get_by_role("heading", name="Task", exact=True)).to_be_visible()
            assert not errors
            receipt["phase"] = "complete"
            receipt["passed"] = True
        except Exception as error:
            receipt["error"] = type(error).__name__
            trace = error.__traceback__
            while trace:
                if trace.tb_frame.f_code.co_filename == __file__:
                    receipt["error_line"] = trace.tb_lineno
                trace = trace.tb_next
        finally:
            browser.close()
    receipt["completed_at"] = datetime.now(UTC).isoformat()
    (artifacts / "projects-i18n-browser.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "English project browser passed"
        if receipt["passed"]
        else "English project browser failed; see sanitized receipt"
    )
    raise SystemExit(0 if receipt["passed"] else 1)


if __name__ == "__main__":
    main()
