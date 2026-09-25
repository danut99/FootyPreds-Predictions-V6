"""Exercise competition filters without sending requests to RapidAPI."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parent.parent
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1100})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:8000", wait_until="networkidle")
    expect(
        page.get_by_role("checkbox", name="Champions League Europe", exact=False)
    ).to_be_visible()
    page.locator("#competition-search").fill("Nations")
    expect(page.locator("#competition-list")).to_contain_text("UEFA Nations League")
    page.locator("#competition-search").fill("")
    page.locator("#plan-source").select_option("demo")
    expect(page.locator(".competition-option")).to_have_count(6)
    page.locator("#plan-date").fill(str(datetime.now(timezone.utc).date() + timedelta(days=1)))
    page.locator('[data-mode="custom"]').click()
    page.locator("#target-odds").fill("2")
    page.locator(".competition-option input").nth(0).check()
    expect(page.locator("#diverse-leagues")).to_be_disabled()
    expect(page.locator("#diverse-leagues")).not_to_be_checked()
    page.locator(".competition-option input").nth(1).check()
    expect(page.locator("#diverse-leagues")).to_be_enabled()
    expect(page.locator("#competition-summary")).to_contain_text("Demo League 1 + Demo League 2")
    with page.expect_response(
        lambda r: r.url.endswith("/api/plans") and r.request.method == "POST"
    ) as response:
        page.locator("#generate-plan").click()
    saved = response.value.json()
    assert saved["request"]["competitions"] == ["|demo league 1", "|demo league 2"]
    expect(page.locator("#generation-fraction")).to_have_text("1/1", timeout=30000)
    plan = page.request.get(f"http://127.0.0.1:8000/api/plans/{saved['id']}").json()
    assert plan["status"] != "failed", plan
    for day in plan["days"]:
        if day["ticket"]:
            assert all(
                leg["competition_id"] in saved["request"]["competitions"]
                for leg in day["ticket"]["legs"]
            )
    page.locator("#clear-competitions").click()
    expect(page.locator("#competition-summary")).to_contain_text("Toate competițiile")
    page.locator("#plan-source").select_option("live")
    expect(page.locator("#competition-list")).to_contain_text("Champions League")
    page.locator(".competition-picker").scroll_into_view_if_needed()
    page.screenshot(path=str(root / "artifacts/competitions-desktop.png"))
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator(".competition-picker").scroll_into_view_if_needed()
    page.screenshot(path=str(root / "artifacts/competitions-mobile.png"))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    assert not errors, errors
    browser.close()
    print("Competitions OK: national teams, multi-select, single-league rules, persistence, mobile")
