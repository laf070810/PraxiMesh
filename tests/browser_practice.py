"""Real HTTP account/token, CLI/WASI, server judging and opt-in sharing journey.

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

from browser_community import launch_arguments
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.practice import Client, PracticeError  # noqa: E402


def main():
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
    artifacts = ROOT / "test-results"
    artifacts.mkdir(exist_ok=True)
    receipt = {
        "passed": False,
        "https": not local_http,
        "phase": "startup",
        "account_deleted": False,
    }
    username = "practice-browser-" + uuid.uuid4().hex[:10]
    password = secrets.token_urlsafe(32)
    private_marker = "private-practice-" + uuid.uuid4().hex
    created = False
    errors = []
    problems = {
        item["id"]: item
        for item in json.loads((ROOT / "content/starter.json").read_text())["problems"]
    }
    with (
        tempfile.TemporaryDirectory(prefix="praximesh-practice-") as temporary,
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
                timeout=120,
            )
            assert result.returncode == 0, "CLI command failed (private output withheld)"
            return json.loads(result.stdout)

        def finished(identity):
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                result = api.request(f"/api/v1/submissions/{identity}/", token=raw)
                if result["status"] == "finished":
                    return result
                time.sleep(1)
            raise AssertionError("Real submission did not finish")

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
            for slug in ("sum-positive", "js-run-length", "sensor-analysis-report"):
                problem = problems[slug]
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
                wrong = (
                    "function solve() { return null; }"
                    if problem["language"] == "javascript"
                    else "print('null')"
                    if problem["evaluator"] == "python-stdio-v1"
                    else "def solve(*args): return None"
                )
                source_file.write_text(wrong)
                wrong_job = cli(*connection, "submit", str(directory))
                assert finished(wrong_job["id"])["verdict"] == "wrong_answer"
                results.append(
                    {"problem": slug, "accepted": queued["id"], "wrong_answer": wrong_job["id"]}
                )
            receipt["local_public_and_actual_server_results"] = results
            receipt["phase"] = "project_progress_and_public_consent"
            page.goto(base + "/projects/sensor-analysis/")
            expect(page.locator("main")).to_contain_text("1 / 4")
            assert guest.goto(base + f"/showcase/profiles/{username}/").status == 404
            page.goto(base + "/showcase/settings/")
            page.locator("#id_public_name").fill("Explicit public learner")
            page.locator("#id_bio").fill("Explicit public biography")
            page.locator("#id_is_public").check()
            page.locator("#id_confirm_public").check()
            page.get_by_role("button", name="保存公开设置", exact=True).click()
            for result in results:
                page.goto(base + f"/showcase/share/{result['accepted']}/")
                page.locator("#id_confirm_public").check()
                page.get_by_role("button", name="明确分享这次结果", exact=True).click()
                result["public_entry"] = page.url.removeprefix(base)
                assert guest.goto(page.url).status == 200
                text = guest.locator("main").inner_text()
                assert private_marker not in text and "Explicit public learner" in text
            page.goto(base + "/showcase/boards/")
            expect(page.locator("main")).to_contain_text("连续相同值的游程")
            expect(page.locator("main")).to_contain_text("生成传感器读数分析报告")
            receipt["each_runtime_can_share_real_versioned_result"] = True
            page.goto(base + "/showcase/settings/")
            page.get_by_role("button", name="关闭主页并撤回全部分享", exact=True).click()
            for result in results:
                assert guest.goto(base + result["public_entry"]).status == 404
            receipt["profile_close_withdraws_all_results"] = True
            receipt["phase"] = "export_and_revoke"
            page.goto(base + "/accounts/profile/")
            with page.expect_download() as download:
                page.get_by_role("button", name="下载 JSON", exact=True).click()
            exported = json.loads(Path(download.value.path()).read_text())
            assert private_marker in json.dumps(exported) and raw not in json.dumps(exported)
            assert len(exported["practice_api"]["tokens"]) == 1
            assert len(exported["showcase"]["shared_results"]) == 3
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
    (artifacts / "practice-browser.json").write_text(
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
