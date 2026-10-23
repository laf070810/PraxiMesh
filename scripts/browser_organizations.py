"""Real Chromium organization journey with an explicit trusted owner runner.

Production/release harnesses call ``run_journey(environment, owner_run, artifacts)``.
``owner_run(*management_script_arguments, env=private_environment)`` must target
their owned DB-owner control process. It must raise on failure. Only reviewer
bootstrap, exact publication/withdrawal and read-only retention checks use it.
All organization/course/package/user actions use actual browser forms.

``--local`` prepares a disposable SQLite application with judging disabled. Its
receipt proves the browser journey, not PostgreSQL role isolation or judging.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
REVIEWER_PREFIX = "org-reviewer-"
HEX64 = re.compile(r"[a-f0-9]{64}\Z")


def bootstrap_reviewer(owner_run, environment):
    credentials = {
        "BROWSER_ORG_REVIEWER_USERNAME": REVIEWER_PREFIX + secrets.token_hex(6),
        "BROWSER_ORG_REVIEWER_PASSWORD": secrets.token_urlsafe(36),
    }
    private = dict(environment, **credentials)
    owner_run(
        "manage.py",
        "shell",
        "-c",
        "import os\nfrom django.contrib.auth import get_user_model\n"
        "from django.core.management import call_command\nfrom django.db import transaction\n"
        "with transaction.atomic():\n"
        "    user=get_user_model().objects.create_user("
        "username=os.environ['BROWSER_ORG_REVIEWER_USERNAME'], "
        "password=os.environ['BROWSER_ORG_REVIEWER_PASSWORD'])\n"
        "    call_command('organization_reviewer', user.username, grant=True)\n"
        "    assert not user.is_staff and not user.is_superuser and not user.groups.exists()\n"
        "    assert set(user.user_permissions.values_list('content_type__app_label','codename')) "
        "== {('organizations','review_packages')}\n",
        env=private,
    )
    return credentials


def synthetic_payload(catalog, marker):
    """Clone data only; uploaded reference source is never executed by this script."""
    domain = copy.deepcopy(next(row for row in catalog["nodes"] if row["kind"] == "domain"))
    domain.update(
        id="domain",
        title="浏览器隔离题库领域",
        domains=[],
        topics=[],
        concepts=[],
        relations=[],
        steps=[],
    )
    concept = copy.deepcopy(next(row for row in catalog["nodes"] if row["kind"] == "concept"))
    concept.update(
        id="concept",
        title="浏览器隔离题库概念",
        domains=["domain"],
        topics=[],
        concepts=[],
        relations=[],
        aliases=[],
        steps=[],
    )
    problem = copy.deepcopy(next(row for row in catalog["problems"] if row["id"] == "sum-positive"))
    problem.update(
        id="positive-sum", revision=1, concepts=["concept"], title="浏览器投稿的正数累计"
    )
    problem["reference"] += "\n# " + marker + "-reference\n"
    next(case for case in problem["tests"] if not case["public"])["name"] = marker + "-hidden-case"
    return {
        "source_url": "https://example.org/isolated-browser-fixture",
        "license": "LicenseRef-Pending-Maintainer-Approval",
        "attribution": "浏览器隔离验收内容，不构成公开授权。",
        "content": {"schema_version": 1, "nodes": [domain, concept], "problems": [problem]},
        "external": [],
    }


def checked_owner_action(owner_run, environment, action, identity, digest):
    if not isinstance(digest, str) or not HEX64.fullmatch(digest):
        raise ValueError("Invalid exact package digest")
    if action == "publish":
        if not isinstance(identity, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", identity):
            raise ValueError("Invalid package revision")
        command = "org_package_publish"
    elif action == "withdraw":
        if not isinstance(identity, str) or str(uuid.UUID(identity)) != identity:
            raise ValueError("Invalid package identity")
        command = "org_package_withdraw"
    else:
        raise ValueError("Unsupported owner action")
    owner_run("manage.py", command, identity, "--digest", digest, env=environment)


def verify_retention(owner_run, environment, namespace, usernames, course_digest, package_digest):
    """Fixed read-only assertions; never create evidence or alter retained snapshots."""
    if not re.fullmatch(r"org-browser-[a-f0-9]{10}", namespace):
        raise ValueError("Expected this journey's isolated organization")
    if len(usernames) != 3 or any(
        not re.fullmatch(r"(?:org-owner-|org-learner-|org-reviewer-)[a-f0-9]{12}", value)
        for value in usernames
    ):
        raise ValueError("Expected only synthetic journey accounts")
    if any(
        not isinstance(value, str) or not HEX64.fullmatch(value)
        for value in (course_digest, package_digest)
    ):
        raise ValueError("Expected the exact retained snapshot digests")
    owner_run(
        "manage.py",
        "shell",
        "-c",
        "import json,os; from django.contrib.auth import get_user_model; "
        "from core.content import digest; from organizations.packages import package_digest; "
        "from organizations.models import Organization,Membership,Invitation,CourseRevision,"
        "CourseEnrollment,Package,PackageRevision,OrganizationEvent; "
        "namespace=os.environ['BROWSER_ORG_NAMESPACE']; "
        "org=Organization.objects.get(pk=namespace); assert org.created_by_id is None; "
        "assert not get_user_model().objects.filter(username__in=json.loads(os.environ['BROWSER_ORG_USERS'])).exists(); "
        "assert not Membership.objects.filter(organization=org).exists(); "
        "assert not Invitation.objects.filter(organization=org).exists(); "
        "assert not CourseEnrollment.objects.filter(revision__course__organization=org).exists(); "
        "courses=CourseRevision.objects.filter(course__organization=org); "
        "assert courses.count()==1 and not courses.exclude(author_id=None).exists(); "
        "course=courses.get(); assert len(course.data['units'])==2; "
        "assert course.digest==digest(course.data)==os.environ['BROWSER_ORG_COURSE_DIGEST']; "
        "package=Package.objects.get(organization=org); assert package.state=='withdrawn'; "
        "revision=PackageRevision.objects.get(package=package); "
        "assert revision.author_id is None and revision.approved_by_id is None; "
        "assert revision.digest==revision.approved_digest==package_digest(package,revision.payload)"
        "==os.environ['BROWSER_ORG_PACKAGE_DIGEST']; "
        "assert revision.payload['content']['problems'][0]['reference']; "
        "assert not package.managed_objects.filter(active=True).exists(); "
        "events=OrganizationEvent.objects.filter(organization=org); "
        "assert events.exists() and not events.exclude(actor_id=None).exists()",
        env=dict(
            environment,
            BROWSER_ORG_NAMESPACE=namespace,
            BROWSER_ORG_USERS=json.dumps(usernames),
            BROWSER_ORG_COURSE_DIGEST=course_digest,
            BROWSER_ORG_PACKAGE_DIGEST=package_digest,
        ),
    )


def run_journey(environment, owner_run, artifacts=None):
    from playwright.sync_api import expect, sync_playwright

    sys.path.insert(0, str(ROOT / "tests"))
    from browser_community import (
        assert_status,
        delete_account,
        export_account,
        launch_arguments,
        login,
        register,
    )

    base = environment["BROWSER_URL"].rstrip("/")
    arguments = launch_arguments(base, environment.get("BROWSER_TEST_SPKI", ""))
    artifacts = Path(artifacts or ROOT / "test-results")
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "organizations-browser.json").unlink(missing_ok=True)
    namespace = "org-browser-" + secrets.token_hex(5)
    marker = "ORG_PRIVATE_" + secrets.token_hex(8)
    judge = environment.get("BROWSER_JUDGE") == "1"
    receipt = {
        "passed": False,
        "https": base.startswith("https://"),
        "phase": "startup",
        "formal_judging": "requested" if judge else "not_run",
        "institutional_snapshots_retained": False,
    }
    accounts, errors = [], []
    published = withdrawn = False
    package_id = package_digest = course_digest = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=arguments)
        try:
            for prefix in ("org-owner-", "org-learner-"):
                accounts.append(
                    {
                        "username": prefix + secrets.token_hex(6),
                        "password": secrets.token_urlsafe(32),
                        "created": False,
                        "deleted": False,
                        "page": browser.new_context(
                            locale="zh-CN", accept_downloads=True
                        ).new_page(),
                    }
                )
            reviewer_page = browser.new_context(locale="zh-CN", accept_downloads=True).new_page()
            credentials = bootstrap_reviewer(owner_run, environment)
            username = credentials["BROWSER_ORG_REVIEWER_USERNAME"]
            password = credentials["BROWSER_ORG_REVIEWER_PASSWORD"]
            accounts.append(
                {
                    "username": username,
                    "password": password,
                    "created": True,
                    "deleted": False,
                    "page": reviewer_page,
                }
            )
            owner, learner, reviewer = accounts
            page, member, review = [account["page"] for account in accounts]
            guest = browser.new_context(locale="zh-CN").new_page()
            for current in (page, member, review, guest):
                current.on("pageerror", lambda error: errors.append(type(error).__name__))
            register(owner, base)
            register(learner, base)
            login(review, base, username, password)
            receipt["phase"] = "invitation_requires_acceptance"
            page.goto(base + "/organizations/new/")
            page.locator('[name="namespace"]').fill(namespace)
            page.locator('[name="name"]').fill("浏览器私有组织 " + namespace)
            page.locator("#id_description").fill("机构说明 <script>window.orgXss=true</script>")
            page.get_by_role("button", name="保存私有修订", exact=True).click()
            org_url = base + "/organizations/" + namespace + "/"
            expect(page).to_have_url(org_url)
            assert page.evaluate("typeof window.orgXss") == "undefined"
            assert_status(member, org_url, 404)
            assert_status(review, org_url, 404)
            invitation = page.locator(f'form[action="/organizations/{namespace}/invite/"]')
            invitation.locator('[name="username"]').fill(learner["username"])
            invitation.locator('[name="role"]').select_option("learner")
            invitation.get_by_role("button", name="发送邀请", exact=True).click()
            assert_status(member, org_url, 404)
            member.goto(base + "/organizations/")
            member.get_by_role("button", name="接受邀请", exact=True).click()
            assert_status(member, org_url, 200)
            assert member.get_by_text("成员与角色", exact=True).count() == 0
            receipt["invitation_and_member_privacy"] = True

            receipt["phase"] = "pinned_course_and_free_jump"
            units = []
            pins = {}
            for key, slug in (("first", "sum-positive"), ("second", "dot-product")):
                # Navigate through Chromium so the exact loopback certificate pin
                # applies. Playwright's separate API client has its own TLS trust.
                response = page.goto(base + f"/api/v1/problems/{slug}/")
                assert response.status == 200
                public = response.json()
                pins[slug] = public["source_digest"]
                units.append(
                    {
                        "key": key,
                        "title": "自由访问单元 " + key,
                        "description": "先后顺序不限制访问。",
                        "problem": slug,
                        "revision": public["problem"]["revision"],
                        "digest": public["source_digest"],
                    }
                )
            plan = {"title": "固定版本浏览器课程", "summary": "私人加入与自由跳转", "units": units}
            page.goto(org_url + "courses/new/")
            page.locator('[name="slug"]').fill("browser-course")
            page.locator('[name="plan"]').fill(json.dumps(plan, ensure_ascii=False))
            page.locator('[name="acknowledge"]').check()
            page.get_by_role("button", name="保存私有修订", exact=True).click()
            course_url = page.url
            course_digest = page.locator('form[action$="/publish/"] [name="digest"]').input_value()
            assert (
                course_digest
                == hashlib.sha256(
                    json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
                ).hexdigest()
            )
            assert_status(member, course_url, 404)
            page.get_by_role("button", name="发布这个课程修订", exact=True).click()
            assert_status(member, course_url, 200)
            member.get_by_role("button", name="私有加入这个课程版本", exact=True).click()
            member.goto(course_url)
            expect(member.locator("main")).to_contain_text("仅本人可见：已通过 0 个固定版本单元。")
            links = member.get_by_role("link", name="打开固定版本任务", exact=True)
            assert links.count() == 2
            second_url = links.nth(1).get_attribute("href")
            first_url = links.nth(0).get_attribute("href")
            member.goto(base + second_url)
            expect(
                member.get_by_role("heading", name="自由访问单元 second", exact=True)
            ).to_be_visible()
            member.goto(base + first_url)
            expect(
                member.get_by_role("heading", name="自由访问单元 first", exact=True)
            ).to_be_visible()
            page.goto(course_url)
            assert page.get_by_text("仅本人可见：已通过", exact=False).count() == 0
            assert_status(review, course_url, 404)
            receipt["exact_version_course_and_unlocked_units"] = True

            receipt["phase"] = "private_learner_evidence"
            member.goto(base + "/problems/sum-positive/")
            source = "def solve(values):\n    return sum(x for x in values if x > 0)\n# " + marker
            member.locator("#source").fill(source)
            member.get_by_role("button", name="保存草稿", exact=True).click()
            expect(member.locator("#editor-status")).to_have_text("草稿已私密保存")
            if judge:
                member.get_by_role("button", name="提交评测", exact=False).click()
                expect(member.get_by_role("heading", name="通过", exact=True)).to_be_visible(
                    timeout=180000
                )
                result_url = member.url
                assert_status(page, result_url, 404)
                assert_status(review, result_url, 404)
                member.goto(course_url)
                expect(member.locator("main")).to_contain_text(
                    "仅本人可见：已通过 1 个固定版本单元。"
                )
                receipt["formal_judging"] = "actual_accepted_and_private_course_progress"
            learner_export = export_account(member, base)
            assert marker in json.dumps(learner_export)
            assert len(learner_export["organizations"]["course_enrollments"]) == 1
            owner_export = export_account(page, base)
            assert marker not in json.dumps(owner_export)
            assert owner_export["organizations"]["course_enrollments"] == []
            del learner_export, owner_export
            receipt["own_enrollment_source_and_result_privacy"] = True

            receipt["phase"] = "independent_package_review"
            catalog = json.loads((ROOT / "content/starter.json").read_text())
            payload = synthetic_payload(catalog, marker)
            page.goto(org_url + "packages/new/")
            page.locator('[name="slug"]').fill("browser-package")
            page.locator('[name="payload"]').set_input_files(
                {
                    "name": "browser-package.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(payload, ensure_ascii=False).encode(),
                }
            )
            page.locator('[name="acknowledge"]').check()
            page.get_by_role("button", name="保存私有修订", exact=True).click()
            package_url = page.url
            package_id = urlsplit(package_url).path.split("/packages/")[1].split("/")[0]
            assert str(uuid.UUID(package_id)) == package_id
            form = page.locator('form[action$="/submit/"]')
            revision_id = form.get_attribute("action").split("/package-revisions/")[1].split("/")[0]
            package_digest = form.locator('[name="digest"]').input_value()
            public_url = base + "/organizations/packages/" + package_id + "/"
            problem_url = base + "/problems/" + namespace + "--browser-package--positive-sum/"
            assert_status(review, base + "/organizations/review/" + revision_id + "/", 404)
            assert_status(member, package_url, 403)
            assert_status(guest, public_url, 404)
            form.get_by_role("button", name="主动提交独立技术审核", exact=True).click()
            assert_status(page, base + "/organizations/review/" + revision_id + "/", 403)
            review.goto(base + "/organizations/review/")
            review.locator(f'a[href="/organizations/review/{revision_id}/"]').click()
            assert review.locator('[name="digest"]').input_value() == package_digest
            expect(review.locator("main")).to_contain_text(marker + "-reference")
            review.locator('[name="action"]').select_option("approve")
            review.locator('[name="note"]').fill(
                "独立核对精确投稿摘要；此浏览器流程不声称执行了投稿参考代码或获得公开许可。"
            )
            review.locator('[name="acknowledge"]').check()
            review.get_by_role("button", name="保存技术审核决定", exact=True).click()
            assert_status(guest, public_url, 404)
            assert_status(guest, problem_url, 404)
            checked_owner_action(owner_run, environment, "publish", revision_id, package_digest)
            published = True
            assert_status(guest, public_url, 200)
            assert marker not in guest.locator("body").inner_text()
            assert_status(guest, problem_url, 200)
            assert marker not in guest.locator("body").inner_text()
            receipt["independent_review_then_exact_owner_publication"] = True
            receipt["reference_and_hidden_tests_not_public"] = True

            receipt["phase"] = "owner_withdrawal_and_account_deletion"
            page.goto(package_url)
            page.get_by_role("button", name="请求可信维护者撤回本包", exact=True).click()
            assert_status(guest, public_url, 200)
            checked_owner_action(owner_run, environment, "withdraw", package_id, package_digest)
            withdrawn = True
            assert_status(guest, public_url, 404)
            assert_status(guest, problem_url, 404)
            assert (
                guest.goto(base + "/api/v1/problems/sum-positive/").json()["source_digest"]
                == pins["sum-positive"]
            )
            exported = export_account(page, base)
            assert len(exported["organizations"]["course_contributions"]) == 1
            assert len(exported["organizations"]["package_contributions"]) == 1
            assert exported["organizations"]["package_contributions"][0]["state"] == "withdrawn"
            assert marker in json.dumps(exported["organizations"]["package_contributions"])
            del exported
            receipt["withdrawal_is_scoped_and_export_is_own"] = True
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
            if published and not withdrawn:
                try:
                    checked_owner_action(
                        owner_run, environment, "withdraw", package_id, package_digest
                    )
                    withdrawn = True
                except Exception as error:
                    receipt["withdraw_cleanup_error"] = type(error).__name__
                    receipt["passed"] = False
            for account in accounts:
                if account["created"] and not account["deleted"]:
                    try:
                        delete_account(account, base)
                    except Exception as error:
                        receipt["cleanup_error"] = type(error).__name__
                        receipt["passed"] = False
            receipt["synthetic_accounts_deleted"] = len(accounts) == 3 and all(
                account["deleted"] for account in accounts
            )
            browser.close()
    if receipt["passed"] and receipt["synthetic_accounts_deleted"]:
        try:
            verify_retention(
                owner_run,
                environment,
                namespace,
                [row["username"] for row in accounts],
                course_digest,
                package_digest,
            )
            receipt["institutional_snapshots_retained"] = True
            receipt["deleted_actor_links_cleared"] = True
        except Exception as error:
            receipt["retention_error"] = type(error).__name__
            receipt["passed"] = False
    receipt["completed_at"] = datetime.now(UTC).isoformat()
    target = artifacts / "organizations-browser.json"
    target.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
    target.chmod(0o600)
    if not receipt["passed"]:
        raise RuntimeError("Organization browser journey failed; see sanitized receipt")
    return receipt


def local():
    artifacts = ROOT / "test-results"
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="praximesh-org-browser-") as temporary:
        environment = dict(
            os.environ,
            PRAXIMESH_MODE="local",
            DATABASE_URL="",
            SQLITE_PATH=str(Path(temporary) / "app.sqlite3"),
            APP_SECRET_KEY=secrets.token_urlsafe(48),
            REGISTRATION_OPEN="1",
            APP_ALLOWED_HOSTS="127.0.0.1,localhost",
            JUDGE_ENABLED="0",
            BROWSER_JUDGE="0",
        )
        for key in ("BROWSER_TEST_SPKI", "BROWSER_CA_FILE"):
            environment.pop(key, None)

        def run(*arguments, env):
            subprocess.run(
                [sys.executable, *arguments],
                cwd=ROOT,
                env=env,
                check=True,
                capture_output=True,
                timeout=120,
            )

        run("manage.py", "migrate", "--noinput", "--verbosity=0", env=environment)
        run("manage.py", "content_import", env=environment)
        with socket.socket() as allocated:
            allocated.bind(("127.0.0.1", 0))
            port = allocated.getsockname()[1]
        environment["BROWSER_URL"] = f"http://127.0.0.1:{port}"
        with (artifacts / "organizations-browser-server.log").open("w") as log:
            server = subprocess.Popen(
                [sys.executable, "manage.py", "runserver", f"127.0.0.1:{port}", "--noreload"],
                cwd=ROOT,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                for _ in range(100):
                    if server.poll() is not None:
                        raise RuntimeError("Owned application exited")
                    try:
                        with opener.open(environment["BROWSER_URL"] + "/health/", timeout=1):
                            break
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Owned application did not start")
                return run_journey(environment, run, artifacts)
            finally:
                if server.poll() is None:
                    server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait(timeout=3)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", action="store_true", required=True)
    parser.parse_args()
    result = local()
    print(
        json.dumps(
            {
                "passed": result["passed"],
                "formal_judging": result["formal_judging"],
                "institutional_snapshots_retained": result["institutional_snapshots_retained"],
            }
        )
    )
    raise SystemExit(0 if result["passed"] else 1)
