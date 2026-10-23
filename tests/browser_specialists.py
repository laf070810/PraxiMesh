"""Real specialist Web Run, public API/CLI, formal judging, quota and privacy journey.

Requires BROWSER_JUDGE=1 and a verified local bundle. The launcher supplies an
ephemeral CA for HTTPS; this script never creates results or users via the DB.
"""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.practice import Client, PracticeError  # noqa: E402


def wait_finished(api, identity, token):
    """Keep polling below the user's 60/minute API budget; never reset it."""
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        try:
            result = api.request(f"/api/v1/submissions/{identity}/", token=token)
        except PracticeError as error:
            # Client exposes fixed safe messages, not response bodies. Only its
            # exact 429 mapping is retryable; auth/transport failures surface.
            if str(error) != "Rate or pending-submission limit reached. Retry later.":
                raise
        else:
            if result["status"] == "finished" and time.monotonic() < deadline:
                return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(2, remaining))
    raise AssertionError("Real submission did not finish")


def main():
    from browser_community import launch_arguments
    from playwright.sync_api import expect, sync_playwright

    base = os.environ["BROWSER_URL"].rstrip("/")
    if os.environ.get("BROWSER_JUDGE") != "1":
        raise RuntimeError("This acceptance journey requires actual server judging")
    arguments = launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
    ca = os.environ.get("BROWSER_CA_FILE")
    local_http = base.startswith("http://")
    api = Client(base, ca_file=ca, allow_local_http=local_http)
    connection = ["--server", base]
    if ca:
        connection += ["--ca-file", ca]
    if local_http:
        connection += ["--allow-local-http"]
    artifacts = Path(os.environ.get("BROWSER_ARTIFACTS", ROOT / "test-results"))
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "specialist-browser.json").unlink(missing_ok=True)
    receipt = {
        "passed": False,
        "https": not local_http,
        "phase": "startup",
        "account_deleted": False,
    }
    username = "specialist-browser-" + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(32)
    private_marker = "private-specialist-" + uuid.uuid4().hex
    created = False
    errors = []
    problems = {
        item["id"]: item
        for item in json.loads((ROOT / "content/starter.json").read_text())["problems"]
    }
    with (
        tempfile.TemporaryDirectory(prefix="praximesh-specialists-") as temporary,
        sync_playwright() as playwright,
    ):
        browser = playwright.chromium.launch(args=arguments)
        context = browser.new_context(locale="zh-CN", accept_downloads=True)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        guest = browser.new_context(locale="zh-CN").new_page()
        raw = None

        def cli(*arguments):
            environment = dict(os.environ, PRAXIMESH_API_TOKEN=raw or "")
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/practice.py"), *arguments],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                timeout=240,
            )
            assert result.returncode == 0, "CLI command failed (private output withheld)"
            return json.loads(result.stdout)

        def finished(identity):
            return wait_finished(api, identity, raw)

        try:
            receipt["phase"] = "register_and_issue_token"
            page.goto(base + "/accounts/register/")
            page.locator("#id_username").fill(username)
            page.locator("#id_password1").fill(password)
            page.locator("#id_password2").fill(password)
            page.get_by_role("button", name="注册", exact=True).click()
            expect(page.get_by_role("heading", name="保存你的恢复码。", exact=True)).to_be_visible()
            created = True
            page.goto(base + "/accounts/tokens/")
            page.locator("#id_label").fill("Browser CLI acceptance")
            page.locator("#id_days").fill("1")
            page.locator("#id_can_submit").check()
            page.locator("#id_can_read_results").check()
            page.locator("#id_password").fill(password)
            page.get_by_role("button", name="创建令牌", exact=True).click()
            raw = page.locator(".api-token").inner_text()
            assert raw.startswith("pm_")
            page.goto(base + "/accounts/tokens/")
            assert page.locator(".api-token").count() == 0
            receipt["token_issued_once_via_authenticated_form"] = True
            receipt["phase"] = "local_and_server_judging"
            results = []
            for slug in ("train-linear-model", "weighted-line-fit", "approved-ledger-report"):
                problem = problems[slug]
                page.goto(base + f"/problems/{slug}/")
                expect(page.locator("main")).to_contain_text("CPU 练习额度")
                page.locator("#source").fill(problem["reference"] + "\n# " + private_marker)
                page.get_by_role("button", name="运行样例", exact=True).click()
                page.wait_for_url("**/submissions/*/")
                run_id = page.url.rstrip("/").rsplit("/", 1)[1]
                web_run = finished(run_id)
                assert web_run["verdict"] == "accepted" and not web_run["formal_result"]
                page.reload()
                expect(page.locator("main")).to_contain_text("不计入正式完成状态")
                directory = Path(temporary) / slug
                package = cli(*connection, "download", slug, str(directory))
                assert package["formal_result"] is False
                source_file = directory / package["solution"]
                suffix = "\n// " if problem["language"] == "javascript" else "\n# "
                source_file.write_text(problem["reference"] + suffix + private_marker + "\n")
                local = cli(
                    "test",
                    str(directory),
                    "--bundle",
                    os.environ["JUDGE_WASI_BUNDLE"],
                    "--bundle-sha256",
                    os.environ["JUDGE_WASI_SHA256"],
                )
                assert local["formal_result"] is False and local["result"]["verdict"] == "accepted"
                key = str(uuid.uuid4())
                queued = cli(*connection, "submit", str(directory), "--request-key", key)
                replay = cli(*connection, "submit", str(directory), "--request-key", key)
                assert (
                    queued["created"] is True
                    and replay["created"] is False
                    and queued["id"] == replay["id"]
                )
                final = finished(queued["id"])
                assert final["formal_result"] is True and final["verdict"] == "accepted"
                assert "source" not in final and private_marker not in json.dumps(final)
                queried = cli(*connection, "status", queued["id"])
                assert queried["verdict"] == "accepted"
                page.goto(base + f"/submissions/{queued['id']}/")
                expected_metric = {
                    "train-linear-model": "留出集均方误差",
                    "weighted-line-fit": "约化卡方",
                    "approved-ledger-report": "实际工具结果",
                }[slug]
                expect(page.locator("main")).to_contain_text(expected_metric)
                expect(page.locator("main")).to_contain_text("开始调用")
                assert (
                    page.get_by_role("link", name="选择公开这条评测结果", exact=True).count() == 0
                )
                page.screenshot(path=str(artifacts / (slug + "-feedback.png")), full_page=True)
                assert guest.goto(base + f"/showcase/share/{queued['id']}/").status == 200
                assert (
                    "/accounts/login/" in guest.url
                    and private_marker not in guest.locator("main").inner_text()
                )
                source_file.write_text(f"def {problem['function']}(*args): return None")
                wrong_job = cli(*connection, "submit", str(directory), "--mode", "run")
                wrong = finished(wrong_job["id"])
                assert wrong["verdict"] == "wrong_answer" and not wrong["formal_result"]
                cost = 6 if slug == "approved-ledger-report" else 1
                assert final["resource_reservation"]["units"] == cost * len(problem["tests"])
                assert final["resource_reservation"]["state"] == "consumed"
                results.append(
                    {
                        "problem": slug,
                        "accepted": queued["id"],
                        "web_run": run_id,
                        "wrong_run": wrong_job["id"],
                    }
                )
            receipt["local_public_and_actual_server_results"] = results
            receipt["phase"] = "private_progress_and_quota"
            page.goto(base + "/learning/")
            expect(page.locator("main")).to_contain_text("当前版本通过 3 /")
            expect(page.locator("main")).to_contain_text("一次通过不等于掌握")
            final_budget = finished(results[-1]["accepted"])["cpu_budget"]
            expected_units = sum(
                (6 if slug == "approved-ledger-report" else 1)
                * (
                    len(problems[slug]["tests"])
                    + 2 * sum(case["public"] for case in problems[slug]["tests"])
                )
                for slug in ("train-linear-model", "weighted-line-fit", "approved-ledger-report")
            )
            assert (
                final_budget["consumed"] == expected_units <= 120 and final_budget["reserved"] == 0
            )
            receipt["resource_units_consumed"] = expected_units
            receipt["private_progress_verified"] = True
            receipt["phase"] = "export_and_revoke"
            page.goto(base + "/accounts/profile/")
            with page.expect_download() as download:
                page.get_by_role("button", name="下载 JSON", exact=True).click()
            exported = json.loads(Path(download.value.path()).read_text())
            assert private_marker in json.dumps(exported) and raw not in json.dumps(exported)
            assert len(exported["practice_api"]["tokens"]) == 1
            assert not exported["showcase"]["shared_results"]
            assert len(exported["submissions"]) == 9
            assert all("resource_reservation" in item for item in exported["submissions"])
            page.goto(base + "/accounts/tokens/")
            page.get_by_role("button", name="撤销令牌", exact=True).click()
            try:
                api.request(f"/api/v1/submissions/{results[0]['accepted']}/", token=raw)
            except PracticeError as error:
                assert "revoked" in str(error)
            else:
                raise AssertionError("Revoked bearer still authorized")
            assert not errors
            receipt["export_excludes_secret_and_revocation_is_effective"] = True
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
            if created:
                try:
                    page.goto(base + "/accounts/profile/")
                    form = page.locator('form[action="/accounts/delete/"]')
                    form.locator('[name="password"]').fill(password)
                    form.locator('[name="confirm"]').fill("DELETE")
                    form.get_by_role("button").click()
                    expect(
                        page.get_by_role("heading", name="账号已删除", exact=True)
                    ).to_be_visible()
                    receipt["account_deleted"] = True
                except Exception:
                    receipt["cleanup_failed"] = True
                    receipt["passed"] = False
            browser.close()
    (artifacts / "specialist-browser.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "passed": receipt["passed"],
                "phase": receipt["phase"],
                "account_deleted": receipt["account_deleted"],
            }
        )
    )
    return 0 if receipt["passed"] and receipt["account_deleted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
