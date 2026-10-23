"""Real blog creation, review, publication and withdrawal through browser forms.

The operator bootstraps one synthetic community reviewer. This script creates
its own author and deletes both accounts; it never writes application tables.
"""

import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

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


def main():
    base = os.environ.get("BROWSER_URL", "http://127.0.0.1:8765").rstrip("/")
    username = os.environ.get("BROWSER_MODERATOR_USERNAME", "")
    password = os.environ.get("BROWSER_MODERATOR_PASSWORD", "")
    if not username.startswith(MODERATOR_PREFIX) or not password:
        raise SystemExit("Provide an isolated synthetic community reviewer.")
    arguments = launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
    artifacts = Path(os.environ.get("BROWSER_ARTIFACTS", "test-results"))
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "blogs-browser.json").unlink(missing_ok=True)
    receipt = {"passed": False, "https": base.startswith("https://"), "phase": "startup"}
    accounts = []
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=arguments)
        try:
            author = {
                "username": "blog-browser-" + uuid.uuid4().hex[:12],
                "password": secrets.token_urlsafe(32),
                "created": False,
                "deleted": False,
                "page": browser.new_context(locale="zh-CN", accept_downloads=True).new_page(),
            }
            reviewer = {
                "username": username,
                "password": password,
                "created": True,
                "deleted": False,
                "page": browser.new_context(locale="zh-CN").new_page(),
            }
            accounts = [author, reviewer]
            page, review_page = author["page"], reviewer["page"]
            guest = browser.new_context(locale="en-US").new_page()
            for current in (page, review_page, guest):
                current.on("pageerror", lambda error: errors.append(type(error).__name__))
            receipt["phase"] = "private_blog_draft"
            register(author, base)
            login(review_page, base, username, password)
            page.goto(base + "/community/blogs/")
            page.get_by_role("link", name="写一篇私有博文草稿", exact=True).click()
            expect(page.locator('[name="kind"]')).to_have_value("blog")
            title = "自主送审博文 " + uuid.uuid4().hex[:8]
            body = "用户原文 <script>window.blogXss=true</script>"
            page.locator('[name="title"]').fill(title)
            page.locator('[name="body"]').fill(body)
            page.get_by_role("button", name="保存内容", exact=True).click()
            target = page.url
            directory = base + "/community/blogs/" + author["username"] + "/"
            expect(page.locator("main")).to_contain_text("私有草稿")
            assert_status(guest, target, 404)
            assert_status(guest, directory, 404)
            assert_status(review_page, target, 404)
            receipt["private_default"] = True

            receipt["phase"] = "reviewed_blog_publication"
            submit(page, target)
            assert_status(guest, directory, 404)
            moderate(review_page, base, target, "post", "publish", "审核本篇主动送审博文")
            assert_status(guest, directory, 200)
            expect(guest.locator("html")).to_have_attribute("lang", "en")
            expect(
                guest.get_by_role("heading", name=author["username"] + "’s blog", exact=True)
            ).to_be_visible()
            guest.get_by_role("link", name=title, exact=True).click()
            expect(guest.locator("main")).to_contain_text(body)
            assert guest.evaluate("typeof window.blogXss") == "undefined"
            expect(guest.get_by_role("link", name=author["username"], exact=True)).to_be_visible()
            receipt["public_directory_and_escaped_original_text"] = True

            receipt["phase"] = "edited_blog_requires_new_review"
            page.goto(target)
            page.get_by_role("link", name="编辑", exact=True).click()
            revised = title + " · 修订"
            page.locator('[name="title"]').fill(revised)
            page.locator('[name="body"]').fill(body + "\n作者修订正文")
            page.get_by_role("button", name="保存内容", exact=True).click()
            assert_status(guest, target, 404)
            assert_status(guest, directory, 404)
            moderate(review_page, base, target, "post", "publish", "审核最新博文修订")
            assert_status(guest, directory, 200)
            expect(guest.get_by_role("link", name=revised, exact=True)).to_be_visible()
            receipt["edit_hides_until_reviewed"] = True

            receipt["phase"] = "withdraw_export_delete"
            page.goto(target)
            page.get_by_role("button", name="撤回公开内容", exact=True).click()
            assert_status(guest, directory, 404)
            assert_status(guest, target, 404)
            exported = export_account(page, base)
            assert any(
                item["kind"] == "blog" and item["title"] == revised and item["state"] == "withdrawn"
                for item in exported["community"]["posts"]
            )
            del exported
            page.goto(target)
            page.get_by_role("button", name="删除正文及关联评论", exact=True).click()
            assert_status(page, target, 404)
            receipt["withdrawn_blog_export_and_delete"] = True
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
            for account in accounts:
                if account["created"]:
                    try:
                        delete_account(account, base)
                    except Exception as error:
                        receipt["cleanup_error"] = type(error).__name__
                        receipt["passed"] = False
            receipt["synthetic_accounts_deleted"] = bool(accounts) and all(
                account["deleted"] for account in accounts
            )
            browser.close()
    receipt["completed_at"] = datetime.now(UTC).isoformat()
    (artifacts / "blogs-browser.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(
        "Blog browser workflow passed"
        if receipt["passed"]
        else "Blog browser failed; see sanitized receipt"
    )
    raise SystemExit(0 if receipt["passed"] else 1)


if __name__ == "__main__":
    main()
