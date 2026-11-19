"""Appointment pages: customer booking + token management, staff check-in, manager slots."""

from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from queues import engine
from queues.models import Booking, Service, Slot, Ticket

from .conftest import make_user, myt

pytestmark = pytest.mark.django_db
B = Booking.Status
NOW = myt(2026, 11, 19, 9, 45)
TEN = myt(2026, 11, 19, 10, 0)


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch, settings):
    """Pin the clock so day boundaries never make these tests flaky."""
    settings.GILIRAN_BOOKING_GRACE_MIN = 10
    settings.GILIRAN_CHECKIN_EARLY_MIN = 30
    settings.GILIRAN_BOOKING_DAYS = 7
    monkeypatch.setattr(timezone, "now", lambda: NOW)


def make_slot(service, start=TEN, capacity=2, **extra):
    return Slot.objects.create(
        service=service,
        starts_at=start,
        ends_at=start + timedelta(minutes=30),
        capacity=capacity,
        **extra,
    )


@pytest.fixture
def slot(consult):
    return make_slot(consult)


def _book_url(branch, service, day="2026-11-19"):
    return reverse("book", args=[branch.slug]) + f"?service={service.pk}&day={day}"


# ---- customer booking ---------------------------------------------------------------


def test_book_page_lists_free_times(client, branch, consult, slot):
    make_slot(consult, start=myt(2026, 11, 19, 9, 0))  # past grace: hidden
    full = make_slot(consult, start=myt(2026, 11, 19, 11, 0), capacity=1)
    engine.book_slot(full, nickname="X", phone="0000000009", now=NOW)
    body = client.get(_book_url(branch, consult)).content.decode()
    assert "10:00" in body and "2 left" in body
    assert "09:00" not in body and "11:00" not in body
    assert 'name="phone"' in body


def test_book_page_without_choice_shows_only_search(client, branch, consult, slot):
    body = client.get(reverse("book", args=[branch.slug])).content.decode()
    assert "Show times" in body and 'name="phone"' not in body
    assert "Thu 19 Nov · Today" in body


def test_book_creates_booking_and_redirects(client, branch, consult, slot):
    r = client.post(
        _book_url(branch, consult),
        {"slot": slot.pk, "nickname": "Aunty Demo", "phone": "000-000 1234"},
    )
    b = Booking.objects.get()
    assert r.status_code == 302 and r.url == reverse("booking", args=[b.token])
    assert (b.nickname, b.phone, b.status) == ("Aunty Demo", "0000001234", B.BOOKED)
    slot.refresh_from_db()
    assert slot.booked_count == 1


@pytest.mark.parametrize(
    "data,error",
    [
        ({"nickname": "A", "phone": "12"}, "9 to 15 digits"),
        ({"nickname": "", "phone": "0000001234"}, "This field is required"),
        ({"nickname": "A", "phone": "0000001234", "slot": 999999}, "no longer available"),
    ],
)
def test_book_validation(client, branch, consult, slot, data, error):
    data = {"slot": slot.pk, **data}
    r = client.post(_book_url(branch, consult), data)
    assert r.status_code == 200 and error in r.content.decode()
    assert Booking.objects.count() == 0


def test_book_full_slot_shows_message(client, branch, consult):
    s = make_slot(consult, capacity=1)
    engine.book_slot(s, nickname="X", phone="0000000009", now=NOW)
    r = client.post(
        _book_url(branch, consult), {"slot": s.pk, "nickname": "Y", "phone": "0000000010"}
    )
    # A full slot is no longer a valid choice.
    assert "no longer available" in r.content.decode()
    assert Booking.objects.count() == 1


def test_book_rejects_slot_of_other_service(client, branch, consult, pharmacy):
    other = make_slot(pharmacy)
    r = client.post(
        _book_url(branch, consult), {"slot": other.pk, "nickname": "Y", "phone": "0000000010"}
    )
    assert r.status_code == 200 and Booking.objects.count() == 0


def test_book_rejects_day_outside_window(client, branch, consult):
    r = client.get(_book_url(branch, consult, day="2026-12-31"))
    assert "Select a valid choice" in r.content.decode()


