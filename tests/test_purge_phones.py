"""Phone retention: old bookings lose their phone number; idempotent; keeps everything else."""

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction

from queues import engine
from queues.models import Booking, Slot

from .conftest import myt

pytestmark = pytest.mark.django_db
NOW = myt(2026, 11, 20, 12, 0)


@pytest.fixture(autouse=True)
def retention(settings):
    settings.GILIRAN_PHONE_RETENTION_DAYS = 30


def slot_at(service, when):
    return Slot.objects.create(
        service=service, starts_at=when, ends_at=when + timedelta(minutes=30), capacity=50
    )


def book(slot, n):
    return engine.book_slot(
        slot,
        nickname=f"Guest {n}",
        phone=f"000-000 {n:04d}",
        now=slot.starts_at - timedelta(days=1),
    )


def test_old_bookings_lose_phone_recent_ones_keep_it(consult):
    old = book(slot_at(consult, NOW - timedelta(days=31)), 1)
    edge = book(slot_at(consult, NOW - timedelta(days=30) + timedelta(minutes=1)), 2)
    future = book(slot_at(consult, NOW + timedelta(days=1)), 3)
    assert engine.purge_phones(now=NOW) == 1
    for b in (old, edge, future):
        b.refresh_from_db()
    assert old.phone == "" and edge.phone and future.phone
    # Everything but the phone stays.
    assert (old.nickname, old.ref, old.status) == ("Guest 1", old.ref, Booking.Status.BOOKED)
    assert old.phone_masked == "—"


def test_purge_is_idempotent(consult):
    book(slot_at(consult, NOW - timedelta(days=60)), 1)
    assert engine.purge_phones(now=NOW) == 1
    assert engine.purge_phones(now=NOW) == 0
    assert Booking.objects.exclude(phone="").count() == 0


def test_purge_covers_every_status(consult):
    s = slot_at(consult, NOW - timedelta(days=45))
    a, b, c = book(s, 1), book(s, 2), book(s, 3)
    Booking.objects.filter(pk=b.pk).update(status=Booking.Status.CANCELLED)
    Booking.objects.filter(pk=c.pk).update(status=Booking.Status.NO_SHOW)
    assert engine.purge_phones(now=NOW) == 3
    assert not Booking.objects.exclude(phone="").exists()
    assert a.pk


def test_blank_phones_do_not_collide_on_the_unique_constraint(consult):
    # One phone per slot is enforced for real numbers, but purged (blank) rows may share "".
    s = slot_at(consult, NOW - timedelta(days=40))
    book(s, 1)
    book(s, 2)
    engine.purge_phones(now=NOW)
    assert Booking.objects.filter(slot=s, phone="").count() == 2
    # The constraint still stops two active bookings with the same real phone.
    fresh = slot_at(consult, NOW + timedelta(days=2))
    book(fresh, 5)
    with pytest.raises(IntegrityError), transaction.atomic():
        Booking.objects.create(slot=fresh, ref="DUPLICAT", nickname="x", phone="0000000005")


def test_custom_days_and_validation(consult):
    book(slot_at(consult, NOW - timedelta(days=5)), 1)
    assert engine.purge_phones(30, now=NOW) == 0
    assert engine.purge_phones(3, now=NOW) == 1
    with pytest.raises(ValueError):
        engine.purge_phones(0)


def test_management_command(consult, settings):
    book(slot_at(consult, timedelta(days=0) + engine.timezone.now() - timedelta(days=40)), 1)
    out = StringIO()
    call_command("purge_phones", stdout=out)
    assert "Blanked the phone number of 1 booking(s)." in out.getvalue()
    out = StringIO()
    call_command("purge_phones", stdout=out)
    assert "of 0 booking(s)" in out.getvalue()
    with pytest.raises(CommandError):
        call_command("purge_phones", "--days", "0")


def test_command_uses_setting_and_days_option(consult, settings):
    settings.GILIRAN_PHONE_RETENTION_DAYS = 7
    book(slot_at(consult, engine.timezone.now() - timedelta(days=10)), 1)
    out = StringIO()
    call_command("purge_phones", "--days", "30", stdout=out)
    assert "of 0 booking(s)" in out.getvalue()
    call_command("purge_phones", stdout=out)
    assert Booking.objects.filter(phone="").count() == 1


def test_staff_pages_render_purged_booking(staff_client, branch, consult):
    from django.urls import reverse

    b = book(slot_at(consult, myt(2026, 10, 1, 9, 0)), 1)
    engine.purge_phones(now=NOW)
    r = staff_client.get(
        reverse("staff:appointments", args=[branch.slug]) + "?day=2026-10-01&q=Guest"
    )
    assert r.status_code == 200 and b.ref in r.content.decode()


def test_search_by_last_digits_never_matches_purged_rows(staff_client, branch, consult):
    from django.urls import reverse

    book(slot_at(consult, myt(2026, 10, 1, 9, 0)), 1)
    engine.purge_phones(now=NOW)
    r = staff_client.get(
        reverse("staff:appointments", args=[branch.slug]) + "?day=2026-10-01&q=0001"
    )
    assert "Guest 1" not in r.content.decode()
