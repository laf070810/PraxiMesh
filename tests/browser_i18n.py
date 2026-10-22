"""English browser workflow. Uses only HTTP actions and deletes its synthetic account."""

import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from browser_community import launch_arguments
from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ.get("BROWSER_URL", "http://127.0.0.1:8765").rstrip("/")
    arguments = launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
    artifacts = Path("test-results")
    artifacts.mkdir(exist_ok=True)
    username = "i18n-browser-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    receipt = {
        "passed": False,
        "https": base.startswith("https://"),
        "phase": "startup",
        "account_deleted": False,
    }
    created = False
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=arguments)
        context = browser.new_context(locale="en-US", accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        guest = browser.new_context(locale="en-US").new_page()
        try:
            receipt["phase"] = "language_switch_and_content"
            response = page.goto(base)
            if receipt["https"]:
                assert "max-age=" in response.headers["strict-transport-security"]
                assert page.evaluate("window.isSecureContext")
            expect(page.locator("html")).to_have_attribute("lang", "en")
            page.locator('[name="language"]').select_option("zh-hans")
            page.get_by_role("button", name="Switch", exact=True).click()
            expect(page.locator("html")).to_have_attribute("lang", "zh-hans")
            page.locator('[name="language"]').select_option("en")
            page.get_by_role("button", name="切换", exact=True).click()
            expect(page.locator("html")).to_have_attribute("lang", "en")
            page.goto(base + "/learn/python-to-numerical/")
            expect(
                page.get_by_role(
                    "heading", name="From Python foundations to numerical computation", exact=True
                )
            ).to_be_visible()
            page.goto(base + "/problems/sum-positive/")
            expect(
                page.get_by_role("heading", name="Sum only positive values", exact=True)
            ).to_be_visible()
            expect(page.get_by_role("heading", name="Your task", exact=True)).to_be_visible()
            assert "全非正数" not in page.locator("main").inner_text()
            page.screenshot(path=str(artifacts / "english-problem.png"), full_page=True)
            page.goto(base + "/problems/argmax-first/")
            expect(
                page.get_by_text(
                    "No English translation is available for this version. The Chinese original is shown below.",
                    exact=True,
                )
            ).to_be_visible()
            receipt["language_switch_exact_source_and_fallback"] = True

            receipt["phase"] = "english_registration_and_editor"
            page.goto(base + "/accounts/register/")
            page.locator("#id_username").fill(username)
            page.locator("#id_password1").fill(password)
            page.locator("#id_password2").fill(password)
            page.get_by_role("button", name="Register", exact=True).click()
            expect(
                page.get_by_role("heading", name="Save your recovery code.", exact=True)
            ).to_be_visible()
            created = True
            if receipt["https"]:
                cookies = {cookie["name"]: cookie for cookie in context.cookies()}
                assert cookies["sessionid"]["secure"] and cookies["sessionid"]["httpOnly"]
                assert (
                    cookies["django_language"]["secure"] and cookies["django_language"]["httpOnly"]
                )
            page.goto(base + "/problems/sum-positive/")
            source = (
                "def solve(values): return sum(x for x in values if x > 0)\n# 私有代码 "
                + uuid.uuid4().hex
            )
            page.locator("#source").fill(source)
            expect(page.locator("#editor-status")).to_have_text("Unsaved changes")
            page.get_by_role("button", name="Save draft", exact=True).click()
            expect(page.locator("#editor-status")).to_have_text("Draft saved privately")
            if os.environ.get("BROWSER_JUDGE") == "1":
                page.get_by_role("button", name="Submit for judging ↗", exact=True).click()
                expect(page.get_by_role("heading", name="Accepted", exact=True)).to_be_visible(
                    timeout=120000
                )
                receipt["actual_english_judging"] = True
            else:
                receipt["actual_english_judging"] = False
            page.goto(base + "/learn/tutorial-functions/")
            page.get_by_role("button", name="Mark as read", exact=True).click()
            page.goto(base + "/learn/tutorial-functions/")
            expect(page.get_by_role("button", name="Undo reading mark", exact=True)).to_be_visible()
            page.goto(base + "/learning/")
            expect(
                page.get_by_role("heading", name="Practice evidence and suggestions", exact=True)
            ).to_be_visible()
            receipt["english_forms_js_and_learning"] = True

            receipt["phase"] = "private_text_and_review"
            page.goto(base + "/community/new/")
            page.locator('[name="kind"]').select_option("discussion")
            page.locator('[name="title"]').fill("Private English workflow " + uuid.uuid4().hex[:8])
            original = "用户原文不自动翻译 <script>window.i18nXss=true</script>"
            page.locator('[name="body"]').fill(original)
            page.get_by_role("button", name="Save content", exact=True).click()
            target = page.url
            expect(page.locator("main")).to_contain_text(original)
            assert page.evaluate("typeof window.i18nXss") == "undefined"
            assert guest.goto(target).status == 404
            page.get_by_role("button", name="Submit for public review", exact=True).click()
            page.goto(target)
            expect(page.locator("main")).to_contain_text("Awaiting review")
            assert guest.goto(target).status == 404
            page.get_by_role("button", name="Withdraw public content", exact=True).click()
            page.goto(target)
            page.get_by_role("button", name="Delete text and related comments", exact=True).click()
            assert guest.goto(target).status == 404
            receipt["user_text_unchanged_and_private_workflow"] = True
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
            if created:
                try:
                    page.goto(base + "/accounts/profile/")
                    form = page.locator("form.danger")
                    form.locator('[name="password"]').fill(password)
                    form.locator('[name="confirm"]').fill("DELETE")
                    form.get_by_role(
                        "button", name="Permanently delete this account", exact=True
                    ).click()
                    expect(
                        page.get_by_role("heading", name="Account deleted", exact=True)
                    ).to_be_visible()
                    receipt["account_deleted"] = True
                except Exception as error:
                    receipt["cleanup_error"] = type(error).__name__
                    receipt["passed"] = False
            browser.close()
    receipt["completed_at"] = datetime.now(UTC).isoformat()
    (artifacts / "i18n-browser.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "English browser workflow passed"
        if receipt["passed"]
        else "English browser workflow failed; see sanitized receipt"
    )
    raise SystemExit(0 if receipt["passed"] else 1)


if __name__ == "__main__":
    main()