def test_booking_page_check_in_and_cancel_states(client, slot):
    b = engine.book_slot(slot, nickname="Guest", phone="0000000001", now=NOW)
    body = client.get(reverse("booking", args=[b.token])).content.decode()
    assert b.ref in body and "I have arrived: check in" in body and "Cancel my booking" in body
    assert "0000000001" not in body  # the phone is never shown back
    r = client.post(reverse("booking_check_in", args=[b.token]))
    t = Ticket.objects.get()
    assert r.status_code == 302 and r.url == reverse("ticket", args=[t.token])
    body = client.get(reverse("booking", args=[b.token])).content.decode()
    assert f"Open my queue number {t.code}" in body and "Cancel my booking" not in body
    assert "Booked 10:00" in client.get(reverse("ticket", args=[t.token])).content.decode()


def test_booking_page_before_check_in_opens(client, consult):
    s = make_slot(consult, start=myt(2026, 11, 19, 14, 0))
    b = engine.book_slot(s, nickname="Guest", phone="0000000001", now=NOW)
    body = client.get(reverse("booking", args=[b.token])).content.decode()
    assert "You can check in here from 13:30" in body
    assert "I have arrived" not in body
    r = client.post(reverse("booking_check_in", args=[b.token]), follow=True)
    assert "Check-in opens at 13:30." in r.content.decode()


def test_booking_cancel_via_token(client, slot):
    b = engine.book_slot(slot, nickname="Guest", phone="0000000001", now=NOW)
    assert client.get(reverse("booking_cancel", args=[b.token])).status_code == 405
    r = client.post(reverse("booking_cancel", args=[b.token]), follow=True)
    assert "Your booking was cancelled." in r.content.decode()
    b.refresh_from_db()
    assert b.status == B.CANCELLED and Slot.objects.get().booked_count == 0
    r = client.post(reverse("booking_cancel", args=[b.token]), follow=True)
    assert "can no longer be cancelled" in r.content.decode()


def test_missed_booking_page(client, consult):
    s = make_slot(consult, start=myt(2026, 11, 19, 9, 0))
    b = engine.book_slot(s, nickname="Guest", phone="0000000001", now=myt(2026, 11, 18, 9, 0))
    body = client.get(reverse("booking", args=[b.token])).content.decode()
    assert "Check-in time passed" in body and "Cancel my booking" not in body


def test_unknown_booking_token_404(client, db):
    assert client.get("/a/00000000-0000-0000-0000-000000000000/").status_code == 404


def test_home_and_join_link_to_booking(client, branch, consult):
    url = reverse("book", args=[branch.slug])
    assert url in client.get(reverse("home")).content.decode()
    assert url in client.get(reverse("join", args=[branch.slug])).content.decode()


# ---- staff ----------------------------------------------------------------------------


def test_staff_appointments_list_and_check_in(staff_client, branch, slot, consult):
    b = engine.book_slot(slot, nickname="Guest", phone="000-000 4321", now=NOW)
    url = reverse("staff:appointments", args=[branch.slug])
    body = staff_client.get(url).content.decode()
    assert b.ref in body and "•••• 4321" in body and "0000004321" not in body
    assert "1 / 2 booked" in body
    r = staff_client.post(reverse("staff:booking_check_in", args=[b.pk]))
    assert r.status_code == 302 and r.url == f"{url}?day=2026-11-19"
    t = Ticket.objects.get()
    assert t.source == "booking" and t.appointment_at == TEN
    assert t.events.get().actor.username == "staff"
    body = staff_client.get(r.url).content.decode()
    assert t.code in body and "Checked in" in body


def test_staff_appointments_search(staff_client, branch, consult, slot):
    a = engine.book_slot(slot, nickname="Alpha", phone="0000001111", now=NOW)
    b = engine.book_slot(slot, nickname="Bravo", phone="0000002222", now=NOW)
    url = reverse("staff:appointments", args=[branch.slug])
    for q, shown, hidden in [
        ("2222", b, a),
        ("alph", a, b),
        (b.ref.lower(), b, a),
    ]:
        body = staff_client.get(url, {"q": q, "day": "2026-11-19"}).content.decode()
        assert shown.ref in body and hidden.ref not in body
    # Fewer than 4 digits does not search phone numbers.
    body = staff_client.get(url, {"q": "22"}).content.decode()
    assert "No bookings match." in body


