"""Real stateful API exercise through browser, local CLI and server worker."""

import json
import os
import secrets
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from browser_community import delete_account, export_account, launch_arguments, register
from browser_specialists import wait_finished
from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.practice import Client, PracticeError  # noqa: E402


def main():
    base = os.environ["BROWSER_URL"].rstrip("/")
    assert os.environ.get("BROWSER_JUDGE") == "1"
    artifacts = Path(os.environ.get("BROWSER_ARTIFACTS", ROOT / "test-results"))
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt_path = artifacts / "object-api-browser.json"
    receipt_path.unlink(missing_ok=True)
    ca = os.environ.get("BROWSER_CA_FILE")
    local_http = base.startswith("http://")
    api = Client(base, ca_file=ca, allow_local_http=local_http)
    connection = ["--server", base]
    if ca:
        connection += ["--ca-file", ca]
    if local_http:
        connection += ["--allow-local-http"]
    problem = next(
        p
        for p in json.loads((ROOT / "content/starter.json").read_text())["problems"]
        if p["id"] == "bounded-queue-api"
    )
    private = "private-api-" + uuid.uuid4().hex
    receipt = {"passed": False, "phase": "startup", "https": not local_http}
    accounts, errors = [], []
    raw = None
    with tempfile.TemporaryDirectory(prefix="praximesh-api-") as temporary, sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
        )
        for _ in range(2):
            page = browser.new_context(locale="zh-CN", accept_downloads=True).new_page()
            page.on("pageerror", lambda error: errors.append(type(error).__name__))
            accounts.append(
                {
                    "page": page,
                    "username": "api-browser-" + uuid.uuid4().hex[:10],
                    "password": secrets.token_urlsafe(32),
                    "created": False,
                    "deleted": False,
                }
            )
        page, peer = (account["page"] for account in accounts)
        guest = browser.new_context(locale="zh-CN").new_page()

        def cli(*arguments):
            result = subprocess.run(
                [sys.executable, str(ROOT / "scripts/practice.py"), *arguments],
                cwd=ROOT,
                env=dict(os.environ, PRAXIMESH_API_TOKEN=raw or ""),
                capture_output=True,
                timeout=180,
            )
            assert result.returncode == 0, "CLI failed (private output withheld)"
            return json.loads(result.stdout)

        def finished(identity):
            return wait_finished(api, identity, raw)

        try:
            receipt["phase"] = "accounts_and_contract"
            for account in accounts:
                register(account, base)
            page.goto(base + "/accounts/tokens/")
            page.locator("#id_label").fill("Stateful API acceptance")
            page.locator("#id_days").fill("1")
            page.locator("#id_can_submit").check()
            page.locator("#id_can_read_results").check()
            page.locator("#id_password").fill(accounts[0]["password"])
            page.get_by_role("button", name="创建令牌", exact=True).click()
            raw = page.locator(".api-token").inner_text()
            assert raw.startswith("pm_")
            page.goto(base + "/practice/?evaluator=python-object-api-v1")
            expect(page.locator("main")).to_contain_text(problem["title"])
            page.goto(base + "/learn/python-object-api-path/")
            page.get_by_role("button", name="加入我的路径", exact=True).click()
            expect(page.locator("main")).to_contain_text("0 / 1 道当前版本练习已通过")
            page.goto(base + "/problems/bounded-queue-api/")
            expect(page.locator("main")).to_contain_text("定义 BoundedQueue 类及声明的方法")
            expect(page.locator("main")).to_contain_text("q.put(7)")
            page.locator("#source").fill(problem["reference"] + "\n# " + private)
            page.get_by_role("button", name="运行样例", exact=True).click()
            page.wait_for_url("**/submissions/*/")
            run_id = page.url.rstrip("/").rsplit("/", 1)[1]
            run = finished(run_id)
            assert run["verdict"] == "accepted" and not run["formal_result"]
            assert run["resource_reservation"]["units"] == 2
            page.goto(base + "/learn/python-object-api-path/")
            expect(page.locator("main")).to_contain_text("0 / 1 道当前版本练习已通过")
            receipt["run_is_not_completion"] = True

            receipt["phase"] = "local_and_formal_execution"
            directory = Path(temporary) / "queue"
            downloaded = cli(*connection, "download", problem["id"], str(directory))
            assert downloaded["formal_result"] is False
            package = json.loads((directory / "exercise.json").read_text())["package"]
            assert len(package["public_tests"]) == 2
            for hidden in problem["tests"][2:]:
                assert hidden["name"] not in json.dumps(package, ensure_ascii=False)
            source = directory / downloaded["solution"]
            source.write_text(problem["reference"] + "\n# " + private)
            local = cli(
                "test",
                str(directory),
                "--bundle",
                os.environ["JUDGE_WASI_BUNDLE"],
                "--bundle-sha256",
                os.environ["JUDGE_WASI_SHA256"],
            )
            assert local["formal_result"] is False and local["result"]["passed"] == 2
            key = str(uuid.uuid4())
            queued = cli(*connection, "submit", str(directory), "--request-key", key)
            replay = cli(*connection, "submit", str(directory), "--request-key", key)
            assert queued["created"] and not replay["created"] and queued["id"] == replay["id"]
            accepted = finished(queued["id"])
            assert accepted["formal_result"] and accepted["verdict"] == "accepted"
            assert accepted["resource_reservation"]["units"] == 8
            assert accepted["resource_reservation"]["state"] == "consumed"
            source.write_text(problem["reference"].replace("pop(0)", "pop()"))
            wrong_job = cli(*connection, "submit", str(directory))
            wrong = finished(wrong_job["id"])
            assert wrong["formal_result"] and wrong["verdict"] == "wrong_answer"
            assert wrong["cpu_budget"]["consumed"] == 18 and wrong["cpu_budget"]["reserved"] == 0
            receipt["jobs"] = {
                "web_run": run_id,
                "accepted": queued["id"],
                "wrong": wrong_job["id"],
            }
            receipt["consumed_units"] = 18

            receipt["phase"] = "private_progress_and_optional_sharing"
            page.goto(base + "/learn/python-object-api-path/")
            expect(page.locator("main")).to_contain_text("1 / 1 道当前版本练习已通过")
            page.goto(base + "/learning/")
            expect(page.locator("main")).to_contain_text("当前版本通过 1 /")
            peer.goto(base + "/learning/")
            expect(peer.locator("main")).to_contain_text("当前版本通过 0 /")
            for identity in receipt["jobs"].values():
                assert peer.goto(base + f"/submissions/{identity}/").status == 404
                assert peer.goto(base + f"/api/submissions/{identity}/").status == 404
            page.goto(base + f"/submissions/{queued['id']}/")
            expect(page.locator("main")).to_contain_text("8 / 8")
            page.screenshot(path=str(artifacts / "object-api-result.png"), full_page=True)
            assert guest.goto(base + f"/showcase/profiles/{accounts[0]['username']}/").status == 404
            page.goto(base + "/showcase/settings/")
            page.locator("#id_public_name").fill("API learner")
            page.locator("#id_bio").fill("Optional API result")
            page.locator("#id_is_public").check()
            page.locator("#id_confirm_public").check()
            page.get_by_role("button", name="保存公开设置", exact=True).click()
            page.goto(base + f"/showcase/share/{queued['id']}/")
            page.locator("#id_confirm_public").check()
            page.get_by_role("button", name="明确分享这次结果", exact=True).click()
            public_url = page.url
            assert guest.goto(public_url).status == 200
            assert private not in guest.locator("main").inner_text()
            page.goto(base + "/showcase/settings/")
            page.get_by_role("button", name="关闭主页并撤回全部分享", exact=True).click()
            assert guest.goto(public_url).status == 404
            receipt["private_progress_and_explicit_sharing"] = True

            receipt["phase"] = "english_mobile_export_revoke"
            guest.goto(base + "/problems/bounded-queue-api/")
            guest.locator("#language-select").select_option("en")
            guest.get_by_role("button", name="切换", exact=True).click()
            expect(guest.locator("main")).to_contain_text("Define the BoundedQueue class")
            expect(guest.locator("main")).to_contain_text("Call sequence:")
            guest.set_viewport_size({"width": 390, "height": 844})
            assert guest.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
            guest.screenshot(path=str(artifacts / "object-api-mobile-en.png"), full_page=True)
            exported = export_account(page, base)
            serialized = json.dumps(exported)
            assert private in serialized and raw not in serialized
            assert len(exported["submissions"]) == 3
            assert len(exported["showcase"]["shared_results"]) == 1
            assert all("resource_reservation" in row for row in exported["submissions"])
            assert private not in json.dumps(export_account(peer, base))
            page.goto(base + "/accounts/tokens/")
            page.get_by_role("button", name="撤销令牌", exact=True).click()
            try:
                api.request(f"/api/v1/submissions/{queued['id']}/", token=raw)
            except PracticeError as error:
                assert "revoked" in str(error)
            else:
                raise AssertionError("Revoked token authorized")
            assert not errors
            receipt["english_mobile_export_and_revoke"] = True
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
                        delete_account(account, base)
                    except Exception:
                        receipt["passed"] = False
            receipt["accounts_deleted"] = all(a["created"] and a["deleted"] for a in accounts)
            browser.close()
    receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt))
    return 0 if receipt["passed"] and receipt["accounts_deleted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
