"""Browser end-to-end: a customer joins from the phone page, staff calls the
ticket from the counter console, and the customer's page and the TV board update
live without a reload.

Run: pytest -m e2e   (needs `playwright install chromium`)
Set GILIRAN_SCREENSHOTS=docs/screenshots to save README screenshots.
"""

import re

import pytest
from django.core.management import call_command
from playwright.sync_api import expect

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

PASSWORD = "e2e-pass-123"


@pytest.fixture
def demo(transactional_db):
    call_command("seed_demo", password=PASSWORD, verbosity=0)


def _shot(page, screenshot_dir, name, full_page=False):
    if screenshot_dir:
        page.screenshot(path=f"{screenshot_dir}/{name}.png", full_page=full_page)


def test_join_call_and_live_update(live_server, demo, browser, screenshot_dir):
    base = live_server.url
    phone = browser.new_context(viewport={"width": 390, "height": 780}, locale="en-US")
    customer = phone.new_page()
    customer.goto(f"{base}/b/seri-mawar/join/")
    expect(customer.get_by_role("heading", name="Take a number")).to_be_visible()
    _shot(customer, screenshot_dir, "join-phone", full_page=True)
    customer.get_by_text("Consultation").click()
    customer.get_by_label("Nickname (optional)").fill("E2E Tester")
    customer.get_by_role("button", name="Get my ticket").click()
    expect(customer).to_have_url(re.compile(r"/t/[0-9a-f-]+/$"))
    code = customer.locator('[data-field="code"]').inner_text()
    assert re.fullmatch(r"A-\d{3}", code)
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Waiting")
    _shot(customer, screenshot_dir, "ticket-waiting-phone", full_page=True)

    desk = browser.new_context(viewport={"width": 1280, "height": 800})
    staff = desk.new_page()
    staff.goto(f"{base}/staff/")
    expect(staff).to_have_url(re.compile(r"/accounts/login/"))
    staff.get_by_label("Username").fill("manager")
    staff.get_by_label("Password").fill(PASSWORD)
    staff.get_by_role("button", name="Log in").click()
    expect(staff.get_by_role("heading", name="Staff dashboard")).to_be_visible()
    staff.get_by_role("link", name="Room 1").click()

    # Room 1 already holds a seeded ticket; finish it, then call until ours comes up.
    for _ in range(10):
        current = staff.locator('[data-testid="current-code"]')
        if current.count() and current.inner_text() == code:
            break
        if current.count():
            staff.get_by_role("button", name="Done").click()
        staff.get_by_role("button", name="Call next").click()
    expect(staff.locator('[data-testid="current-code"]')).to_have_text(code)
    _shot(staff, screenshot_dir, "counter-console")

    # The customer page updates by polling, no reload.
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Called", timeout=5000)
    expect(customer.locator('[data-field="counter"]')).to_have_text("Room 1")
    expect(customer.get_by_role("button", name="Cancel my ticket")).to_be_hidden()
    expect(customer.locator("dl.stats")).to_be_hidden()
    _shot(customer, screenshot_dir, "ticket-called-phone", full_page=True)

    tv = browser.new_context(viewport={"width": 1600, "height": 900}).new_page()
    tv.goto(f"{base}/b/seri-mawar/board/")
    expect(tv.locator('[data-list="serving"]')).to_contain_text(code)
    _shot(tv, screenshot_dir, "display-board")
    staff.get_by_role("button", name="Start serving").click()
    staff.get_by_role("button", name="Done").click()
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Done", timeout=5000)
    expect(tv.locator('[data-list="serving"]')).not_to_contain_text(code, timeout=5000)

    staff.goto(f"{base}/staff/b/seri-mawar/tickets/?q=E2E")
    expect(staff.locator("tbody tr")).to_have_count(1)
    expect(staff.locator("tbody")).to_contain_text(code)
    _shot(staff, screenshot_dir, "ticket-history")


def test_customer_cancel_and_language_switch(live_server, demo, page):
    page.goto(f"{live_server.url}/b/cempaka/join/")
    page.get_by_label("Language").select_option("ms")
    expect(page.get_by_role("heading", name="Ambil nombor")).to_be_visible()
    page.get_by_text("Service desk").click()
    page.get_by_role("button", name="Dapatkan tiket saya").click()
    expect(page.locator('[data-field="status_label"]')).to_have_text("Menunggu")
    page.get_by_role("button", name="Batalkan tiket saya").click()
    expect(page.locator('[data-field="status_label"]')).to_have_text("Dibatalkan")


def test_staff_cannot_open_manager_pages(live_server, demo, page):
    page.goto(f"{live_server.url}/accounts/login/")
    page.get_by_label("Username").fill("staff1")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page.get_by_role("link", name="Services & counters")).to_have_count(0)
    response = page.goto(f"{live_server.url}/manage/b/seri-mawar/")
    assert response.status == 403
