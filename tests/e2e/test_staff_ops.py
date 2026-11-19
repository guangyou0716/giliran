"""Browser end-to-end: counter pause / resume (shown on the board) and ticket transfer."""

import re

import pytest
from django.core.management import call_command
from playwright.sync_api import expect

from queues import engine
from queues.models import DailySequence, Service, Ticket
from tests.conftest import PASSWORD
from tests.e2e.axe_util import audit

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def demo(transactional_db):
    call_command("seed_demo", password=PASSWORD, verbosity=0)
    Ticket.objects.all().delete()  # a clean queue (and numbering) for deterministic steps
    DailySequence.objects.all().delete()


def login(browser, base, user):
    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.goto(f"{base}/staff/")
    page.get_by_label("Username").fill(user)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    expect(page.get_by_role("heading", name="Staff dashboard")).to_be_visible()
    return page


def test_pause_resume_shows_on_board_and_blocks_calling(live_server, demo, browser):
    base = live_server.url
    service = Service.objects.get(branch__slug="cempaka", prefix="S")
    engine.issue_ticket(service)
    tv = browser.new_context(viewport={"width": 1600, "height": 900}).new_page()
    tv.goto(f"{base}/b/cempaka/board/")
    paused = tv.locator('[data-list="paused"]')
    expect(paused).to_be_hidden()

    staff = login(browser, base, "staff3")
    staff.get_by_role("link", name="Counter 2").click()
    staff.get_by_role("button", name="Pause counter").click()
    expect(staff.get_by_role("heading", level=1)).to_contain_text("Paused")
    expect(staff.get_by_role("button", name="Call next")).to_have_count(0)
    expect(paused).to_contain_text("Counter 2", timeout=12000)
    audit(tv, "board with a paused counter")
    audit(staff, "paused counter console")

    staff.get_by_role("button", name="Resume counter").click()
    expect(staff.get_by_role("button", name="Call next")).to_be_enabled()
    expect(paused).to_be_hidden(timeout=12000)
    staff.get_by_role("button", name="Call next").click()
    expect(staff.locator('[data-testid="current-code"]')).to_have_text("S-001")


def test_transfer_called_ticket_to_another_service(live_server, demo, browser):
    base = live_server.url
    service = Service.objects.get(branch__slug="cempaka", prefix="S")
    ticket = engine.issue_ticket(service)

    customer = browser.new_context(viewport={"width": 390, "height": 780}).new_page()
    customer.goto(f"{base}/t/{ticket.token}/")
    expect(customer.locator('[data-field="code"]')).to_have_text("S-001")

    staff = login(browser, base, "staff3")
    staff.get_by_role("link", name="Counter 2").click()
    staff.get_by_role("button", name="Call next").click()
    expect(staff.locator('[data-testid="current-code"]')).to_have_text("S-001")
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Called", timeout=8000)

    staff.get_by_text("Transfer to another service").first.click()
    staff.get_by_label("Move S-001 to").select_option(label="C · Collection")
    staff.get_by_role("button", name="Transfer").click()
    expect(staff.locator(".messages")).to_contain_text(
        re.compile(r"S-001 moved to Collection as C-001")
    )
    expect(staff.get_by_text("No ticket at this counter.")).to_be_visible()
    # The customer's page follows the ticket to its new number without a reload.
    expect(customer.locator('[data-field="code"]')).to_have_text("C-001", timeout=8000)
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Waiting", timeout=8000)
    expect(customer).to_have_title(re.compile(r"^C-001"), timeout=8000)
    audit(staff, "counter console after transfer")
    # The new service's counter can call it, and the customer's page follows again.
    collect = login(browser, base, "staff3")
    collect.get_by_role("link", name="Counter 1").click()
    collect.get_by_role("button", name="Call next").click()
    expect(collect.locator('[data-testid="current-code"]')).to_have_text("C-001")
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Called", timeout=8000)
    expect(customer.locator('[data-field="counter"]')).to_have_text("Counter 1")
