"""Browser end-to-end for appointments: a customer books a time on the phone, checks in
with their booking link, and staff "Call next" picks the booked ticket before the
walk-ins who were already waiting (the slot time has come).

Run: pytest -m e2e   (GILIRAN_SCREENSHOTS=docs/screenshots saves README screenshots)
"""

import re
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone
from playwright.sync_api import expect

from queues import engine
from queues.models import Booking, Service, Slot

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

PASSWORD = "e2e-pass-123"


@pytest.fixture
def demo(transactional_db):
    # Slots and a few seeded bookings, but no walk-in tickets (numbering stays predictable).
    call_command("seed_demo", password=PASSWORD, tickets=0, verbosity=0)


def _shot(page, screenshot_dir, name, full_page=False):
    if screenshot_dir:
        page.screenshot(path=f"{screenshot_dir}/{name}.png", full_page=full_page)


def test_book_check_in_and_call(live_server, demo, browser, screenshot_dir):
    base = live_server.url
    consult = Service.objects.get(branch__slug="seri-mawar", prefix="A")
    now = timezone.now()
    local = timezone.localtime(now)
    since_midnight = timedelta(hours=local.hour, minutes=local.minute, seconds=local.second)
    # A slot that started a moment ago (same local day), so the booking is on time.
    start = now - min(timedelta(minutes=2), since_midnight)
    slot = Slot.objects.create(
        service=consult, starts_at=start, ends_at=start + timedelta(minutes=30), capacity=2
    )
    # Two walk-ins were already waiting before the booked customer arrives.
    walk_ins = [engine.issue_ticket(consult, nickname=f"Walk-in {i}") for i in (1, 2)]

    phone = browser.new_context(viewport={"width": 390, "height": 780}, locale="en-US")
    customer = phone.new_page()
    customer.goto(f"{base}/b/seri-mawar/book/")
    expect(customer.get_by_role("heading", name="Book an appointment")).to_be_visible()
    customer.get_by_text("Consultation").click()
    # Tomorrow first, for the README screenshot (a full day of seeded slots).
    customer.get_by_label("Day").select_option(index=1)
    customer.get_by_role("button", name="Show times").click()
    expect(customer.get_by_role("heading", name="Choose a time")).to_be_visible()
    expect(customer.locator(".slot-choice").first).to_be_visible()
    _shot(customer, screenshot_dir, "book-phone", full_page=True)

    customer.get_by_label("Day").select_option(index=0)
    customer.get_by_role("button", name="Show times").click()
    radio = customer.locator(f'input[name="slot"][value="{slot.pk}"]')
    customer.locator("label.slot-choice", has=radio).click()
    expect(radio).to_be_checked()
    customer.get_by_label("Nickname").fill("E2E Booker")
    customer.get_by_label("Phone number").fill("000-000 9999")
    customer.get_by_role("button", name="Book this time").click()

    expect(customer).to_have_url(re.compile(r"/a/[0-9a-f-]+/$"))
    booking = Booking.objects.get(nickname="E2E Booker")
    expect(customer.get_by_test_id("booking-ref")).to_have_text(booking.ref)
    expect(customer.get_by_text("Booked. Keep this page")).to_be_visible()
    _shot(customer, screenshot_dir, "booking-phone", full_page=True)

    customer.get_by_role("button", name="I have arrived: check in").click()
    expect(customer).to_have_url(re.compile(r"/t/[0-9a-f-]+/$"))
    code = customer.locator('[data-field="code"]').inner_text()
    assert code == "A-003"  # numbered after the two walk-ins...
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Waiting")
    expect(customer.get_by_text(re.compile(r"Booked \d\d:\d\d"))).to_be_visible()
    expect(customer.locator('[data-field="ahead"]')).to_have_text("0")  # ...but called first

    desk = browser.new_context(viewport={"width": 1280, "height": 800})
    staff = desk.new_page()
    staff.goto(f"{base}/accounts/login/")
    staff.get_by_label("Username").fill("staff1")
    staff.get_by_label("Password").fill(PASSWORD)
    staff.get_by_role("button", name="Log in").click()
    staff.get_by_role("link", name="Appointments").click()
    expect(staff.get_by_role("heading", name="Appointments")).to_be_visible()
    row = staff.locator("tr", has_text=booking.ref)
    expect(row).to_contain_text("Checked in")
    expect(row).to_contain_text(code)
    _shot(staff, screenshot_dir, "appointments-staff", full_page=True)

    staff.goto(f"{base}/staff/")
    staff.get_by_role("link", name="Room 1").click()
    expect(staff.locator(".queue-list li").first).to_contain_text(code)
    staff.get_by_role("button", name="Call next").click()
    expect(staff.get_by_test_id("current-code")).to_have_text(code)

    expect(customer.locator('[data-field="status_label"]')).to_have_text("Called", timeout=5000)
    expect(customer.locator('[data-field="counter"]')).to_have_text("Room 1")
    # The walk-ins are next, in their original order.
    staff.get_by_role("button", name="Done").click()
    staff.get_by_role("button", name="Call next").click()
    expect(staff.get_by_test_id("current-code")).to_have_text(walk_ins[0].code)


def test_staff_checks_in_at_the_counter_and_manager_adds_slots(live_server, demo, page):
    consult = Service.objects.get(branch__slug="seri-mawar", prefix="A")
    local = timezone.localtime()
    since_midnight = timedelta(hours=local.hour, minutes=local.minute, seconds=local.second)
    start = timezone.now() - min(timedelta(minutes=1), since_midnight)
    slot = Slot.objects.create(
        service=consult, starts_at=start, ends_at=start + timedelta(minutes=30), capacity=1
    )
    booking = engine.book_slot(slot, nickname="Desk Booker", phone="000-000 8888")
    base = live_server.url
    page.goto(f"{base}/accounts/login/")
    page.get_by_label("Username").fill("manager")
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()
    day = timezone.localdate(start).isoformat()
    page.goto(f"{base}/staff/b/seri-mawar/appointments/?q=8888&day={day}")
    page.get_by_role("button", name=f"Check in {booking.ref}").click()
    expect(page.get_by_role("status")).to_contain_text(f"{booking.ref} checked in: ticket A-001")

    page.get_by_role("link", name="Appointment slots").click()
    page.get_by_label("Service").select_option(label="A · Consultation")
    later = (timezone.localdate() + timedelta(days=5)).isoformat()
    page.get_by_label("Day").last.fill(later)
    page.get_by_label("From").fill("13:00")
    page.get_by_label("Until").fill("14:00")
    page.get_by_role("button", name="Create slots").click()
    expect(page.get_by_role("status")).to_contain_text("2 slots created")
    expect(page.locator("tbody tr")).to_have_count(2)
