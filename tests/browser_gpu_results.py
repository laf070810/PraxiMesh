"""Synthetic GPU result rendering on browser_acceptance's disposable local DB."""

import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    database = Path(os.environ.get("SQLITE_PATH", "")).resolve(strict=True)
    address = os.environ.get("BROWSER_URL", "")
    target = urlparse(address)
    if (
        os.environ.get("BROWSER_GPU_RESULTS") != "1"
        or os.environ.get("JUDGE_ENABLED") != "0"
        or os.environ.get("DATABASE_URL")
        or target.scheme != "http"
        or target.hostname != "127.0.0.1"
        or not database.parent.name.startswith("praximesh-browser-")
        or database.parent.stat().st_uid != os.getuid()
        or stat.S_IMODE(database.parent.stat().st_mode) != 0o700
    ):
        raise ValueError("Use scripts/browser_acceptance.py --gpu-results with its disposable DB")
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "praximesh.settings")
    import django

    django.setup()
    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.urls import reverse
    from playwright.sync_api import sync_playwright

    from core.models import GPUDevice
    from tests.gpu_evidence_fixtures import job_fixture, version_fixture

    assert Path(settings.DATABASES["default"]["NAME"]).resolve() == database
    assert not GPUDevice.objects.exists()
    token = secrets.token_hex(6)
    out = ROOT / "test-results" / ("px-gpu-results-" + token)
    out.mkdir(mode=0o700)
    password = secrets.token_urlsafe(32)
    user = get_user_model().objects.create_user(username="gpu-view-" + token, password=password)
    other = get_user_model().objects.create_user(username="gpu-other-" + token, password=password)
    version = version_fixture()
    jobs = {
        "accepted": job_fixture(user, version),
        "batch-failed": job_fixture(user, version, verdict="wrong_answer", batch_failed=True),
        "run": job_fixture(user, version, mode="run"),
        "corrupt": job_fixture(user, version),
    }
    jobs["corrupt"].result["total"] = "PROTECTED_COUNT_SENTINEL"
    jobs["corrupt"].save(update_fields=["result"])
    record = {
        "scope": "Synthetic GPU result UI only; no CUDA, startup receipt or performance acceptance",
        "database": str(database),
        "gpu_executed": False,
        "passed": False,
        "checks": [],
        "sources": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "core/gpu_evidence.py",
                "core/result_evidence.py",
                "core/content.py",
                "core/views.py",
                "judge/backends.py",
                "judge/gpu_execution_contract.py",
                "judge/gpu_policy.py",
                "templates/submission.html",
                "templates/gpu_feedback.html",
                "templates/resource_reservation.html",
                "locale/en/LC_MESSAGES/django.mo",
                "tests/gpu_evidence_fixtures.py",
                "tests/browser_gpu_results.py",
                "scripts/browser_acceptance.py",
            )
        },
    }

    def save():
        (out / "result.json").write_text(json.dumps(record, indent=2) + "\n")

    def login(page, username):
        page.goto(address + reverse("login"))
        page.locator("input[name=username]").fill(username)
        page.locator("input[name=password]").fill(password)
        page.locator('form:has(input[name="password"]) button').click()
        page.wait_for_url(lambda url: reverse("login") not in url)

    save()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for language in ("zh-hans", "en"):
                    context = browser.new_context(locale=language)
                    context.add_cookies(
                        [{"name": "django_language", "value": language, "url": address}]
                    )
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error, captured=errors: captured.append(str(error)))
                    login(page, user.username)
                    for width in (390, 768, 1440):
                        page.set_viewport_size({"width": width, "height": 900})
                        for kind, job in jobs.items():
                            response = page.goto(address + reverse("submission", args=[job.pk]))
                            assert response.status == 200
                            text = page.locator("body").inner_text()
                            assert "PRIVATE_LEARNER_SOURCE_SENTINEL" in text
                            for forbidden in (
                                "PRIVATE_REFERENCE_SENTINEL",
                                "PROTECTED_CASE_SENTINEL",
                                "PROTECTED_COUNT_SENTINEL",
                            ):
                                assert forbidden not in text
                            label = (
                                "控制器批次往返时间中位数"
                                if language == "zh-hans"
                                else "Controller round-trip batch median"
                            )
                            units = (
                                "秒 GPU 整卡时段"
                                if language == "zh-hans"
                                else "GPU device-slot seconds"
                            )
                            assert units in text
                            assert (label in text) == (kind == "accepted")
                            if kind == "accepted":
                                assert "12.500" in text and "3.000" in text
                            if kind == "batch-failed":
                                assert (
                                    "重复批次仍可能失败"
                                    if language == "zh-hans"
                                    else "Repeated batches can fail"
                                ) in text
                            assert not page.locator(
                                'a[href="' + reverse("showcase:share", args=[job.pk]) + '"]'
                            ).count()
                            assert page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth + 1"
                            )
                            box = page.locator(".result-panel").bounding_box()
                            assert box and box["x"] >= 0 and box["x"] + box["width"] <= width + 1
                            if width == 390 and kind == "accepted":
                                page.screenshot(
                                    path=str(out / (language + "-390.png")), full_page=True
                                )
                            record["checks"].append(
                                {"language": language, "width": width, "case": kind, "passed": True}
                            )
                    assert not errors, errors
                    context.close()
                context = browser.new_context()
                page = context.new_page()
                login(page, other.username)
                response = page.goto(address + reverse("submission", args=[jobs["accepted"].pk]))
                assert (
                    response.status == 404
                    and "PRIVATE_LEARNER_SOURCE_SENTINEL" not in page.locator("body").inner_text()
                )
                record["other_account_denied"] = True
                context.close()
            finally:
                browser.close()
        assert not GPUDevice.objects.exists()
        record["passed"] = True
    finally:
        save()
    print(json.dumps({"passed": True, "report": str(out / "result.json")}), flush=True)


if __name__ == "__main__":
    main()
