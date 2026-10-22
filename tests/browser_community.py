"""Real community workflow against an explicitly prepared, isolated application.

All writing, moderation, export and deletion use browser forms. The operator
bootstraps only a synthetic moderator with community.moderate_content. No database
connection, direct state changes or private submission imports are used here.
"""

import base64
import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

MODERATOR_PREFIX = "community-reviewer-"


def launch_arguments(base, pin):
    address = urlsplit(base)
    if address.scheme not in {"http", "https"} or not address.hostname:
        raise ValueError("BROWSER_URL must be an HTTP(S) application origin.")
    if not pin:
        return []
    if address.scheme != "https" or address.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Test certificate pins are permitted only for loopback HTTPS.")
    if len(base64.b64decode(pin, validate=True)) != 32:
        raise ValueError("Expected a SHA-256 SPKI pin.")
    # This exact ephemeral key is the sole test exception, as in browser_smoke.py.
    # Never disable TLS verification for a context or install system trust.
    return ["--ignore-certificate-errors-spki-list=" + pin]


def login(page, base, username, password):
    page.goto(base + "/accounts/login/")
    page.locator("#id_username").fill(username)
    page.locator("#id_password").fill(password)
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("button", name="退出", exact=True)).to_be_visible()
    expect(page.locator(".user-link")).to_have_text(username)


def register(account, base):
    page = account["page"]
    page.goto(base + "/accounts/register/")
    page.locator("#id_username").fill(account["username"])
    page.locator("#id_password1").fill(account["password"])
    page.locator("#id_password2").fill(account["password"])
    page.get_by_role("button", name="注册", exact=True).click()
    expect(page.get_by_role("heading", name="保存你的恢复码。", exact=True)).to_be_visible()
    account["created"] = True
    if base.startswith("https://"):
        cookies = {cookie["name"]: cookie for cookie in page.context.cookies()}
        assert cookies["sessionid"]["secure"] and cookies["sessionid"]["httpOnly"]
        assert cookies["csrftoken"]["secure"]


def delete_account(account, base):
    page = account["page"]
    login(page, base, account["username"], account["password"])
    page.goto(base + "/accounts/profile/")
    form = page.locator("form.danger")
    form.locator('[name="password"]').fill(account["password"])
    form.locator('[name="confirm"]').fill("DELETE")
    form.get_by_role("button", name="永久删除当前账号", exact=True).click()
    expect(page.get_by_role("heading", name="账号已删除", exact=True)).to_be_visible()
    account["deleted"] = True


def export_account(page, base):
    page.goto(base + "/accounts/profile/")
    with page.expect_download() as download:
        page.get_by_role("button", name="下载 JSON", exact=True).click()
    artifact = download.value
    try:
        return json.loads(Path(artifact.path()).read_text())
    finally:
        # Export includes synthetic private source; do not retain it as a CI artifact.
        artifact.delete()


def assert_status(page, target, expected):
    response = page.goto(target)
    assert response is not None and response.status == expected, "Unexpected community visibility"


def submit(page, target):
    page.goto(target)
    expect(page.get_by_text("正文及作者用户名", exact=False)).to_be_visible()
    page.get_by_role("button", name="主动提交公开审核", exact=True).click()
    # The action may redirect to the author's list; re-open the exact owned item.
    page.goto(target)
    expect(page.locator("main")).to_contain_text("等待审核")


def moderate(page, base, target, kind, action, note):
    if action in {"publish", "reject"}:
        page.goto(base + "/community/moderation/?kind=" + kind)
        page.locator(f'a[href="{urlsplit(target).path}"]').click()
    else:
        page.goto(target)
    form = page.locator('form[action*="/community/moderation/"]')
    form.locator('[name="action"]').select_option(action)
    form.locator('[name="note"]').fill(note)
    form.get_by_role("button", name="保存审核决定", exact=True).click()
    expect(page.locator("main")).to_contain_text("审核决定已保存")


def new_comment(page, post_url, body):
    page.goto(post_url)
    page.get_by_role("link", name="写评论草稿", exact=True).click()
    page.locator('[name="body"]').fill(body)
    page.get_by_role("button", name="保存内容", exact=True).click()
    expect(page.locator("main")).to_contain_text("私有草稿")
    return page.url