def test_staff_page_marks_missed_bookings_no_show(staff_client, branch, consult):
    s = make_slot(consult, start=myt(2026, 11, 19, 9, 0))
    b = engine.book_slot(s, nickname="Guest", phone="0000000001", now=myt(2026, 11, 18, 9, 0))
    body = staff_client.get(reverse("staff:appointments", args=[branch.slug])).content.decode()
    b.refresh_from_db()
    assert b.status == B.NO_SHOW and "No show" in body
    staff_client.post(reverse("staff:booking_check_in", args=[b.pk]))
    t = Ticket.objects.get()
    assert t.appointment_at is None  # late: queued like a walk-in


def test_staff_other_branch_forbidden(client, outsider, branch, slot):
    b = engine.book_slot(slot, nickname="Guest", phone="0000000001", now=NOW)
    client.force_login(outsider)
    assert client.get(reverse("staff:appointments", args=[branch.slug])).status_code == 403
    r = client.post(reverse("staff:booking_check_in", args=[b.pk]))
    assert r.status_code == 403
    b.refresh_from_db()
    assert b.status == B.BOOKED


def test_anonymous_appointments_redirect_to_login(client, branch):
    r = client.get(reverse("staff:appointments", args=[branch.slug]))
    assert r.status_code == 302 and "/accounts/login/" in r.url


def test_counter_console_shows_booked_pill(staff_client, slot, room1):
    b = engine.book_slot(slot, nickname="Guest", phone="0000000001", now=NOW)
    engine.check_in(b, now=NOW)
    body = staff_client.get(reverse("staff:counter", args=[room1.pk])).content.decode()
    assert "Booked 10:00" in body


# ---- manager slots --------------------------------------------------------------------


def test_manager_generates_slots(manager_client, branch, consult):
    url = reverse("manage:slot_generate", args=[branch.slug])
    data = {
        "service": consult.pk,
        "day": "2026-11-20",
        "start": "09:00",
        "end": "10:30",
        "every": 30,
        "capacity": 3,
    }
    r = manager_client.post(url, data, follow=True)
    assert "3 slots created, 0 already existed." in r.content.decode()
    starts = [timezone.localtime(s.starts_at).strftime("%H:%M") for s in Slot.objects.all()]
    assert starts == ["09:00", "09:30", "10:00"]
    assert set(Slot.objects.values_list("capacity", flat=True)) == {3}
    r = manager_client.post(url, {**data, "end": "11:00"}, follow=True)
    assert "1 slots created, 3 already existed." in r.content.decode()


@pytest.mark.parametrize(
    "change,error",
    [
        ({"end": "08:00"}, "end time must be after"),
        ({"end": "09:10"}, "shorter than one slot"),
        ({"day": "2026-11-18"}, "today or a later day"),
        ({"capacity": 0}, "greater than or equal to 1"),
        ({"every": 5, "start": "00:00", "end": "23:55"}, "too many slots"),
    ],
)
def test_slot_generate_validation(manager_client, branch, consult, change, error):
    data = {
        "service": consult.pk,
        "day": "2026-11-20",
        "start": "09:00",
        "end": "10:00",
        "every": 30,
        "capacity": 2,
        **change,
    }
    r = manager_client.post(reverse("manage:slot_generate", args=[branch.slug]), data)
    assert r.status_code == 200 and error in r.content.decode()
    assert Slot.objects.count() == 0


def test_manager_edits_capacity_not_below_booked(manager_client, branch, slot):
    engine.book_slot(slot, nickname="A", phone="0000000001", now=NOW)
    engine.book_slot(slot, nickname="B", phone="0000000002", now=NOW)
    url = reverse("manage:slot_edit", args=[branch.slug, slot.pk])
    r = manager_client.post(url, {"capacity": 1, "is_active": "on"})
    assert "2 seats are already booked" in r.content.decode()
    r = manager_client.post(url, {"capacity": 5})
    assert r.status_code == 302
    slot.refresh_from_db()
    assert (slot.capacity, slot.is_active, slot.booked_count) == (5, False, 2)


def test_manager_deletes_only_empty_slots(manager_client, branch, consult, slot):
    empty = make_slot(consult, start=myt(2026, 11, 19, 15, 0))
    engine.book_slot(slot, nickname="A", phone="0000000001", now=NOW)
    r = manager_client.post(reverse("manage:slot_delete", args=[branch.slug, slot.pk]), follow=True)
    assert "has bookings" in r.content.decode() and Slot.objects.filter(pk=slot.pk).exists()
    manager_client.post(reverse("manage:slot_delete", args=[branch.slug, empty.pk]))
    assert not Slot.objects.filter(pk=empty.pk).exists()


