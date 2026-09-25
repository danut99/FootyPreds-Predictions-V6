"""Exercise the new plan builder and strict benchmark using synthetic ticket data."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100}, reduced_motion="reduce")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8000", wait_until="networkidle")
    expect(page.locator("#view-studio")).to_be_visible()
    page.locator("#plan-source").select_option("demo")
    page.locator("#plan-date").fill(
        (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    )
    page.locator("#generate-plan").click()
    expect(page.locator("#generation-fraction")).to_have_text("7/7", timeout=30000)
    expect(page.locator(".ticket-card")).to_have_count(7)
    expect(page.locator(".ticket-details")).to_have_count(7)
    expect(page.locator("#plan-board .source-tag")).to_have_text("DEMO · COTE SINTETICE")
    page.screenshot(
        path=str(root / "artifacts/studio-desktop.png"), full_page=True, animations="disabled"
    )
    page.locator(".ticket-details").first.click()
    expect(page.locator(".slip-detail-leg")).not_to_have_count(0)
    page.get_by_role("button", name="Închide analiza").click()
    page.reload(wait_until="networkidle")
    expect(page.locator(".ticket-card")).to_have_count(7)
    page.locator("[data-mode='custom']").click()
    page.locator("[data-odds='10']").click()
    page.locator("#plan-source").select_option("demo")
    page.locator("#generate-plan").click()
    expect(page.locator("#generation-fraction")).to_have_text("1/1", timeout=30000)
    expect(page.locator(".ticket-card")).to_have_count(1)
    expect(page.locator(".ticket-details")).to_have_count(1)
    odds = float(page.locator(".slip-odds").inner_text().replace("×", "").strip())
    assert 9 <= odds <= 11
    page.locator("[data-view='plans']").click()
    expect(page.locator(".saved-plan").first).to_be_visible()
    page.locator("[data-view='backtest']").click()
    page.locator("#strict-report").click()
    expect(page.locator("#strict-output .metric-strip")).to_be_visible()
    expect(page.locator("#strict-output")).to_contain_text("7.156")
    page.screenshot(
        path=str(root / "artifacts/strict-benchmark.png"), full_page=True, animations="disabled"
    )
    page.locator("[data-view='studio']").click()
    page.set_viewport_size({"width": 390, "height": 844})
    page.screenshot(
        path=str(root / "artifacts/studio-mobile.png"), full_page=True, animations="disabled"
    )
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not errors, errors
    print(
        "Studio OK: week plan, persistence, custom odds 10, detail, saved plans, benchmark, mobile"
    )
    browser.close()
