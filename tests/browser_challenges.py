"""Two accounts, opt-in participation, real Run/Submit, keyboard and private achievements."""

import json
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from browser_community import delete_account, export_account, launch_arguments, register
from playwright.sync_api import expect, sync_playwright


def language(page, base, code):
    page.goto(base + "/challenges/")
    form = page.locator('form[action="/i18n/setlang/"]')
    form.locator('[name="language"]').select_option(code)
    form.locator('button[type="submit"]').click()


def run_journey(environment, artifacts):
    base = environment["BROWSER_URL"].rstrip("/")
    slug = environment["BROWSER_CHALLENGE"]
    target = base + "/challenges/" + slug + "/revisions/1/"
    artifacts = Path(artifacts)
    artifacts.mkdir(parents=True, exist_ok=True)
    output = artifacts / "challenges-browser.json"
    output.unlink(missing_ok=True)
    real_judge = environment.get("BROWSER_JUDGE") == "1"
    receipt = {
        "passed": False,
        "phase": "startup",
        "real_judge": real_judge,
        "https": base.startswith("https://"),
        "accounts_deleted": False,
    }
    accounts = [
        {
            "username": "challenge-" + uuid.uuid4().hex[:12],
            "password": secrets.token_urlsafe(32),
            "created": False,
            "deleted": False,
        }
        for _ in range(2)
    ]
    errors = []
    references = {
        p["id"]: p["reference"]
        for p in json.loads(
            (Path(__file__).resolve().parent.parent / "content/starter.json").read_text()
        )["problems"]
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=launch_arguments(base, environment.get("BROWSER_TEST_SPKI", ""))
        )
        try:
            for account in accounts:
                account["page"] = browser.new_context(
                    locale="zh-CN", accept_downloads=True
                ).new_page()
                account["page"].on("pageerror", lambda error: errors.append(type(error).__name__))
                register(account, base)
            page, other = (account["page"] for account in accounts)
            receipt["phase"] = "consent_and_keyboard"
            page.goto(target)
            assert page.locator("#challenge-progress").count() == 0
            button = page.get_by_role("button", name="加入此活动", exact=True)
            button.focus()
            page.keyboard.press("Enter")
            expect(page.locator("#challenge-progress")).to_contain_text("0 / 2")
            page.locator('a[href$="/tasks/population-stats/"]').click()
            expect(page.get_by_role("heading", name="公开测试", exact=True)).to_be_visible()
            receipt["voluntary_join_keyboard_and_free_task_order"] = True
            if real_judge:
                receipt["phase"] = "real_run_and_formal_evidence"
                page.goto(base + "/problems/population-stats/")
                page.locator("#source").fill(references["population-stats"])
                page.get_by_role("button", name="运行样例", exact=True).click()
                expect(page.get_by_role("heading", name="通过", exact=True)).to_be_visible(
                    timeout=180000
                )
                page.goto(target)
                expect(page.locator("#challenge-progress")).to_contain_text("0 / 2")
                formal = []
                for problem in ("population-stats", "sum-positive"):
                    page.goto(base + "/problems/" + problem + "/")
                    page.locator("#source").fill(references[problem])
                    page.get_by_role("button", name="提交评测", exact=False).click()
                    expect(page.get_by_role("heading", name="通过", exact=True)).to_be_visible(
                        timeout=180000
                    )
                    formal.append(page.url.rsplit("/", 2)[-2])
                    assert other.goto(page.url).status == 404
                page.goto(target)
                expect(page.locator("#challenge-progress")).to_contain_text("2 / 2")
                assert other.goto(target).status == 200
                assert other.locator("#challenge-progress").count() == 0
                page.goto(base + "/learning/achievements/")
                expect(page.locator("#first-pass-v1")).to_contain_text("已达成")
                expect(page.locator("#challenge-complete-v1")).to_contain_text("已达成")
                page.screenshot(path=str(artifacts / "achievements-desktop.png"), full_page=True)
                exported = export_account(page, base)
                assert len(exported["learning"]["challenges"]) == 1
                facts = {row["id"]: row for row in exported["learning"]["achievements"]}
                assert (
                    facts["challenge-complete-v1"]["achieved"]
                    and facts["five-problems-v1"]["count"] == 2
                )
                del exported
                receipt["formal_results"] = formal
                receipt["run_excluded_and_two_formal_passes_counted"] = True
                receipt["private_achievements_export_and_cross_account_denial"] = True
            receipt["phase"] = "english_mobile_and_rejoin"
            language(page, base, "en")
            page.goto(base + "/practice/")
            page.locator('[name="evaluator"]').select_option("python-ml-linear-v1")
            page.locator('[name="max_units"]').fill("48")
            page.get_by_role("button", name="Filter", exact=True).click()
            assert page.locator(".problem-row").count() == 1
            expect(page.locator('select[name="evaluator"]')).to_have_value("python-ml-linear-v1")
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(target)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(path=str(artifacts / "challenge-mobile.png"), full_page=True)
            page.get_by_role("button", name="Leave this activity", exact=True).click()
            page.goto(target)
            page.get_by_role("button", name="Join this activity", exact=True).click()
            expect(page.locator("#challenge-progress")).to_contain_text("0 / 2")
            receipt["english_filters_mobile_and_rejoin_reset"] = True
            assert not errors
            receipt["passed"] = True
        except Exception as error:
            receipt["error_type"] = type(error).__name__
            trace = error.__traceback__
            while trace:
                if trace.tb_frame.f_code.co_filename == __file__:
                    receipt["error_line"] = trace.tb_lineno
                trace = trace.tb_next
        finally:
            for account in accounts:
                if account["created"]:
                    try:
                        language(account["page"], base, "zh-hans")
                        delete_account(account, base)
                    except Exception as error:
                        receipt["cleanup_error"] = type(error).__name__
            receipt["accounts_deleted"] = all(account["deleted"] for account in accounts)
            receipt["passed"] &= receipt["accounts_deleted"]
            receipt["finished_at"] = datetime.now(UTC).isoformat()
            output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
            browser.close()
    if not receipt["passed"]:
        raise AssertionError("Challenge browser acceptance failed; inspect its bounded receipt.")
    return receipt
