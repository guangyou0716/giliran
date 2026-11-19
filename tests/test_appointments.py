"""Appointments engine: booking, capacity, cancel, check-in, fair ordering, no-shows."""

from datetime import datetime, timedelta

import pytest
from django.core.management import call_command
from django.db import IntegrityError, transaction

from queues import engine
from queues.models import Booking, Slot, Ticket

from .conftest import myt

pytestmark = pytest.mark.django_db
B = Booking.Status

# Slot at 10:00 MYT; grace 10 min (on time until 10:10), self check-in opens 09:30.
START = myt(2026, 11, 19, 10, 0)


@pytest.fixture(autouse=True)
def booking_settings(settings):
    settings.GILIRAN_BOOKING_GRACE_MIN = 10
    settings.GILIRAN_CHECKIN_EARLY_MIN = 30


def make_slot(service, start=START, capacity=2, minutes=30, **extra):
    return Slot.objects.create(
        service=service,
        starts_at=start,
        ends_at=start + timedelta(minutes=minutes),
        capacity=capacity,
        **extra,
    )


@pytest.fixture
def slot(consult):
    return make_slot(consult)


def book(slot, n=1, at=START - timedelta(hours=2)):
    return engine.book_slot(slot, nickname=f"Guest {n}", phone=f"000-000 {n:04d}", now=at)


# ---- booking and capacity ---------------------------------------------------


def test_book_takes_a_seat_and_normalizes_phone(slot):
    b = book(slot)
    slot.refresh_from_db()
    assert slot.booked_count == 1 and slot.seats_left == 1
    assert b.status == B.BOOKED and b.phone == "0000000001"
    assert len(b.ref) == 6 and b.ref.isalnum() and b.ref.upper() == b.ref
    assert b.phone_masked == "•••• 0001"


def test_full_slot_refuses_booking(slot):
    book(slot, 1)
    book(slot, 2)
    with pytest.raises(engine.SlotFull):
        book(slot, 3)
    slot.refresh_from_db()
    assert slot.booked_count == 2 and Booking.objects.count() == 2


def test_same_phone_cannot_book_same_slot_twice(slot):
    book(slot, 1)
    with pytest.raises(engine.BookingError, match="already has a booking"):
        engine.book_slot(slot, nickname="Again", phone="0000000001", now=START - timedelta(hours=1))
    assert Slot.objects.get().booked_count == 1


def test_inactive_slot_or_service_refuses_booking(slot, consult):
    slot.is_active = False
    slot.save()
    with pytest.raises(engine.BookingError, match="not open"):
        book(slot)
    slot.is_active = True
    slot.save()
    consult.is_active = False
    consult.save()
    with pytest.raises(engine.BookingError, match="not open"):
        book(slot)


def test_booking_closes_after_grace(slot):
    book(slot, 1, at=START + timedelta(minutes=10))  # last on-time minute: still allowed
    with pytest.raises(engine.BookingError, match="passed"):
        book(slot, 2, at=START + timedelta(minutes=11))


def test_database_rejects_overbooking_even_without_the_engine(slot):
    # The check constraint is the last line of defence against any code path.
    with pytest.raises(IntegrityError), transaction.atomic():
        Slot.objects.filter(pk=slot.pk).update(booked_count=3)
    with pytest.raises(IntegrityError), transaction.atomic():
        make_slot(slot.service, start=START + timedelta(hours=1), capacity=0)


def test_database_rejects_second_active_booking_per_phone(slot):
    b = book(slot, 1)
    with pytest.raises(IntegrityError), transaction.atomic():
        Booking.objects.create(slot=slot, ref="ZZZZZZ", nickname="x", phone=b.phone)


def test_bookable_slots_hides_full_inactive_and_past(consult):
    full = make_slot(consult, capacity=1)
    book(full)
    closed = make_slot(consult, start=START + timedelta(hours=1), is_active=False)
    open_ = make_slot(consult, start=START + timedelta(hours=2))
    past = make_slot(consult, start=START - timedelta(hours=1))
    other_day = make_slot(consult, start=START + timedelta(days=1))
    now = START - timedelta(minutes=5)
    listed = list(engine.bookable_slots(consult, START.date(), now=now))
    assert listed == [open_]
    assert full not in listed and closed not in listed and past not in listed
    assert other_day not in listed


