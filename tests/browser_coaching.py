"""Real browser workflow with an explicitly enabled real model provider.

Only synthetic accounts/public problem context are used. No database edits,
provider fixtures, fake hints or fabricated verdicts occur in this workflow.
The operator prepares an isolated application and fixed provider separately.
"""

import json
import os
import secrets
import uuid
from datetime import UTC, datetime
from pathlib import Path

from browser_community import delete_account, export_account, launch_arguments, register
from playwright.sync_api import expect, sync_playwright


def language(page, base, code):
    page.goto(base + "/coaching/")
    form = page.locator('form[action="/i18n/setlang/"]')
    form.locator('[name="language"]').select_option(code)
    form.locator('button[type="submit"]').click()


def main():
    real_model = (
        os.environ.get("BROWSER_COACHING") == "1" or os.environ.get("BROWSER_COACHING_REAL") == "1"
    )
    base = os.environ["BROWSER_URL"].rstrip("/")
    arguments = launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
    artifacts = Path(os.environ.get("BROWSER_ARTIFACTS", "test-results"))
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "coaching-browser.json").unlink(missing_ok=True)
    receipt = {
        "passed": False,
        "phase": "startup",
        "https": base.startswith("https://"),
        "actual_model_request": False,
        "ai_enabled_test": real_model,
        "objective_results_unchanged": False,
        "accounts_deleted": False,
    }
    accounts = [
        {
            "username": "coaching-browser-" + uuid.uuid4().hex[:12],
            "password": secrets.token_urlsafe(32),
            "created": False,
            "deleted": False,
        }
        for _ in range(2)
    ]
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=arguments)
        try:
            for account in accounts:
                account["page"] = browser.new_context(
                    locale="zh-CN", accept_downloads=True
                ).new_page()
                account["page"].on("pageerror", lambda error: errors.append(type(error).__name__))
                register(account, base)
            page, other = (account["page"] for account in accounts)
            receipt["phase"] = "private_goals_and_pinned_plan"
            language(page, base, "en")
            page.goto(base + "/coaching/goals/")
            page.locator('[name="targets"][value="sequences"]').check()
            page.get_by_role("button", name="Create a rule-based learning plan", exact=True).click()
            plan_url = page.url
            expect(
                page.get_by_role("heading", name="My plan with exact problem versions", exact=True)
            ).to_be_visible()
            assert other.goto(plan_url).status == 404
            page.get_by_role("button", name="Skip this suggestion", exact=True).first.click()
            expect(page.get_by_text("Skipped by you", exact=True)).to_be_visible()
            page.get_by_role("button", name="Restore this suggestion", exact=True).first.click()
            page.get_by_role("link", name="Open the pinned task", exact=True).first.click()
            expect(page.get_by_role("heading", name="Public examples", exact=True)).to_be_visible()
            page.goto(base + "/coaching/analysis/")
            expect(
                page.get_by_text("No verified official learning evidence yet.", exact=True)
            ).to_be_visible()
            receipt["private_goals_and_pins"] = True

            if real_model:
                receipt["phase"] = "explicit_public_input_preview"
                page.goto(base + "/problems/sum-positive/")
                page.get_by_role(
                    "link", name="Optional: preview AI tutor input", exact=True
                ).click()
                page.locator('[name="question"]').fill(
                    "According to this public task, should zero contribute to the sum? Give a conceptual hint without writing a solution."
                )
                page.locator('[name="hint_level"]').select_option("1")
                assert not page.locator('[name="include_source"]').is_checked()
                sentinel = "UNSELECTED-PRIVATE-SOURCE-" + uuid.uuid4().hex
                page.locator('[name="source"]').fill(sentinel)
                page.get_by_role(
                    "button", name="Create an input preview (do not send yet)", exact=True
                ).click()
                hint_url = page.url
                payload = json.loads(page.locator("#hint-preview").inner_text())
                assert not payload["source_opt_in"] and "learner_source" not in payload
                assert sentinel not in json.dumps(payload)
                assert len(payload["selected_author_hints"]) == 1
                assert payload["problem"]["statement"] and payload["problem"]["constraints"]
                assert "reference" not in payload["problem"] and "tests" not in payload["problem"]
                assert other.goto(hint_url).status == 404
                receipt["explicit_preview_and_source_default_off"] = True

                receipt["phase"] = "real_model_hint"
                page.locator('[name="confirm"]').check()
                page.get_by_role(
                    "button", name="Confirm sending and use one request", exact=True
                ).click()
                expect(page.locator("#hint-response")).to_be_visible(timeout=25000)
                response_text = page.locator("#hint-response").inner_text()
                assert 1 <= len(response_text.strip()) <= 4000
                receipt["actual_model_request"] = True
                receipt["synthetic_public_response_for_quality_review"] = response_text
                receipt["quality_scope"] = (
                    "Transport/UI completion only; advice requires independent human verification."
                )
                page.screenshot(path=str(artifacts / "coaching-real-model.png"), full_page=True)

            else:
                receipt["phase"] = "ai_disabled_state"
                page.goto(base + "/problems/sum-positive/")
                page.get_by_role(
                    "link", name="Optional: preview AI tutor input", exact=True
                ).click()
                expect(
                    page.get_by_text(
                        "Tutoring is disabled or unavailable. Normal practice remains available.",
                        exact=True,
                    )
                ).to_be_visible()
                assert page.locator('[name="question"]').count() == 0
                receipt["ai_disabled_verified"] = True

            receipt["phase"] = "private_export_and_deletion"
            language(page, base, "zh-hans")
            exported = export_account(page, base)
            assert exported["submissions"] == []
            assert len(exported["coaching"]["plans"]) == 1
            if real_model:
                assert len(exported["coaching"]["hints"]) == 1
                assert exported["coaching"]["hints"][0]["response"] == response_text
                assert exported["coaching"]["usage"][0]["count"] == 1
                page.goto(hint_url)
                page.get_by_role("button", name="删除这份私有会话", exact=True).click()
                exported = export_account(page, base)
                assert exported["coaching"]["hints"] == []
                assert exported["coaching"]["usage"][0]["count"] == 1
            else:
                assert exported["coaching"]["hints"] == []
                assert exported["coaching"]["usage"] == []
            receipt["objective_results_unchanged"] = True
            page.goto(plan_url)
            page.get_by_role("button", name="关闭这个计划", exact=True).click()
            page.get_by_role("button", name="删除这份私有计划", exact=True).click()
            assert export_account(page, base)["coaching"]["plans"] == []
            assert not errors
            receipt["passed"] = True
            receipt["phase"] = "complete"
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
                        language(account["page"], base, "zh-hans")
                        delete_account(account, base)
                    except Exception as error:
                        receipt["cleanup_error"] = type(error).__name__
            receipt["accounts_deleted"] = all(account["deleted"] for account in accounts)
            receipt["passed"] &= receipt["accounts_deleted"]
            receipt["finished_at"] = datetime.now(UTC).isoformat()
            (artifacts / "coaching-browser.json").write_text(
                json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
            )
            browser.close()
    print(
        json.dumps(
            {
                key: value
                for key, value in receipt.items()
                if key != "synthetic_public_response_for_quality_review"
            },
            ensure_ascii=False,
        )
    )
    if not receipt["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
