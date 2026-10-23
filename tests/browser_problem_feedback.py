"""Versioned feedback through real forms; only moderator bootstrap uses owner access."""

import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from browser_community import (
    MODERATOR_PREFIX,
    assert_status,
    delete_account,
    export_account,
    launch_arguments,
    login,
    moderate,
    register,
    submit,
)
from playwright.sync_api import expect, sync_playwright


def journey(browser, base, accounts, artifacts, receipt):
    author, reviewer = accounts
    page, review = author["page"], reviewer["page"]
    guest = browser.new_context(locale="en-US", viewport={"width": 390, "height": 844}).new_page()
    errors = []
    for current in (page, review, guest):
        current.on("pageerror", lambda error: errors.append(type(error).__name__))
    register(author, base)
    login(review, base, reviewer["username"], reviewer["password"])
    receipt["phase"] = "versioned_private_draft"
    page.goto(base + "/problems/sum-positive/")
    page.get_by_role("link", name="反馈此题", exact=True).click()
    expect(page.locator("#id_kind")).to_have_value("feedback")
    version = page.locator("#id_version").input_value()
    assert version
    expect(page.get_by_role("heading", name="反馈说明", exact=True)).to_be_visible()
    title = "题目示例修订建议 " + uuid.uuid4().hex[:10]
    body = "建议明确空列表的示例。<script>window.feedbackXss=true</script>"
    page.locator("#id_title").fill(title)
    page.locator("#id_body").fill(body)
    page.locator("#id_version").select_option("")
    page.get_by_role("button", name="保存内容", exact=True).click()
    expect(page.locator(".errorlist")).to_contain_text("题目反馈必须选择明确的题目版本")
    page.locator("#id_version").select_option(version)
    page.get_by_role("button", name="保存内容", exact=True).click()
    target = page.url
    assert_status(guest, target, 404)
    assert_status(review, target, 404)
    receipt["explicit_version_and_private_draft"] = True

    receipt["phase"] = "review_filtered_feedback"
    submit(page, target)
    assert_status(guest, target, 404)
    review.goto(base + "/community/moderation/")
    review.get_by_role("link", name="待审题目反馈", exact=True).click()
    expect(review.locator(f'a[href="{urlsplit(target).path}"]')).to_be_visible()
    moderate(review, base, target, "post", "publish", "反馈可以公开讨论，题目尚未修订。")
    guest.goto(base + "/problems/sum-positive/")
    guest.get_by_role("link", name="Problem feedback", exact=True).click()
    guest.get_by_role("link", name=title, exact=True).click()
    expect(guest.locator("main")).to_contain_text(body)
    expect(guest.locator("main")).to_contain_text("does not mean the problem has been corrected")
    assert guest.evaluate("typeof window.feedbackXss") == "undefined"
    assert guest.evaluate("document.documentElement.scrollWidth <= innerWidth")
    guest.screenshot(path=str(artifacts / "problem-feedback-mobile.png"), full_page=True)
    receipt["moderated_publication_filter_english_mobile_and_escaping"] = True

    receipt["phase"] = "edit_requires_review"
    page.goto(target)
    page.get_by_role("link", name="编辑", exact=True).click()
    expect(page.locator("#id_version")).to_have_value(version)
    page.locator("#id_body").fill(body + "\n修订建议的补充理由。")
    page.get_by_role("button", name="保存内容", exact=True).click()
    assert_status(guest, target, 404)
    moderate(review, base, target, "post", "publish", "补充内容已审核。")
    assert_status(guest, target, 200)
    receipt["edit_hides_until_fresh_review"] = True

    receipt["phase"] = "withdraw_export_delete"
    page.goto(target)
    page.get_by_role("button", name="撤回公开内容", exact=True).click()
    assert_status(guest, target, 404)
    assert_status(review, target, 404)
    exported = export_account(page, base)
    posts = exported["community"]["posts"]
    assert len(posts) == 1 and posts[0]["kind"] == "feedback"
    assert posts[0]["state"] == "withdrawn" and posts[0]["version_id"] == version
    assert not export_account(review, base)["community"]["posts"]
    del exported, posts
    delete_account(author, base)
    assert_status(guest, target, 404)
    assert_status(review, target, 404)
    receipt["withdrawal_owned_export_and_account_deletion"] = True
    assert not errors
    receipt["phase"] = "complete"


def main():
    base = os.environ["BROWSER_URL"].rstrip("/")
    username = os.environ["BROWSER_MODERATOR_USERNAME"]
    password = os.environ["BROWSER_MODERATOR_PASSWORD"]
    if not username.startswith(MODERATOR_PREFIX) or not password:
        raise SystemExit("Provide an isolated synthetic community reviewer.")
    arguments = launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
    artifacts = Path(os.environ.get("BROWSER_ARTIFACTS", "test-results"))
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt_path = artifacts / "problem-feedback-browser.json"
    receipt_path.unlink(missing_ok=True)
    receipt = {"passed": False, "https": base.startswith("https://"), "phase": "startup"}
    accounts = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=arguments)
        try:
            accounts = [
                {
                    "username": "feedback-browser-" + uuid.uuid4().hex[:12],
                    "password": secrets.token_urlsafe(32),
                    "created": False,
                    "deleted": False,
                },
                {"username": username, "password": password, "created": True, "deleted": False},
            ]
            for account in accounts:
                account["page"] = browser.new_context(
                    locale="zh-CN", accept_downloads=True
                ).new_page()
            journey(browser, base, accounts, artifacts, receipt)
            receipt["passed"] = True
        except Exception as error:
            receipt["error"] = type(error).__name__
            trace = error.__traceback__
            while trace:
                if trace.tb_frame.f_code.co_filename == __file__:
                    receipt["error_line"] = trace.tb_lineno
                trace = trace.tb_next
        finally:
            for account in accounts:
                if account["created"] and not account["deleted"]:
                    try:
                        delete_account(account, base)
                    except Exception as error:
                        receipt["passed"] = False
                        receipt["cleanup_error"] = type(error).__name__
            receipt["all_accounts_deleted"] = bool(accounts) and all(a["deleted"] for a in accounts)
            receipt["passed"] = receipt["passed"] and receipt["all_accounts_deleted"]
            browser.close()
    receipt["at"] = datetime.now(UTC).isoformat()
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    if not receipt["passed"]:
        raise SystemExit("Problem feedback browser acceptance failed; inspect the safe receipt.")


if __name__ == "__main__":
    main()