# ---- cancel ---------------------------------------------------------------


def test_cancel_frees_the_seat(slot):
    b = book(slot, 1)
    book(slot, 2)
    b = engine.cancel_booking(b, now=START - timedelta(minutes=30))
    assert b.status == B.CANCELLED and b.cancelled_at is not None
    assert b.slot.booked_count == 1
    book(slot, 3)  # the freed seat can be taken again
    with pytest.raises(engine.BookingError):
        engine.cancel_booking(b, now=START - timedelta(minutes=20))


def test_cannot_cancel_after_grace(slot):
    b = book(slot)
    with pytest.raises(engine.BookingError, match="no longer be cancelled"):
        engine.cancel_booking(b, now=START + timedelta(minutes=11))


def test_cancelled_phone_can_book_again(slot):
    b = book(slot, 1)
    engine.cancel_booking(b, now=START - timedelta(hours=1))
    again = book(slot, 1)
    assert again.status == B.BOOKED


# ---- check-in -------------------------------------------------------------


def test_customer_check_in_window(slot):
    b = book(slot)
    with pytest.raises(engine.BookingError, match="opens at 09:30"):
        engine.check_in(b, now=START - timedelta(minutes=31))
    t = engine.check_in(b, now=START - timedelta(minutes=30))
    b.refresh_from_db()
    assert b.status == B.CHECKED_IN and b.ticket == t
    assert (t.code, t.source, t.status, t.nickname) == ("A-001", "booking", "waiting", "Guest 1")
    assert t.appointment_at == START
    with pytest.raises(engine.BookingError, match="cannot be checked in"):
        engine.check_in(b, now=START)


def test_customer_cannot_check_in_late_but_staff_can(slot, staff_user):
    b = book(slot)
    late = START + timedelta(minutes=11)
    with pytest.raises(engine.BookingError, match="passed"):
        engine.check_in(b, now=late)
    t = engine.check_in(b, by_staff=True, actor=staff_user, now=late)
    assert t.appointment_at is None  # queued like a walk-in from now
    assert t.events.get().actor == staff_user


def test_check_in_only_on_the_day(slot):
    b = book(slot)
    with pytest.raises(engine.BookingError, match="day of the appointment"):
        engine.check_in(b, by_staff=True, now=START - timedelta(days=1))


def test_check_in_works_when_branch_closed_for_walk_ins(slot, branch):
    branch.is_open = False
    branch.save()
    b = book(slot)
    assert engine.check_in(b, now=START).status == "waiting"


# ---- no-shows ---------------------------------------------------------------


def test_expire_marks_missed_bookings_no_show(slot, consult):
    missed = book(slot, 1)
    arrived = book(slot, 2)
    engine.check_in(arrived, now=START)
    later = make_slot(consult, start=START + timedelta(hours=1))
    upcoming = book(later, 3)
    assert engine.expire_bookings(now=START + timedelta(minutes=10)) == 0  # grace not over
    assert engine.expire_bookings(now=START + timedelta(minutes=11)) == 1
    statuses = dict(Booking.objects.values_list("pk", "status"))
    assert statuses == {missed.pk: B.NO_SHOW, arrived.pk: B.CHECKED_IN, upcoming.pk: B.BOOKED}
    assert Slot.objects.get(pk=slot.pk).booked_count == 2  # no-shows keep their seat


def test_expire_is_branch_scoped(slot, other_branch):
    from queues.models import Service

    other = Service.objects.create(branch=other_branch, name="Desk", prefix="D")
    book(slot, 1)
    book(make_slot(other), 2)
    assert engine.expire_bookings(now=START + timedelta(hours=1), branch=slot.service.branch) == 1
    assert Booking.objects.filter(status=B.BOOKED).count() == 1


def test_staff_can_check_in_a_no_show_same_day(slot):
    b = book(slot)
    engine.expire_bookings(now=START + timedelta(minutes=30))
    with pytest.raises(engine.BookingError, match="passed"):
        engine.check_in(b, now=START + timedelta(minutes=31))
    t = engine.check_in(b, by_staff=True, now=START + timedelta(minutes=31))
    assert t.appointment_at is None
    b.refresh_from_db()
    assert b.status == B.CHECKED_IN


