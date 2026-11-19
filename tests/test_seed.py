import pytest
from django.contrib.auth import authenticate
from django.core.management import call_command

from queues.models import Booking, Branch, Counter, Membership, Service, Slot, Ticket

pytestmark = pytest.mark.django_db


def test_seed_demo_is_idempotent():
    call_command("seed_demo", password="seed-pass-123", verbosity=0)
    counts = (
        Branch.objects.count(),
        Service.objects.count(),
        Counter.objects.count(),
        Membership.objects.count(),
        Ticket.objects.count(),
        Slot.objects.count(),
        Booking.objects.count(),
    )
    # Today: 12 tickets x 2 branches; 16 half-hour slots (09:00-17:00) x 3 days x 2 branches;
    # 4 bookings x 2 days x 2 branches. History (fixed random seed): 594 tickets; per past
    # day and branch 3 slots with one booking each (checked in, no-show, cancelled):
    # 14 x 3 x 2 = 84 slots and 84 bookings.
    assert counts == (2, 5, 5, 5, 24 + 594, 96 + 84, 16 + 84)
    call_command("seed_demo", password="seed-pass-123", verbosity=0)
    assert (
        Branch.objects.count(),
        Service.objects.count(),
        Counter.objects.count(),
        Membership.objects.count(),
        Ticket.objects.count(),
        Slot.objects.count(),
        Booking.objects.count(),
    ) == counts


def test_seed_roles_and_login():
    call_command("seed_demo", password="seed-pass-123", no_tickets=True, verbosity=0)
    assert Ticket.objects.count() == 0 and Booking.objects.count() == 0
    assert Slot.objects.count() == 96
    assert authenticate(username="manager", password="seed-pass-123") is not None
    roles = dict(Membership.objects.values_list("user__username", "role"))
    assert roles == {
        "manager": "manager",
        "staff1": "staff",
        "staff2": "staff",
        "manager2": "manager",
        "staff3": "staff",
    }


def test_seed_generates_password_only_for_new_accounts(monkeypatch, capsys):
    monkeypatch.delenv("GILIRAN_DEMO_PASSWORD", raising=False)
    call_command("seed_demo", no_tickets=True)
    out = capsys.readouterr().out
    generated = out.rsplit(": ", 1)[1].strip()
    assert "Generated password for new demo accounts" in out
    assert authenticate(username="staff3", password=generated) is not None
    # Second run creates nobody, so it must not print a password that was never applied.
    call_command("seed_demo", no_tickets=True)
    assert "Generated password" not in capsys.readouterr().out
    assert authenticate(username="staff3", password=generated) is not None


def test_seed_bookings_are_synthetic_and_consistent():
    call_command("seed_demo", password="seed-pass-123", verbosity=0)
    assert set(Booking.objects.values_list("phone", flat=True)) <= {
        f"000000010{i}" for i in range(1, 5)
    }
    for slot in Slot.objects.all():
        held = slot.bookings.exclude(status="cancelled").count()
        assert slot.booked_count == held <= slot.capacity


def test_seed_just_after_midnight_stays_idempotent(monkeypatch):
    # 00:02 MYT: 12 tickets 4 minutes apart would start the previous evening; they
    # must all land on today, or a second run would not see them and add 12 more.
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    fixed = datetime(2026, 11, 20, 0, 2, tzinfo=ZoneInfo("Asia/Kuala_Lumpur"))
    monkeypatch.setattr("django.utils.timezone.now", lambda: fixed)
    call_command("seed_demo", password="seed-pass-123", verbosity=0)
    call_command("seed_demo", password="seed-pass-123", verbosity=0)
    # The 14 days of history end before 18:00 the previous day, so everything taken
    # in the last hour is "today's" demo tickets.
    recent = Ticket.objects.filter(created_at__gt=fixed - timedelta(hours=1))
    assert recent.count() == 24 == Ticket.objects.filter(day=fixed.date()).count()
    assert set(recent.values_list("day", flat=True)) == {fixed.date()}
    assert max(recent.values_list("created_at", flat=True)) <= fixed
    assert min(recent.values_list("created_at", flat=True)) >= fixed - timedelta(minutes=2)
    assert Ticket.objects.count() == 24 + 594
