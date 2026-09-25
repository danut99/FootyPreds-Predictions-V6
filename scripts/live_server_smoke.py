"""Reproduce Live Server's separate static origin and nested /web/index.html path."""

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parent.parent
handler = functools.partial(SimpleHTTPRequestHandler, directory=str(root))
server = ThreadingHTTPServer(("127.0.0.1", 5501), handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto("http://127.0.0.1:5501/web/index.html", wait_until="networkidle")
        assert (
            page.locator(".sidebar").evaluate(
                "element => getComputedStyle(element).backgroundColor"
            )
            == "rgb(21, 21, 33)"
        )
        expect(page.locator("#connection")).to_have_text("Cheie API configurată")
        page.locator("[data-view='matches']").click()
        page.get_by_role("button", name="Explorează demo").click()
        expect(page.locator(".match-row")).to_have_count(6)
        page.locator("[data-view='backtest']").click()
        page.get_by_role("button", name="Simulare demo").click()
        expect(page.locator("#backtest-output .metric-strip")).to_be_visible(timeout=20000)
        page.locator("[data-view='matches']").click()
        page.screenshot(path=str(root / "artifacts/liveserver-desktop.png"), full_page=True)
        assert not errors, errors
        browser.close()
        print("Live Server OK: relative CSS/JS, GET and POST to Python via CORS, demo and backtest")
finally:
    server.shutdown()
    server.server_close()
    thread.join()