def test_expire_bookings_command(slot, capsys):
    book(slot, at=myt(2020, 1, 1, 9, 0))  # far in the past relative to the real clock
    call_command("expire_bookings")
    assert "Marked 1 booking(s) as no-show." in capsys.readouterr().out


# ---- fair ordering ------------------------------------------------------------


def _codes(counter, now):
    return [t.code for t in engine.waiting_for_counter(counter, now=now)]


def test_on_time_booking_goes_before_walk_ins_from_slot_time(slot, consult, room1):
    w1 = engine.issue_ticket(consult, now=START - timedelta(minutes=40))  # A-001
    b = book(slot)
    engine.check_in(b, now=START - timedelta(minutes=20))  # early: A-002
    engine.issue_ticket(consult, now=START - timedelta(minutes=10))  # A-003
    # Before 10:00 the early booking waits as if it joined at 10:00: behind both walk-ins.
    assert _codes(room1, START - timedelta(minutes=5)) == ["A-001", "A-003", "A-002"]
    # From 10:00 it is called before regular walk-ins.
    assert _codes(room1, START) == ["A-002", "A-001", "A-003"]
    # Priority walk-ins still come first.
    engine.issue_ticket(consult, priority=True, now=START + timedelta(minutes=1))  # A-004
    assert _codes(room1, START + timedelta(minutes=2)) == ["A-004", "A-002", "A-001", "A-003"]
    called = engine.call_next(room1, now=START + timedelta(minutes=2))
    assert called.code == "A-004"
    engine.apply_action(called, "done", now=START + timedelta(minutes=3))
    assert engine.call_next(room1, now=START + timedelta(minutes=4)).code == "A-002"
    assert w1.code == "A-001"


def test_bookings_ordered_by_slot_time_then_arrival(consult, room1):
    s10 = make_slot(consult, START, capacity=2)
    s1030 = make_slot(consult, START + timedelta(minutes=30))
    late_slot = book(s1030, 1)
    first = book(s10, 2)
    second = book(s10, 3)
    engine.check_in(late_slot, by_staff=True, now=START - timedelta(minutes=5))  # A-001, 10:30
    engine.check_in(second, now=START - timedelta(minutes=4))  # A-002
    engine.check_in(first, now=START - timedelta(minutes=3))  # A-003
    now = START + timedelta(minutes=31)
    # Same slot time: by check-in order (id); then the 10:30 slot.
    assert _codes(room1, now) == ["A-002", "A-003", "A-001"]


def test_late_check_in_queues_like_walk_in(slot, consult, room1):
    engine.issue_ticket(consult, now=START)  # A-001
    b = book(slot)
    engine.check_in(b, by_staff=True, now=START + timedelta(minutes=15))  # A-002, late
    engine.issue_ticket(consult, now=START + timedelta(minutes=16))  # A-003
    assert _codes(room1, START + timedelta(minutes=20)) == ["A-001", "A-002", "A-003"]


def test_people_ahead_follows_waiting_order(slot, consult, room1):
    walk = engine.issue_ticket(consult, now=START - timedelta(minutes=40))
    b = book(slot)
    booked = engine.check_in(b, now=START - timedelta(minutes=20))
    assert engine.people_ahead(booked, now=START - timedelta(minutes=5)) == 1
    assert engine.people_ahead(walk, now=START - timedelta(minutes=5)) == 0
    assert engine.people_ahead(booked, now=START) == 0
    assert engine.people_ahead(walk, now=START) == 1
    view = engine.ticket_view(booked, now=START)
    assert datetime.fromisoformat(view.as_dict()["appointment_at"]) == START


def test_board_next_uses_fair_order(slot, branch, consult):
    engine.issue_ticket(consult, now=START - timedelta(minutes=40))
    engine.check_in(book(slot), now=START - timedelta(minutes=20))
    assert engine.board(branch, now=START)["next"] == ["A-002", "A-001"]
    assert Ticket.objects.filter(source="booking").count() == 1
