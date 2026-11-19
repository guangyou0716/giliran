"""Accessibility audit with axe-core (bundled in the axe-playwright-python dev dependency).

Key public pages (and the staff login / console) must have no serious or critical axe
violations, in English, Bahasa Melayu and Simplified Chinese where the text changes.
"""

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from queues.models import Booking, Service, Ticket
from tests.conftest import PASSWORD
from tests.e2e.axe_util import audit

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]


@pytest.fixture
def demo(transactional_db):
    call_command("seed_demo", password=PASSWORD, verbosity=0)


def public_paths():
    return {
        "home": "/",
        "join": "/b/seri-mawar/join/",
        "book (choose)": "/b/seri-mawar/book/",
        "board": "/b/seri-mawar/board/",
        "login": "/accounts/login/",
    }


@pytest.mark.parametrize("locale", ["en-US", "ms-MY", "zh-CN"])
def test_public_pages_have_no_serious_violations(live_server, demo, browser, locale):
    context = browser.new_context(locale=locale, viewport={"width": 390, "height": 780})
    page = context.new_page()
    for name, path in public_paths().items():
        page.goto(live_server.url + path)
        page.wait_for_load_state("networkidle")
        audit(page, f"{name} [{locale}]")


def test_booking_flow_pages_have_no_serious_violations(live_server, demo, browser):
    page = browser.new_context(viewport={"width": 390, "height": 780}).new_page()
    # Step 2 of booking: slot list + nickname/phone form (tomorrow always has free seeded slots).
    service = Service.objects.get(branch__slug="seri-mawar", prefix="A")
    day = (timezone.localdate() + timedelta(days=1)).isoformat()
    page.goto(f"{live_server.url}/b/seri-mawar/book/?service={service.pk}&day={day}")
    page.wait_for_load_state("networkidle")
    assert page.locator("input[name=slot]").count() > 0
    audit(page, "book (slots)")
    # Booking link page and ticket page (secret links).
    booking = Booking.objects.filter(status="booked").first() or Booking.objects.first()
    page.goto(f"{live_server.url}/a/{booking.token}/")
    audit(page, "booking link")
    ticket = Ticket.objects.filter(status="waiting").first()
    page.goto(f"{live_server.url}/t/{ticket.token}/")
    page.wait_for_load_state("networkidle")
    audit(page, "ticket (waiting)")


def test_rate_limited_page_has_no_serious_violations(live_server, demo, browser, settings):
    settings.GILIRAN_RATE_LIMIT_MAX = 1
    page = browser.new_context().new_page()
    for _ in range(2):
        page.goto(live_server.url + "/b/seri-mawar/join/")
        page.get_by_text("Consultation").click()
        page.get_by_role("button", name="Get my ticket").click()
    assert "Please slow down" in page.content()
    audit(page, "rate limited (429)")


def test_staff_pages_have_no_serious_violations(live_server, demo, browser):
    page = browser.new_context(viewport={"width": 1280, "height": 800}).new_page()
    page.goto(live_server.url + "/staff/")
    page.get_by_label("Username").fill("manager")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    page.wait_for_load_state("networkidle")
    audit(page, "staff dashboard")
    page.get_by_role("link", name="Room 1").click()
    page.wait_for_load_state("networkidle")
    audit(page, "counter console")
