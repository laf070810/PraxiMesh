"""Synthetic sharing journey, called only by the guarded disposable GPU UI check."""

import uuid
from urllib.parse import urlparse

from django.urls import reverse
from playwright.sync_api import expect

from tests.gpu_evidence_fixtures import job_fixture


def prepare_gpu_showcase(user, other, version):
    """Seed only the owned DB, before Playwright starts its event loop."""

    def timing(owner, median):
        item = job_fixture(owner, version)
        item.result["performance"].update(samples_ms=[median] * 5, median_batch_ms=median)
        item.save(update_fields=["result"])
        return item

    return {
        language: {
            "own": timing(user, 2.3455),
            "peer": timing(other, 2.3456),
            "private": timing(user, 0.1),
            "failed": job_fixture(user, version, verdict="wrong_answer", batch_failed=True),
        }
        for language in ("zh-hans", "en")
    }


def check_gpu_showcase(browser, address, user, other, password, version, fixtures, out, record):
    record["showcase_checks"] = []

    def url(name, *args):
        return address + reverse("showcase:" + name, args=args)

    def login(page, owner):
        page.goto(address + reverse("login"))
        page.locator("input[name=username]").fill(owner.username)
        page.locator("input[name=password]").fill(password)
        page.locator('form:has(input[name="password"]) button').click()
        page.wait_for_url(lambda value: reverse("login") not in value)

    def enable(page, label):
        page.goto(url("settings"))
        page.locator("#id_public_name").fill(label)
        page.locator("#id_bio").fill("Synthetic browser record; no GPU execution.")
        page.locator("#id_is_public").check()
        page.locator("#id_confirm_public").check()
        page.locator('form:has(input[name="public_name"]) button').click()
        expect(page.locator(f'form[action="{reverse("showcase:close")}"]')).to_be_visible()

    def publish(page, item):
        page.goto(url("share", item.pk))
        page.locator("#id_confirm_public").focus()
        page.keyboard.press("Space")
        expect(page.locator("#id_confirm_public")).to_be_checked()
        page.locator('form:has(input[name="profile_revision"]) button').click()
        page.wait_for_url("**/showcase/results/**")
        return uuid.UUID(urlparse(page.url).path.rstrip("/").split("/")[-1])

    def checked_layout(page, language, width, kind):
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        for choice in page.locator('input[type="checkbox"]:visible').all():
            offsets = choice.evaluate("""element => {
                const input = element.getBoundingClientRect();
                const label = element.labels[0].getBoundingClientRect();
                return {right: input.right, top: input.top,
                        labelLeft: label.left, labelTop: label.top};
            }""")
            assert 0 <= offsets["labelLeft"] - offsets["right"] <= 12
            assert 0 <= offsets["top"] - offsets["labelTop"] <= 8
        record["showcase_checks"].append({"language": language, "width": width, "page": kind})

    for language in ("zh-hans", "en"):
        contexts = [browser.new_context(locale=language) for _ in range(3)]
        for context in contexts:
            context.add_cookies([{"name": "django_language", "value": language, "url": address}])
        owner_page, other_page, guest = [context.new_page() for context in contexts]
        errors = []
        for page in (owner_page, other_page, guest):
            page.on(
                "pageerror", lambda error, captured=errors: captured.append(type(error).__name__)
            )
        own, peer, private, failed = (
            fixtures[language][key] for key in ("own", "peer", "private", "failed")
        )
        try:
            login(owner_page, user)
            login(other_page, other)
            enable(owner_page, "公开 GPU 作品 · Public GPU work")
            enable(other_page, "另一位参与者 · Another participant")
            for width in (390, 768, 1440):
                owner_page.set_viewport_size({"width": width, "height": 900})
                checked_layout(owner_page, language, width, "profile-settings")
            owner_page.goto(address + reverse("submission", args=[own.pk]))
            owner_page.locator(f'a[href="{reverse("showcase:share", args=[own.pk])}"]').click()
            assert not owner_page.locator("#id_confirm_public").is_checked()
            owner_page.locator('form:has(input[name="profile_revision"]) button').click()
            assert urlparse(owner_page.url).path == reverse("showcase:share", args=[own.pk])
            assert owner_page.locator("#id_confirm_public:invalid").count() == 1
            guest.goto(url("profile", user.username))
            assert guest.locator("article.card").count() == 0
            for width in (390, 768, 1440):
                owner_page.set_viewport_size({"width": width, "height": 900})
                checked_layout(owner_page, language, width, "consent-preview")
                if width == 390:
                    owner_page.screenshot(
                        path=str(out / f"{language}-390-gpu-share.png"), full_page=True
                    )
            entry = publish(owner_page, own)
            peer_entry = publish(other_page, peer)
            board = url("board", version.pk, own.evaluation_fingerprint)
            pages = {
                "result": url("entry", entry),
                "profile": url("profile", user.username),
                "groups": url("boards"),
                "board": board,
            }
            for width in (390, 768, 1440):
                guest.set_viewport_size({"width": width, "height": 900})
                for kind, endpoint in pages.items():
                    response = guest.goto(endpoint)
                    assert response.status == 200
                    checked_layout(guest, language, width, kind)
                    body = guest.locator("body").inner_text()
                    for forbidden in (
                        "PRIVATE_LEARNER_SOURCE_SENTINEL",
                        "PRIVATE_REFERENCE_SENTINEL",
                        "PROTECTED_CASE_SENTINEL",
                        own.environment["target"]["uuid"],
                        own.environment["target"]["model"],
                        "2.3455",
                        str(private.pk),
                        str(failed.pk),
                    ):
                        assert forbidden not in body
                    if kind == "board":
                        assert guest.locator("tbody tr").count() == 2
                        assert guest.locator("tbody tr td:first-child").all_text_contents() == [
                            "1",
                            "1",
                        ]
                        assert guest.locator("tbody tr td:nth-child(3)").all_text_contents() == [
                            "2.346",
                            "2.346",
                        ]
                        if language == "en":
                            assert "rounded half up to 0.001 ms" in body
                            assert "GPU batch timing leaderboard" in body
                        if width in (390, 1440):
                            guest.screenshot(
                                path=str(out / f"{language}-{width}-gpu-board.png"), full_page=True
                            )
            assert owner_page.goto(url("share", failed.pk)).status == 400
            assert other_page.goto(url("share", own.pk)).status == 404
            owner_page.goto(url("mine"))
            owner_page.locator(
                f'form[action="{reverse("showcase:withdraw", args=[entry])}"] button'
            ).click()
            owner_page.wait_for_url("**/showcase/mine/")
            assert guest.goto(url("entry", entry)).status == 404
            guest.goto(board)
            assert guest.locator("tbody tr").count() == 1
            for page in (owner_page, other_page):
                page.goto(url("settings"))
                page.locator(f'form[action="{reverse("showcase:close")}"] button').click()
                page.wait_for_url("**/showcase/settings/")
            assert guest.goto(url("entry", peer_entry)).status == 404
            assert guest.goto(board).status == 404
            assert guest.goto(url("profile", user.username)).status == 404
            assert guest.goto(url("profile", other.username)).status == 404
            assert not errors, errors
            record["showcase_checks"].append(
                {
                    "language": language,
                    "consent_keyboard": True,
                    "private_best_excluded": True,
                    "other_account_denied": True,
                    "withdrawn": True,
                    "closed": True,
                }
            )
        except Exception:
            owner_page.screenshot(
                path=str(out / f"{language}-gpu-share-failure.png"), full_page=True
            )
            raise
        finally:
            for context in contexts:
                context.close()