def run_workflow(browser, base, accounts, result, artifacts, progress, errors):
    def phase(value):
        progress["phase"] = value

    author, reporter, moderator = accounts
    for account in accounts:
        account["page"] = browser.new_context(accept_downloads=True, locale="zh-CN").new_page()
        account["page"].on("pageerror", lambda error: errors.append(type(error).__name__))
    guest = browser.new_context(locale="zh-CN").new_page()
    guest.on("pageerror", lambda error: errors.append(type(error).__name__))
    author_page, reporter_page, reviewer = [account["page"] for account in accounts]
    phase("registration_and_moderator_login")
    register(author, base)
    register(reporter, base)
    login(reviewer, base, moderator["username"], moderator["password"])
    moderator["created"] = True
    assert_status(reviewer, base + "/community/moderation/", 200)
    assert_status(author_page, base + "/community/moderation/", 403)
    if base.startswith("https://"):
        response = guest.goto(base)
        assert "max-age=" in response.headers["strict-transport-security"]
        assert guest.evaluate("window.isSecureContext")

    suffix = uuid.uuid4().hex[:10]
    title = "浏览器审核题解 " + suffix
    body = "独立题解正文 <script>window.communityXss=true</script> " + suffix
    private_source = "def solve(values): return 0\n# PRIVATE-COMMUNITY-SOURCE-" + suffix
    phase("private_draft_and_explicit_review_request")
    author_page.goto(base + "/problems/sum-positive/")
    author_page.locator("#source").fill(private_source)
    author_page.get_by_role("button", name="保存草稿", exact=True).click()
    expect(author_page.locator("#editor-status")).to_have_text("草稿已私密保存")
    author_page.get_by_role("link", name="写题解", exact=True).click()
    expect(author_page.locator('[name="kind"]')).to_have_value("solution")
    assert author_page.locator('[name="version"]').input_value()
    author_page.locator('[name="title"]').fill(title)
    author_page.locator('[name="body"]').fill(body)
    author_page.get_by_role("button", name="保存内容", exact=True).click()
    post_url = author_page.url
    assert_status(guest, post_url, 404)
    assert_status(reviewer, post_url, 404)  # Moderators cannot read unsubmitted drafts.
    submit(author_page, post_url)
    assert_status(guest, post_url, 404)
    assert_status(reporter_page, post_url, 404)
    moderate(reviewer, base, post_url, "post", "publish", "真实浏览器已核对这一修订。")
    assert_status(guest, post_url, 200)
    expect(guest.locator("main")).to_contain_text(body)
    assert guest.evaluate("typeof window.communityXss") == "undefined"
    assert private_source not in guest.locator("body").inner_text()
    assert "PRIVATE-COMMUNITY-SOURCE" not in guest.locator("body").inner_text()
    result["explicit_submission_and_moderator_publication"] = True
    result["draft_private_even_from_moderator"] = True
    result["escaped_plain_text_and_no_private_source_import"] = True

    phase("published_edit_and_fresh_review")
    author_page.goto(post_url)
    author_page.get_by_role("link", name="编辑", exact=True).click()
    author_page.locator('[name="body"]').fill(body + "\n审核后再编辑的新修订。")
    author_page.get_by_role("button", name="保存内容", exact=True).click()
    assert_status(guest, post_url, 404)
    assert_status(reviewer, post_url, 200)
    moderate(reviewer, base, post_url, "post", "publish", "已重新审核编辑后的正文。")
    assert_status(guest, post_url, 200)
    expect(guest.locator("main")).to_contain_text("审核后再编辑的新修订。")
    result["published_edit_hidden_until_fresh_review"] = True

    phase("comment_review")
    comment_body = "浏览器评论，需要独立审核。 " + suffix
    comment_url = new_comment(reporter_page, post_url, comment_body)
    assert_status(author_page, comment_url, 404)
    submit(reporter_page, comment_url)
    assert_status(guest, comment_url, 404)
    moderate(reviewer, base, comment_url, "comment", "publish", "评论已审核。")
    assert_status(guest, comment_url, 200)
    expect(guest.locator("main")).to_contain_text(comment_body)
    result["comment_explicit_submission_and_review"] = True

    phase("private_report_and_resolution")
    report_reason = "仅举报人与审核者可见 " + suffix
    reporter_page.goto(post_url)
    reporter_page.get_by_role("link", name="私有举报", exact=True).click()
    reporter_page.locator('[name="reason"]').fill(report_reason)
    reporter_page.get_by_role("button", name="提交说明", exact=True).click()
    report_url = reporter_page.url
    assert_status(author_page, report_url, 404)
    assert_status(reviewer, report_url, 200)
    reviewer.locator('[name="decision"]').select_option("resolved")
    reviewer.locator('[name="note"]').fill("已检查举报，并记录处理结论。")
    reviewer.get_by_role("button", name="保存处理结果", exact=True).click()
    reporter_page.goto(report_url)
    expect(reporter_page.locator("main")).to_contain_text("已处理")
    expect(reporter_page.locator("main")).to_contain_text("已检查举报，并记录处理结论。")
    result["private_report_reviewed_without_author_access"] = True

    phase("hide_private_appeal_and_fresh_publication")
    moderate(reviewer, base, post_url, "post", "hide", "请作者解释这一处表述。")
    assert_status(guest, post_url, 404)
    assert_status(guest, comment_url, 404)
    assert_status(reporter_page, comment_url, 404)
    author_page.goto(post_url)
    author_page.get_by_role("link", name="对本次决定提出私有申诉", exact=True).click()
    appeal_reason = "仅作者与审核者可见的申诉 " + suffix
    author_page.locator('[name="reason"]').fill(appeal_reason)
    author_page.get_by_role("button", name="提交说明", exact=True).click()
    appeal_url = author_page.url
    assert_status(reporter_page, appeal_url, 404)
    reviewer.goto(appeal_url)
    reviewer.locator('[name="decision"]').select_option("reopened")
    reviewer.locator('[name="note"]').fill("申诉已核对，重新送审。")
    reviewer.get_by_role("button", name="保存处理结果", exact=True).click()
    assert_status(guest, post_url, 404)
    moderate(reviewer, base, post_url, "post", "publish", "申诉后的新一轮审核已通过。")
    assert_status(guest, post_url, 200)
    assert_status(guest, comment_url, 200)
    result["hidden_parent_blocks_comment_access"] = True
    result["private_appeal_reopens_then_requires_review"] = True

    phase("private_account_exports")
    author_export = export_account(author_page, base)
    reporter_export = export_account(reporter_page, base)
    phase("export_author_identity")
    assert author_export["username"] == author["username"]
    phase("export_reporter_identity")
    assert reporter_export["username"] == reporter["username"]
    phase("export_author_post_count")
    assert len(author_export["community"]["posts"]) == 1
    phase("export_author_appeal_count")
    assert len(author_export["community"]["appeals"]) == 1
    phase("export_author_report_isolation")
    assert not author_export["community"]["reports"]
    phase("export_author_excludes_private_report_reason")
    assert report_reason not in json.dumps(author_export, ensure_ascii=False)
    phase("export_reporter_comment_count")
    assert len(reporter_export["community"]["comments"]) == 1
    phase("export_reporter_report_count")
    assert len(reporter_export["community"]["reports"]) == 1
    phase("export_reporter_appeal_isolation")
    assert not reporter_export["community"]["appeals"]
    phase("export_reporter_excludes_private_appeal_reason")
    assert appeal_reason not in json.dumps(reporter_export, ensure_ascii=False)
    phase("export_private_draft_source")
    result["draft_export_exact_source_match"] = any(
        draft["source"] == private_source for draft in author_export["drafts"]
    )
    result["draft_export_matches_after_crlf_normalization"] = any(
        draft["source"].replace("\r\n", "\n") == private_source for draft in author_export["drafts"]
    )
    # Multipart textarea data uses CRLF. Preserve all other source characters.
    assert result["draft_export_matches_after_crlf_normalization"]
    result["exports_isolate_own_writing_reports_and_appeals"] = True

    phase("withdrawal_and_content_deletion")
    author_page.goto(post_url)
    author_page.get_by_role("button", name="撤回公开内容", exact=True).click()
    assert_status(guest, post_url, 404)
    assert_status(guest, comment_url, 404)
    author_page.goto(post_url)
    author_page.get_by_role("button", name="删除正文及关联评论", exact=True).click()
    for target in (post_url, comment_url, report_url, appeal_url):
        assert_status(reviewer, target, 404)
    assert not export_account(reporter_page, base)["community"]["comments"]
    assert not export_account(reporter_page, base)["community"]["reports"]
    result["withdrawal_and_content_deletion_cascade"] = True

    phase("account_deletion_cascade")
    author_page.goto(base + "/community/new/")
    author_page.locator('[name="kind"]').select_option("discussion")
    author_page.locator('[name="title"]').fill("账号删除演练 " + suffix)
    author_page.locator('[name="body"]').fill("删除账号时一起删除的讨论。")
    author_page.get_by_role("button", name="保存内容", exact=True).click()
    deletion_post_url = author_page.url
    submit(author_page, deletion_post_url)
    moderate(reviewer, base, deletion_post_url, "post", "publish", "账号删除演练的合成内容。")
    assert_status(guest, deletion_post_url, 200)
    guest.screenshot(path=str(artifacts / "community-published.png"), full_page=True)
    deletion_comment_url = new_comment(
        reporter_page, deletion_post_url, "别人删除讨论时，我的评论也应删除。"
    )
    delete_account(author, base)
    assert_status(guest, deletion_post_url, 404)
    assert_status(reporter_page, deletion_comment_url, 404)
    assert not export_account(reporter_page, base)["community"]["comments"]
    moderator_export = export_account(reviewer, base)
    events = moderator_export["community"]["moderation_events"]
    assert events
    assert all(
        set(event) == {"id", "target_kind", "target_id", "target_revision", "action", "created_at"}
        for event in events
    )
    delete_account(reporter, base)
    result["account_deletion_removes_writing_and_other_authors_comments"] = True
    result["retained_moderation_events_contain_no_body_or_username"] = True
    assert not errors, "Unexpected browser page errors"
    phase("complete")


