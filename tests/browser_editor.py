"""Real local editor and layout checks; no learner code execution or GPU use."""

import json
import os
import re
import secrets
import uuid
from pathlib import Path

from browser_community import launch_arguments
from playwright.sync_api import expect, sync_playwright


def fill(page, source):
    editor = page.locator(".cm-content[contenteditable]")
    editor.click()
    editor.press("ControlOrMeta+a")
    editor.press("Backspace")
    page.keyboard.insert_text(source)
    expect(page.locator("#source")).to_have_value(source)


def layouts(page, base, artifacts, *, baseline=False):
    metrics = []
    for language in ("zh-hans", "en"):
        page.context.add_cookies([{"name": "django_language", "value": language, "url": base}])
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            for route in (
                "practice",
                "learn",
                "coaching/goals",
                "organizations",
                "problems/sum-positive",
            ):
                page.goto(f"{base}/{route}/")
                if route.startswith("problems/") and not baseline:
                    expect(page.locator(".cm-content")).to_be_visible()
                item = page.evaluate("""() => ({
                    overflow: document.documentElement.scrollWidth > innerWidth,
                    heading: document.querySelector('h1').getBoundingClientRect().x,
                    filters: [...document.querySelectorAll('.filters label')].map(e => ({
                        text: e.firstChild.textContent, rect: e.getBoundingClientRect().toJSON(),
                        control: e.querySelector('input, select').getBoundingClientRect().toJSON()
                    })),
                    choices: [...document.querySelectorAll('input[type=checkbox]')].map(e => ({
                        rect: e.getBoundingClientRect().toJSON(),
                        label: e.closest('label')?.getBoundingClientRect().toJSON()
                    }))
                })""")
                metrics.append({"language": language, "width": width, "route": route, **item})
                page.screenshot(
                    path=str(artifacts / f"{language}-{width}-{route.replace('/', '-')}.png"),
                    full_page=True,
                )
                if not baseline:
                    assert not item["overflow"], metrics[-1]
                    assert item["heading"] >= 16, metrics[-1]
                    for choice in item["choices"]:
                        assert choice["rect"]["width"] <= 24
                        assert choice["label"]["height"] >= 28
                    # Controls in a filter row share a bottom edge, even with long labels.
                    rows = {}
                    for field in item["filters"]:
                        rows.setdefault(round(field["rect"]["y"]), []).append(field["control"])
                    for controls in rows.values():
                        assert (
                            max(c["bottom"] for c in controls) - min(c["bottom"] for c in controls)
                            <= 2
                        )
    (artifacts / "layouts.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2))
    page.context.add_cookies([{"name": "django_language", "value": "zh-hans", "url": base}])


def main():
    base = os.environ.get("BROWSER_URL", "http://127.0.0.1:8765").rstrip("/")
    baseline = os.environ.get("EDITOR_LAYOUT_BASELINE") == "1"
    artifacts = Path(os.environ.get("BROWSER_ARTIFACTS", "test-results")) / (
        "editor-before" if baseline else "editor"
    )
    artifacts.mkdir(parents=True, exist_ok=True)
    password = secrets.token_urlsafe(28)
    errors, violations = [], []
    created = False
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=launch_arguments(base, os.environ.get("BROWSER_TEST_SPKI", ""))
        )
        context = browser.new_context(locale="zh-CN", viewport={"width": 1440, "height": 1050})
        page = context.new_page()
        context.tracing.start(screenshots=True, snapshots=True)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on(
            "console",
            lambda message: (
                violations.append(message.text)
                if "Content Security Policy" in message.text
                else None
            ),
        )
        try:
            page.goto(base + "/accounts/register/")
            page.locator("#id_username").fill("editor-" + uuid.uuid4().hex[:12])
            page.locator("#id_password1").fill(password)
            page.locator("#id_password2").fill(password)
            page.get_by_role("button", name="注册", exact=True).click()
            expect(page.locator(".recovery-code")).to_be_visible()
            created = True
            if baseline:
                layouts(page, base, artifacts, baseline=True)
                return
            response = page.goto(base + "/problems/sum-positive/")
            policy = response.headers["content-security-policy"]
            assert "'wasm-unsafe-eval'" in policy and "'unsafe-eval'" not in policy
            assert "'unsafe-inline'" not in policy
            expect(page.locator("#source")).to_be_hidden()
            expect(page.locator(".cm-gutters")).to_be_visible()
            expect(page.locator("#editor-analysis")).to_contain_text("本地检查", timeout=30000)
            code = "def solve(values):\n    return sum(x for x in values if x > 0)\n"
            fill(page, code)
            expect(page.locator(".cm-line span").first).to_be_visible()
            page.keyboard.press("Control+s")
            expect(page.locator("#editor-status")).to_have_text("草稿已私密保存")
            page.reload()
            expect(page.locator("#source")).to_have_value(code)
            fill(page, "def solve(values):\n    return missing_name\n")
            expect(page.locator("#editor-analysis")).to_contain_text("检查提示", timeout=30000)
            page.get_by_role("button", name="检查代码", exact=True).click()
            expect(page.locator(".cm-diagnostic")).to_contain_text("F821")
            page.locator('.cm-panel-lint [name="close"]').click()
            messy = "def solve( values ):\n return sum( values )\n"
            fill(page, messy)
            page.keyboard.press("Alt+Shift+f")
            expect(page.locator("#source")).to_have_value(
                "def solve(values):\n    return sum(values)\n"
            )
            page.keyboard.press("Control+z")
            expect(page.locator("#source")).to_have_value(messy)
            fill(page, "def solve(values):\n    return su")
            page.keyboard.press("Control+End")
            page.keyboard.press("Control+Space")
            expect(page.locator(".cm-tooltip-autocomplete")).to_contain_text("sum")
            # CodeMirror protects the first 75 ms from accidental acceptance.
            page.wait_for_timeout(100)
            page.keyboard.press("Enter")
            expect(page.locator("#source")).to_have_value(re.compile(r"return sum"))
            page.keyboard.press("Escape")
            page.keyboard.press("Tab")
            assert not page.locator(".cm-content").evaluate(
                "e => e.contains(document.activeElement)"
            )
            fill(page, "value = 1")
            page.keyboard.press("Control+Home")
            page.keyboard.press("Tab")
            expect(page.locator("#source")).to_have_value("    value = 1")
            page.keyboard.press("Shift+Tab")
            expect(page.locator("#source")).to_have_value("value = 1")
            page.keyboard.press("Control+/")
            expect(page.locator("#source")).to_have_value("# value = 1")
            page.keyboard.press("Control+f")
            expect(page.locator(".cm-search")).to_be_visible()
            page.locator('.cm-search input[name="search"]').fill("value")
            page.locator('.cm-search input[name="search"]').press("Enter")
            expect(page.locator(".cm-searchMatch")).to_be_visible()
            page.keyboard.press("Escape")
            fill(page, "def solve(:\n")
            page.get_by_role("button", name="格式化", exact=True).click()
            expect(page.locator("#editor-analysis")).to_have_text("请先修复语法错误，再格式化。")
            expect(page.locator("#source")).to_have_value("def solve(:\n")
            fill(page, code)
            page.locator(".cm-content").press("Control+End")
            page.keyboard.insert_text("x" * 20001)
            expect(page.locator("#source")).to_have_value(code)
            expect(page.locator("#editor-analysis")).to_have_text("代码不能超过 20000 字符。")
            if os.environ.get("BROWSER_JUDGE") == "1":
                page.keyboard.press("Control+Enter")
                expect(page.get_by_role("heading", name="通过", exact=True)).to_be_visible(
                    timeout=180000
                )
                page.goto(base + "/problems/sum-positive/")
            else:
                expect(page.locator('[name="mode"][value="run"]')).to_be_disabled()
                page.keyboard.press("Control+Enter")
                assert page.url.endswith("/problems/sum-positive/")
            page.get_by_role("button", name="保存草稿", exact=True).click()
            expect(page.locator("#editor-status")).to_have_text("草稿已私密保存")
            page.screenshot(path=str(artifacts / "python-editor.png"), full_page=True)

            page.goto(base + "/practice/?language=javascript")
            page.locator(".problem-row").first.click()
            fill(page, "function solve(values){return values.map(x=>x+1)}")
            page.get_by_role("button", name="格式化", exact=True).click()
            expect(page.locator("#source")).to_have_value(
                "function solve(values) {\n  return values.map((x) => x + 1);\n}\n"
            )
            fill(page, "function solve( {")
            page.get_by_role("button", name="检查代码", exact=True).click()
            expect(page.locator(".cm-diagnostic")).to_contain_text("Unexpected")
            page.get_by_role("button", name="保存草稿", exact=True).click()
            expect(page.locator("#editor-status")).to_have_text("草稿已私密保存")

            # Delay delivery to the real formatter to exercise a user typing
            # while a request is pending. Formatting results are not mocked.
            page.add_init_script("""(() => {
                const NativeWorker = window.Worker;
                window.Worker = class extends NativeWorker {
                    postMessage(message, ...args) {
                        if (message.operation === 'format') {
                            setTimeout(() => super.postMessage(message, ...args), 300);
                        } else super.postMessage(message, ...args);
                    }
                };
            })()""")
            page.goto(base + "/problems/sum-positive/")
            fill(page, messy)
            page.get_by_role("button", name="格式化", exact=True).click()
            changed = "def solve(values):\n    return 123  # newer edit\n"
            fill(page, changed)
            expect(page.locator("#editor-analysis")).to_have_text(
                "格式化期间代码已修改，请重新格式化。"
            )
            expect(page.locator("#source")).to_have_value(changed)
            page.get_by_role("button", name="保存草稿", exact=True).click()
            expect(page.locator("#editor-status")).to_have_text("草稿已私密保存")

            # Exercise the translated editor controls, not only English page headings.
            context.add_cookies([{"name": "django_language", "value": "en", "url": base}])
            page.reload()
            fill(page, "def solve(values):\n    return unknown_name\n")
            page.get_by_role("button", name="Check code", exact=True).click()
            expect(page.locator("#editor-analysis")).to_contain_text("diagnostics")
            expect(page.get_by_text("Shortcuts and editor help", exact=True)).to_be_visible()
            page.get_by_role("button", name="Save draft", exact=True).click()
            expect(page.locator("#editor-status")).to_have_text("Draft saved privately")
            context.add_cookies([{"name": "django_language", "value": "zh-hans", "url": base}])

            # Optional resources fail without removing the original source form.
            fallback = browser.new_context(
                locale="zh-CN", java_script_enabled=False, storage_state=context.storage_state()
            )
            fallback_page = fallback.new_page()
            fallback_page.goto(base + "/problems/sum-positive/")
            expect(fallback_page.locator("#source")).to_be_visible()
            fallback_page.locator("#source").fill(code)
            fallback_page.get_by_role("button", name="保存草稿", exact=True).click()
            expect(fallback_page.locator("#source")).to_have_value(code)
            fallback.close()
            blocked = browser.new_context(locale="zh-CN")
            blocked.route("**/editor/editor*.js", lambda route: route.abort())
            blocked_page = blocked.new_page()
            blocked_page.goto(base + "/problems/sum-positive/")
            expect(blocked_page.locator("#source")).to_be_visible()
            blocked.close()
            failed_tools = browser.new_context(
                locale="zh-CN", storage_state=context.storage_state()
            )
            failed_tools.route("**/editor/*.wasm", lambda route: route.abort())
            failed_page = failed_tools.new_page()
            failed_page.goto(base + "/problems/sum-positive/")
            expect(failed_page.locator("#editor-analysis")).to_contain_text(
                "本地检查工具暂不可用", timeout=30000
            )
            fill(
                failed_page, code + "# preserved despite tool failure; " + "long line " * 30 + "\n"
            )
            failed_page.get_by_role("button", name="保存草稿", exact=True).click()
            expect(failed_page.locator("#editor-status")).to_have_text("草稿已私密保存")
            failed_tools.close()
            layouts(page, base, artifacts)
            assert not errors, errors
            assert not violations, violations
            print("Editor and layout browser checks passed; artifacts: " + str(artifacts))
        except Exception:
            page.screenshot(path=str(artifacts / "failure.png"), timeout=10000)
            (artifacts / "errors.json").write_text(
                json.dumps({"errors": errors, "csp": violations})
            )
            raise
        finally:
            context.tracing.stop(path=str(artifacts / "trace.zip"))
            if created:
                context.add_cookies([{"name": "django_language", "value": "zh-hans", "url": base}])
                page.goto(base + "/accounts/profile/")
                deletion = page.locator("form.danger")
                deletion.get_by_label("当前密码").fill(password)
                deletion.get_by_label("输入 DELETE 确认").fill("DELETE")
                deletion.get_by_role("button", name="永久删除当前账号").click()
                expect(page.get_by_role("heading", name="账号已删除")).to_be_visible()
            browser.close()


if __name__ == "__main__":
    main()
