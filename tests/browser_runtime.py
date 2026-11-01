"""Headless acceptance browsers must not attach to the caller's desktop session."""

import json
import os
import struct


def launch_chromium(playwright, *, args=None):
    environment = dict(os.environ)
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "DBUS_SESSION_BUS_ADDRESS"):
        environment.pop(name, None)
    return playwright.chromium.launch(headless=True, args=args or [], env=environment)


def preflight():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = launch_chromium(playwright)
        try:
            page = browser.new_page(viewport={"width": 640, "height": 480})
            page.set_content("<h1>PraxiMesh browser preflight</h1>")
            png = page.screenshot()
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert struct.unpack(">II", png[16:24]) == (640, 480)
            return {"chromium": browser.version, "screenshot_bytes": len(png)}
        finally:
            browser.close()


if __name__ == "__main__":
    print(json.dumps(preflight()))
