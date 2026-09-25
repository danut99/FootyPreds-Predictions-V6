"""Browser smoke test of the web UI against a RUNNING server (default: the offline mock).

    .\\.venv\\Scripts\\python.exe footypreds/scripts/mock_server.py --port 8765   # terminal 1
    .\\.venv\\Scripts\\python.exe footypreds/scripts/ui_smoke.py [--base URL] [--only desktop]

Visits every page (#/, #/meciuri, #/meci/..., #/live, #/bilete, #/simulator, #/portofel,
#/rezultate, #/metoda) on desktop (1440x900) and phone (390x844) and clicks the main flows:
switch sports, open a football/basketball/tennis match, refresh live, generate a ticket and
ask for another variant, play it in the virtual wallet, prepare the recent days and run a
ladder simulation. It fails on console errors, page errors, CSP violations, failed same-origin
requests (HTTP >= 400 or network failure) and horizontal page scroll. Screenshots are saved in
footypreds/artifacts/. Against the real server (http://127.0.0.1:8000) the flows spend
FlashScore requests: use the mock server for routine checks.
"""

import argparse
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

OUTPUT = Path(__file__).resolve().parents[1] / "artifacts"
VIEWPORTS = {"desktop": {"width": 1440, "height": 900}, "mobile": {"width": 390, "height": 844}}
TIMEOUT = 60_000
# Records CSP violations; console errors are captured by Playwright.
CSP_PROBE = """
window.__csp = [];
document.addEventListener('securitypolicyviolation', e => {
  window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`);
});
"""