def test_manager_slots_page_lists_day(manager_client, branch, slot):
    body = manager_client.get(
        reverse("manage:slots", args=[branch.slug]), {"day": "2026-11-19"}
    ).content.decode()
    assert "10:00–10:30" in body and "0 / 2" in body


def test_staff_cannot_manage_slots(staff_client, branch, slot):
    for url in [
        reverse("manage:slots", args=[branch.slug]),
        reverse("manage:slot_edit", args=[branch.slug, slot.pk]),
    ]:
        assert staff_client.get(url).status_code == 403
    r = staff_client.post(reverse("manage:slot_delete", args=[branch.slug, slot.pk]))
    assert r.status_code == 403 and Slot.objects.filter(pk=slot.pk).exists()


def test_manager_cannot_touch_other_branch_slots(manager_client, branch, other_branch):
    other_service = Service.objects.create(branch=other_branch, name="Desk", prefix="D")
    other = make_slot(other_service)
    assert (
        manager_client.get(reverse("manage:slot_edit", args=[branch.slug, other.pk])).status_code
        == 404
    )
    assert manager_client.get(reverse("manage:slots", args=[other_branch.slug])).status_code == 403
    data = {
        "service": other_service.pk,
        "day": "2026-11-20",
        "start": "09:00",
        "end": "10:00",
        "every": 30,
        "capacity": 2,
    }
    r = manager_client.post(reverse("manage:slot_generate", args=[branch.slug]), data)
    assert r.status_code == 200 and Slot.objects.count() == 1  # invalid service choice


def test_dashboard_and_settings_link_to_appointments(manager_client, branch):
    body = manager_client.get(reverse("staff:dashboard")).content.decode()
    assert reverse("staff:appointments", args=[branch.slug]) in body
    body = manager_client.get(reverse("manage:settings", args=[branch.slug])).content.decode()
    assert reverse("manage:slots", args=[branch.slug]) in body


def test_superuser_can_check_in_anywhere(client, slot):
    admin = make_user("root", is_superuser=True, is_staff=True)
    client.force_login(admin)
    b = engine.book_slot(slot, nickname="Guest", phone="0000000001", now=NOW)
    client.post(reverse("staff:booking_check_in", args=[b.pk]))
    b.refresh_from_db()
    assert b.status == B.CHECKED_IN


@pytest.mark.parametrize(
    "lang,heading,day_label",
    [
        ("en", "Book an appointment", "Fri 20 Nov"),
        ("ms", "Tempah temu janji", "Jum, 20 Nov"),
        ("zh-hans", "预约时间", "11月20日 星期五"),
    ],
)
def test_book_page_languages(client, branch, consult, lang, heading, day_label):
    r = client.get(reverse("book", args=[branch.slug]), HTTP_ACCEPT_LANGUAGE=lang)
    body = r.content.decode()
    assert heading in body and day_label in body


def test_no_show_booking_page_says_ask_at_counter(client, consult):
    s = make_slot(consult, start=myt(2026, 11, 19, 9, 0))
    b = engine.book_slot(s, nickname="Guest", phone="0000000001", now=myt(2026, 11, 18, 9, 0))
    engine.expire_bookings()
    body = client.get(reverse("booking", args=[b.token])).content.decode()
    assert "Please ask at the counter" in body and "I have arrived" not in body


def test_admin_slot_form_refuses_capacity_below_booked(admin_client, slot):
    engine.book_slot(slot, nickname="A", phone="0000000001", now=NOW)
    engine.book_slot(slot, nickname="B", phone="0000000002", now=NOW)
    url = reverse("admin:queues_slot_change", args=[slot.pk])
    data = {
        "service": slot.service.pk,
        "starts_at_0": "2026-11-19",
        "starts_at_1": "10:00:00",
        "ends_at_0": "2026-11-19",
        "ends_at_1": "10:30:00",
        "capacity": 1,
        "is_active": "on",
    }
    r = admin_client.post(url, data)
    assert r.status_code == 200 and "2 seats are already booked" in r.content.decode()
    slot.refresh_from_db()
    assert slot.capacity == 2
