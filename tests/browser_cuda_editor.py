"""Called only with the owned, evaluation-disabled GPU UI fixture."""

import re

from browser_editor import fill
from playwright.sync_api import expect


def check_cuda_editor(page, address, slug, out, language):
    english = language == "en"
    url = address + "/problems/" + slug + "/"
    labels = {
        "check": "Check code" if english else "检查代码",
        "format": "Format" if english else "格式化",
        "save": "Save draft" if english else "保存草稿",
        "saved": "Draft saved privately" if english else "草稿已私密保存",
        "valid": "No structure errors found" if english else "未发现代码结构错误",
        "diagnostic": "Possible syntax structure error" if english else "代码结构可能有语法错误",
    }
    page.goto(url)
    expect(page.locator(".cm-content")).to_be_visible()
    expect(page.locator("#source")).to_be_hidden()
    code = 'extern "C" __global__ void solve(float* out){int i=threadIdx.x;out[i]=i;}'
    fill(page, code)
    expect(page.locator("#editor-analysis")).to_contain_text(labels["valid"])
    keyword = page.locator(".cm-line span").filter(has_text="__global__").first
    expect(keyword).to_be_visible()
    assert keyword.evaluate("e => getComputedStyle(e).color") != "rgb(237, 244, 231)"
    page.keyboard.press("Control+s")
    expect(page.locator("#editor-status")).to_have_text(labels["saved"])
    page.reload()
    expect(page.locator("#source")).to_have_value(code)
    fill(page, 'extern "C" __global__ void solve(){ int i = threadI')
    page.keyboard.press("Control+End")
    page.keyboard.press("Control+Space")
    expect(page.locator(".cm-tooltip-autocomplete")).to_contain_text("threadIdx")
    page.wait_for_timeout(100)
    page.keyboard.press("Enter")
    expect(page.locator("#source")).to_have_value(re.compile(r"threadIdx$"))
    fill(page, "void solve() { int customValue = 1; int other = customV")
    page.keyboard.press("Control+End")
    page.keyboard.press("Control+Space")
    expect(page.locator(".cm-tooltip-autocomplete")).to_contain_text("customValue")
    page.keyboard.press("Escape")
    page.keyboard.press("Tab")
    assert not page.locator(".cm-content").evaluate("e => e.contains(document.activeElement)")
    fill(page, code)
    page.keyboard.press("Alt+Shift+f")
    formatted = 'extern "C" __global__ void solve(float *out) {\n    int i = threadIdx.x;\n    out[i] = i;\n}'
    expect(page.locator("#source")).to_have_value(formatted, timeout=30000)
    page.keyboard.press("Control+z")
    expect(page.locator("#source")).to_have_value(code)
    page.keyboard.press("Control+Home")
    page.keyboard.press("Control+/")
    expect(page.locator("#source")).to_have_value("// " + code)
    page.keyboard.press("Control+z")
    expect(page.locator("#source")).to_have_value(code)
    invalid = 'extern "C" __global__ void solve() {\n    float x = ;\n}'
    fill(page, invalid)
    expect(page.locator("#editor-analysis")).to_contain_text(
        "diagnostics" if english else "检查提示"
    )
    page.get_by_role("button", name=labels["check"], exact=True).click()
    expect(page.locator(".cm-diagnostic").first).to_contain_text(labels["diagnostic"])
    # Editing diagnostics never disable draft saves or open GPU admission.
    page.get_by_role("button", name=labels["save"], exact=True).click()
    expect(page.locator("#editor-status")).to_have_text(labels["saved"])
    expect(page.locator('button[name="mode"][value="run"]')).to_be_disabled()
    expect(page.locator('button[name="mode"][value="submit"]')).to_be_disabled()

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
    page.reload()
    fill(page, code)
    page.get_by_role("button", name=labels["format"], exact=True).click()
    newer = code + "\n// newer edit"
    fill(page, newer)
    expect(page.locator("#editor-analysis")).to_contain_text(
        "code changed during formatting" if english else "格式化期间代码已修改", timeout=30000
    )
    expect(page.locator("#source")).to_have_value(newer)

    browser = page.context.browser
    failed = browser.new_context(storage_state=page.context.storage_state())
    failed.route("**/editor/clang-format*.wasm", lambda route: route.abort())
    try:
        fallback = failed.new_page()
        fallback.goto(url)
        fill(fallback, code)
        fallback.get_by_role("button", name=labels["format"], exact=True).click()
        expect(fallback.locator("#editor-analysis")).to_contain_text(
            "unavailable" if english else "暂不可用", timeout=30000
        )
        expect(fallback.locator("#source")).to_have_value(code)
        fallback.get_by_role("button", name=labels["save"], exact=True).click()
        expect(fallback.locator("#editor-status")).to_have_text(labels["saved"])
        fallback.get_by_role("button", name=labels["check"], exact=True).click()
        expect(fallback.locator("#editor-analysis")).to_contain_text(labels["valid"])
    finally:
        failed.close()
    no_js = browser.new_context(
        java_script_enabled=False, storage_state=page.context.storage_state()
    )
    try:
        fallback = no_js.new_page()
        fallback.goto(url)
        expect(fallback.locator("#source")).to_be_visible()
        fallback.locator("#source").fill(newer)
        fallback.get_by_role("button", name=labels["save"], exact=True).click()
        expect(fallback.locator("#source")).to_have_value(newer)
    finally:
        no_js.close()
    page.goto(url)
    expect(page.locator("#source")).to_have_value(newer)
    page.screenshot(path=str(out / (language + "-cuda-editor.png")), full_page=True)
    return {
        "language": language,
        "passed": True,
        "checks": [
            "highlight",
            "cuda-and-file-completion",
            "keyboard-escape",
            "format-and-undo",
            "comment-toggle",
            "structure-diagnostics",
            "private-draft-reload",
            "closed-admission",
            "stale-format",
            "wasm-failure-preserves-source",
            "check-without-wasm",
            "no-js-save",
        ],
    }