class Smoke:
    def __init__(self, browser, base, name, viewport):
        self.base, self.name, self.viewport = base, name, viewport
        self.problems = []
        self.origin = urlparse(base).netloc
        self.page = browser.new_page(viewport=viewport)
        self.page.set_default_timeout(TIMEOUT)
        self.page.add_init_script(CSP_PROBE)
        self.page.on("console", self.console)
        self.page.on("pageerror", lambda e: self.fail(f"page error: {e}"))
        self.page.on("requestfailed", self.request_failed)
        self.page.on("response", self.response)

    def fail(self, message):
        self.problems.append(f"{self.name}: {message}")

    def console(self, message):
        if message.type == "error":
            self.fail(f"console: {message.text} @ {message.location.get('url', '')}")

    def same_origin(self, url):
        return urlparse(url).netloc == self.origin

    def request_failed(self, request):
        # A navigation away aborts pending fetches (ERR_ABORTED); that is not a failure.
        failure = request.failure or ""
        if self.same_origin(request.url) and "ERR_ABORTED" not in failure:
            self.fail(f"request failed: {request.method} {request.url} ({failure})")

    def response(self, response):
        if self.same_origin(response.url) and response.status >= 400:
            self.fail(f"HTTP {response.status}: {response.request.method} {response.url}")

    # -- helpers -------------------------------------------------------------------------------

    def goto(self, route, wait):
        self.page.goto(f"{self.base}/#/{route}")
        self.page.wait_for_selector(wait)

    def check(self, label):
        """Settle, then check CSP, horizontal scroll and take a screenshot."""
        self.page.wait_for_timeout(700)
        width = self.page.evaluate("document.documentElement.scrollWidth")
        if width > self.viewport["width"]:
            self.fail(f"{label}: horizontal scroll ({width}px > {self.viewport['width']}px)")
        for violation in self.page.evaluate("window.__csp || []"):
            self.fail(f"{label}: CSP violation {violation}")
        self.page.evaluate("window.__csp = []")
        self.page.screenshot(path=OUTPUT / f"{label}-{self.name}.png", full_page=True)

    def confirm(self):
        self.page.click("#dialog [data-confirm]")

    # -- pages and flows -----------------------------------------------------------------------

    def home(self):
        self.goto("", "#tickets .ticket")
        self.page.wait_for_selector("#singles .single, #singles .state")
        self.page.wait_for_selector("#live-strip .live-mini, #live-strip .strip-note")
        if not self.page.locator("#tickets .ticket-head").count() == 4:
            self.fail("home: expected four ticket cards x2/x5/x10/x100")
        self.check("home")
        # Sport filter chip -> tennis tickets only, then back to all.
        self.page.click("[data-sport-filter='tennis']")
        self.page.wait_for_selector("#tickets .ticket")
        self.page.click("[data-sport-filter='all']")
        self.page.wait_for_selector("#tickets .ticket")

    def board(self):
        self.goto("meciuri", ".match-card")
        for tab in ("goals", "btts", "score", "htft", "1x2"):
            if self.page.locator(f"[data-tab='{tab}']").count():
                self.page.click(f"[data-tab='{tab}']")
        self.check("meciuri")
        for sport in ("basketball", "tennis", "football"):
            self.page.click(f"[data-switch='{sport}']")
            self.page.wait_for_selector(f"#board-{sport} .match-card")
            if sport != "football":
                self.check(f"meciuri-{sport}")
        self.page.click("[data-switch='all']")
        self.page.wait_for_selector("#board-tennis .match-card")

    def matches(self):
        for sport in ("football", "basketball", "tennis"):
            self.page.goto(f"{self.base}/#/meciuri")
            self.page.wait_for_selector(f"#board-{sport} .match-card")
            self.page.locator(f"#board-{sport} .match-card").first.click()
            self.page.wait_for_selector(".match-hero:not(.skeleton-hero)")
            self.page.wait_for_selector("#markets-table table")
            # Upcoming matches enrich themselves (POST /api/analyze): let it finish.
            self.page.wait_for_timeout(2500)
            self.page.wait_for_selector(".match-hero:not(.skeleton-hero)")
            self.check(f"meci-{sport}")

    def live(self):
        self.goto("live", ".live-card")
        self.page.click("#live-now")
        self.page.wait_for_selector(".live-card")
        self.page.locator("[data-live-detail]").first.click()
        self.page.wait_for_selector("#drawer .drawer-score")
        self.check("live-detaliu")
        self.page.click("#drawer .drawer-close")
        self.page.click("#live-pause")
        self.check("live")

    def tickets(self):
        self.goto("bilete", "#gen-form")
        self.page.click("[data-target='5']")
        self.page.click("#gen-go")
        self.page.wait_for_selector("#generated-ticket")
        self.page.click("#gen-other")
        self.page.wait_for_selector("#generated-ticket")
        self.check("bilete")

    def wallet(self):
        self.goto("portofel", "#deposit-form")
        self.page.fill("#deposit-amount", "250")
        self.page.click("#deposit-form button[type='submit']")
        self.page.wait_for_selector(".toast-success")
        # Play the day's x2 AI ticket from home.
        self.goto("", "#tickets .bet-form")
        self.page.locator("#tickets .bet-form button[type='submit']").first.click()
        self.page.wait_for_selector(".toast-success")
        self.goto("portofel", ".bet-card")
        self.check("portofel")

    def simulator(self, prepare):
        self.goto("simulator", "#sim-form")
        self.page.wait_for_selector("#sim-dataset option[value='football']", state="attached")
        if prepare:
            self.page.select_option("#sim-dataset", "recent")
            self.page.fill("#sim-days", "7")
            self.page.dispatch_event("#sim-days", "change")
            self.page.click("#sim-prepare")
            self.confirm()
            self.page.wait_for_function(
                "!document.querySelector('#sim-prepare').disabled"
                " && !!document.querySelector('#recent-status .recent-line')",
                timeout=120_000,
            )
            self.page.click("input[name='strategy'][value='ladder']", force=True)
            self.page.fill("#sim-bankroll", "5")
            self.page.click("#sim-run")
            self.page.wait_for_selector("#sim-output .ladder-hero, #sim-output .state-error")
            self.check("simulator-recent")
        self.page.select_option("#sim-dataset", "football")
        self.page.click("input[name='strategy'][value='ladder']", force=True)
        self.page.fill("#sim-bankroll", "5")
        self.page.fill("#sim-target", "2")
        self.page.click("#sim-run")
        self.page.wait_for_selector("#sim-output .ladder-hero", timeout=300_000)
        self.page.wait_for_selector("#timeline .day-card")
        self.check("simulator")

    def static_pages(self):
        self.goto("rezultate", "#ledger .kpi-grid, #ledger .state")
        self.page.wait_for_selector("#history-summary .summary-card, #history-summary .state")
        self.check("rezultate")
        self.goto("metoda", "#benchmark table, #benchmark p")
        self.check("metoda")

    def simulator_full(self):
        self.simulator(prepare=True)

    def simulator_quick(self):
        self.simulator(prepare=False)

    def run(self, full):
        steps = [self.home, self.board, self.matches, self.live, self.tickets, self.static_pages]
        steps += [self.wallet, self.simulator_full] if full else [self.simulator_quick]
        for step in steps:
            name = step.__name__
            started = time.monotonic()
            try:
                step()
            except Exception as exc:  # noqa: BLE001 - report every failing flow, keep going
                self.fail(f"{name}: {type(exc).__name__}: {str(exc).splitlines()[0]}")
                try:
                    shot = OUTPUT / f"error-{name}-{self.name}.png"
                    self.page.screenshot(path=shot, full_page=True)
                except Exception:  # noqa: BLE001
                    pass
            print(f"  {self.name:7} {name:14} {time.monotonic() - started:5.1f}s", flush=True)
        self.page.close()
        return self.problems


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--only", choices=sorted(VIEWPORTS), default=None)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    OUTPUT.mkdir(exist_ok=True)
    problems = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        for name, viewport in VIEWPORTS.items():
            if args.only and name != args.only:
                continue
            print(f"{name} {viewport['width']}x{viewport['height']}", flush=True)
            # The wallet and the recent-days flows change server state: run them once.
            problems += Smoke(browser, base, name, viewport).run(full=name == "desktop")
        browser.close()
    unique = list(dict.fromkeys(re.sub(r"\s+", " ", p) for p in problems))
    print("\n".join(unique) or f"OK - screenshots in {OUTPUT}")
    return 1 if unique else 0


if __name__ == "__main__":
    sys.exit(main())
