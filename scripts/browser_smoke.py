"""Local UI checks. Requires playwright + chromium and a server on port 8000."""

from pathlib import Path

from playwright.sync_api import expect, sync_playwright

artifacts = Path(__file__).resolve().parent.parent / "artifacts"
artifacts.mkdir(exist_ok=True)

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8000", wait_until="networkidle")
    page.locator("[data-view='matches']").click()
    expect(page.locator("h1")).to_have_text("O zi nouă. O perspectivă mai bună.")
    expect(page.locator("#connection")).not_to_have_text("Se conectează…")
    page.get_by_role("button", name="Explorează demo").click()
    expect(page.locator(".match-row")).to_have_count(6)
    expect(page.locator("#source-badge")).to_have_text("DEMO · SINTETIC")
    page.screenshot(path=str(artifacts / "dashboard-desktop.png"), full_page=True)
    page.get_by_role("button", name="Detalii").first.click()
    expect(page.locator("#analysis-dialog")).to_be_visible()
    expect(page.locator(".market")).to_have_count(14)
    page.screenshot(path=str(artifacts / "analysis-desktop.png"), full_page=True)
    page.get_by_role("button", name="Închide analiza").click()
    page.locator("#search").fill("Northbridge")
    expect(page.locator(".match-row")).to_have_count(1)
    page.locator("#search").fill("no-match-xyz")
    expect(page.locator(".match-row")).to_have_count(0)
    page.locator("#search").fill("")
    page.locator("#threshold").select_option("0.9")
    expect(page.locator("#pick-count")).to_have_text("2")
    page.locator("[data-view='backtest']").click()
    page.get_by_role("button", name="Simulare demo").click()
    expect(page.locator("#backtest-output .metric-strip")).to_be_visible(timeout=20000)
    expect(page.locator("#backtest-output .notice")).to_contain_text("SINTETICE")
    page.locator("[data-view='results']").click()
    expect(page.locator("#results-summary .metric-strip")).to_be_visible()
    page.locator("[data-view='method']").click()
    expect(page.locator(".method-grid article")).to_have_count(4)
    page.locator("[data-view='matches']").click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(path=str(artifacts / "dashboard-mobile.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (
        "Mobile overflow"
    )
    page.get_by_role("button", name="Detalii").first.click()
    expect(page.locator("#analysis-dialog")).to_be_visible()
    page.screenshot(path=str(artifacts / "analysis-mobile.png"), full_page=True)
    page.get_by_role("button", name="Închide analiza").click()
    # A provider error must be visible, and loading state must recover.
    page.route(
        "**/api/matches?*",
        lambda route: route.fulfill(
            status=429, content_type="application/json", body='{"detail":"Limita API test"}'
        ),
    )
    page.get_by_role("button", name="Încarcă meciuri").click()
    expect(page.locator("#notice")).to_have_text("Limita API test")
    expect(page.locator("#load-button")).to_be_enabled()
    assert not errors, errors
    print(
        "Browser OK: demo, filters, details, threshold, backtest, ledger, "
        "method, mobile, API errors"
    )
    print("Screenshots saved in artifacts/")
    browser.close()