def main():
    base = os.environ.get("BROWSER_URL", "http://127.0.0.1:8765").rstrip("/")
    moderator_name = os.environ.get("BROWSER_MODERATOR_USERNAME", "")
    moderator_password = os.environ.get("BROWSER_MODERATOR_PASSWORD", "")
    if not moderator_name or not moderator_password:
        raise ValueError(
            "Explicit synthetic moderator credentials are required in the environment."
        )
    arguments = launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
    artifacts = Path("test-results")
    artifacts.mkdir(exist_ok=True)
    suffix = uuid.uuid4().hex[:12]
    accounts = [
        {"username": "browser-community-author-" + suffix, "password": secrets.token_urlsafe(36)},
        {"username": "browser-community-reader-" + suffix, "password": secrets.token_urlsafe(36)},
        {"username": moderator_name, "password": moderator_password},
    ]
    result = {
        "passed": False,
        "uses_browser_forms_only": True,
        "https_transport_tested": base.startswith("https://"),
        "test_spki_pin_used": bool(arguments),
    }
    progress = {"phase": "browser_start"}
    errors = []
    failure = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=arguments)
        try:
            run_workflow(browser, base, accounts, result, artifacts, progress, errors)
        except Exception as error:
            # Playwright call logs can include filled passwords; retain only a safe stage/class.
            failure = type(error).__name__
            traceback = error.__traceback__
            while traceback:
                if (
                    Path(traceback.tb_frame.f_code.co_filename).resolve()
                    == Path(__file__).resolve()
                ):
                    result["failure_line"] = traceback.tb_lineno
                traceback = traceback.tb_next
        finally:
            cleanup_errors = []
            for index, account in enumerate(accounts):
                owned = index < 2 or account["username"].startswith(MODERATOR_PREFIX)
                if owned and account.get("created") and not account.get("deleted"):
                    try:
                        delete_account(account, base)
                    except Exception as error:
                        cleanup_errors.append(
                            {
                                "account_role": ("author", "reporter", "moderator")[index],
                                "error_class": type(error).__name__,
                            }
                        )
            result["author_account_deleted"] = bool(accounts[0].get("deleted"))
            result["reporter_account_deleted"] = bool(accounts[1].get("deleted"))
            result["synthetic_moderator_account_deleted"] = bool(accounts[2].get("deleted"))
            result["non_synthetic_moderator_retained"] = not moderator_name.startswith(
                MODERATOR_PREFIX
            )
            result["cleanup_errors"] = cleanup_errors
            result["passed"] = (
                failure is None
                and not cleanup_errors
                and result["author_account_deleted"]
                and result["reporter_account_deleted"]
            )
            result["phase"] = progress["phase"]
            if failure:
                result["failure_class"] = failure
            result["completed_at"] = datetime.now(UTC).isoformat()
            browser.close()
            (artifacts / "community-browser.json").write_text(json.dumps(result, indent=2) + "\n")
    if not result["passed"]:
        raise RuntimeError(
            "Community browser acceptance failed at "
            + progress["phase"]
            + "; inspect community-browser.json (credentials and private exports are not retained)."
        ) from None
    print(
        "Community browser workflow passed: explicit publication, editing and fresh review, comments, private reports/appeals, withdrawal/deletion, isolated exports and account cleanup."
    )


if __name__ == "__main__":
    main()
