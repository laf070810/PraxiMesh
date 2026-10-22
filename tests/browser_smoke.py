"""Real browser checks against a running, isolated local app.

Creates and deletes its own account. With BROWSER_JUDGE=1, additionally exercises
actual code submission; run only after real sandbox release gates have passed.
"""

import base64
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def main():
    base = os.environ.get("BROWSER_URL", "http://127.0.0.1:8765")
    username = f"browser-{uuid.uuid4().hex[:12]}"
    password = "Browser-only-secret-9162"
    artifacts = Path("test-results")
    artifacts.mkdir(exist_ok=True)
    errors = []
    launch_args = []
    pin = os.environ.get("BROWSER_TEST_SPKI", "")
    if pin:
        address = urlsplit(base)
        if address.scheme != "https" or address.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Test certificate pins are permitted only for loopback HTTPS.")
        if len(base64.b64decode(pin, validate=True)) != 32:
            raise ValueError("Expected a SHA-256 SPKI pin.")
        # Scope the test exception to this exact ephemeral key. Do not use
        # ignore_https_errors or install a CA in a system-wide trust store.
        launch_args.append("--ignore-certificate-errors-spki-list=" + pin)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=launch_args)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.on("pageerror", lambda error: errors.append(str(error)))
        response = page.goto(base)
        if base.startswith("https://"):
            assert "max-age=" in response.headers["strict-transport-security"]
            assert page.evaluate("window.isSecureContext")
        expect(page.locator("h1")).to_contain_text("变成“我做到了”。")
        page.screenshot(path=str(artifacts / "home-desktop.png"), full_page=True)
        page.goto(base + "/learn/vectors/")
        expect(page.get_by_role("heading", name="向量与点积", exact=True)).to_be_visible()
        page.get_by_role("link", name="向量的点积", exact=False).click()
        expect(page.get_by_label("Python 源代码")).to_be_visible()
        page.goto(base + "/accounts/register/")
        page.get_by_label("用户名", exact=False).fill(username)
        page.locator("#id_password1").fill(password)
        page.locator("#id_password2").fill(password)
        page.get_by_role("button", name="注册", exact=True).click()
        expect(page.get_by_role("heading", name="保存你的恢复码。")).to_be_visible()
        assert len(page.locator(".recovery-code").inner_text()) > 30
        if base.startswith("https://"):
            cookies = {item["name"]: item for item in page.context.cookies()}
            assert cookies["sessionid"]["secure"] and cookies["sessionid"]["httpOnly"]
            assert cookies["sessionid"]["sameSite"] == "Lax"
            assert cookies["csrftoken"]["secure"]
        page.goto(base + "/problems/sum-positive/")
        source = "def solve(values):\n    return sum(x for x in values if x > 0)\n"
        page.get_by_label("Python 源代码").fill(source)
        page.get_by_role("button", name="保存草稿").click()
        expect(page.locator("#editor-status")).to_have_text("草稿已私密保存")
        page.reload()
        expect(page.get_by_label("Python 源代码")).to_have_value(source)
        if os.environ.get("BROWSER_JUDGE") == "1":
            page.get_by_role("button", name="运行样例", exact=True).click()
            expect(page.get_by_role("heading", name="通过", exact=True)).to_be_visible(
                timeout=180000
            )
            expect(
                page.get_by_text("本次只运行公开样例，不计入正式完成状态。", exact=True)
            ).to_be_visible()
            page.goto(base + "/me/")
            expect(page.get_by_text("已通过 0 道", exact=False)).to_be_visible()
            page.goto(base + "/problems/sum-positive/")
            page.get_by_label("Python 源代码").fill("def solve(values): return -1")
            page.get_by_role("button", name="提交评测", exact=False).click()
            expect(page.get_by_role("heading", name="答案有误", exact=True)).to_be_visible(
                timeout=180000
            )
            page.goto(base + "/problems/sum-positive/")
            page.get_by_label("Python 源代码").fill(source)
            page.get_by_role("button", name="提交评测", exact=False).click()
            expect(page.get_by_role("heading", name="通过", exact=True)).to_be_visible(
                timeout=180000
            )
        else:
            expect(page.get_by_role("button", name="提交评测", exact=False)).to_be_disabled()
            expect(page.get_by_text("评测暂不可用。", exact=False)).to_be_visible()
        page.screenshot(path=str(artifacts / "practice-desktop.png"), full_page=True)
        page.goto(base + "/learn/python-to-numerical/")
        page.get_by_role("button", name="加入我的路径").click()
        expect(page.get_by_role("button", name="退出路径追踪")).to_be_visible()
        if os.environ.get("BROWSER_JUDGE") == "1":
            expect(page.get_by_text("1 / 8 道当前版本练习已通过。", exact=False)).to_be_visible()
        page.goto(base + "/me/")
        expect(page.get_by_role("heading", name="提交历史")).to_be_visible()
        if os.environ.get("BROWSER_JUDGE") == "1":
            expect(page.get_by_text("已通过 1 道", exact=False)).to_be_visible()
            expect(page.locator("tbody tr")).to_have_count(3)
        page.goto(base + "/practice/")
        page.get_by_role("searchbox").fill("Softmax")
        page.get_by_role("button", name="筛选", exact=True).click()
        expect(page.locator(".problem-row")).to_have_count(1)
        page.set_viewport_size({"width": 390, "height": 844})
        page.goto(base + "/")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(artifacts / "home-mobile.png"), full_page=True)
        page.goto(base + "/problems/stable-softmax/")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.goto(base + "/accounts/profile/")
        deletion = page.locator("form.danger")
        deletion.get_by_label("当前密码").fill(password)
        deletion.get_by_label("输入 DELETE 确认").fill("DELETE")
        deletion.get_by_role("button", name="永久删除当前账号").click()
        expect(page.get_by_role("heading", name="账号已删除")).to_be_visible()
        assert not errors, errors
        browser.close()
    print(
        "Browser flow passed: navigation, registration, private draft, path enrollment, search, mobile layout, deletion. Judge mode="
        + os.environ.get("BROWSER_JUDGE", "0")
    )


if __name__ == "__main__":
    main()
