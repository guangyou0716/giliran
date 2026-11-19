"""Row locking under real concurrency (PostgreSQL only; SQLite serialises writes)."""

import threading
from datetime import timedelta

import pytest
from django.db import connection, connections
from django.utils import timezone

from queues import engine
from queues.models import Booking, Counter, Slot, Ticket

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(connection.vendor != "postgresql", reason="needs PostgreSQL row locks"),
]


def _run_parallel(n, fn):
    barrier = threading.Barrier(n)
    results, errors = [], []

    def worker(i):
        try:
            barrier.wait()
            results.append(fn(i))
        except Exception as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    return results


def test_parallel_joins_get_unique_consecutive_numbers(consult):
    n = 8
    _run_parallel(n, lambda i: engine.issue_ticket(consult).number)
    numbers = sorted(Ticket.objects.values_list("number", flat=True))
    assert numbers == list(range(1, n + 1))


def test_parallel_call_next_never_double_assigns(branch, consult):
    counters = []
    for i in range(6):
        c = Counter.objects.create(branch=branch, name=f"Desk {i}")
        c.services.set([consult])
        counters.append(c)
    for _ in range(4):
        engine.issue_ticket(consult)
    results = _run_parallel(len(counters), lambda i: engine.call_next(counters[i]))
    called = [t.code for t in results if t is not None]
    # 4 tickets, 6 desks: exactly 4 calls succeed, each ticket once.
    assert sorted(called) == ["A-001", "A-002", "A-003", "A-004"]
    assert Ticket.objects.filter(status="called").values("counter").distinct().count() == 4


def test_parallel_bookings_never_overbook(consult):
    start = timezone.now() + timedelta(hours=2)
    slot = Slot.objects.create(
        service=consult, starts_at=start, ends_at=start + timedelta(minutes=30), capacity=3
    )
    n = 10

    def attempt(i):
        try:
            return engine.book_slot(slot, nickname=f"Guest {i}", phone=f"00000000{i:02d}").ref
        except engine.SlotFull:
            return None

    results = _run_parallel(n, attempt)
    # 10 people press "Book" at the same moment for 3 seats: exactly 3 get one.
    assert sum(r is not None for r in results) == 3
    slot.refresh_from_db()
    assert slot.booked_count == 3
    assert Booking.objects.filter(slot=slot, status="booked").count() == 3


def test_parallel_cancel_and_book_keep_count_exact(consult):
    start = timezone.now() + timedelta(hours=2)
    slot = Slot.objects.create(
        service=consult, starts_at=start, ends_at=start + timedelta(minutes=30), capacity=4
    )
    held = [engine.book_slot(slot, nickname=f"H{i}", phone=f"000000010{i}") for i in range(4)]

    def work(i):
        if i < 4:
            engine.cancel_booking(held[i])
            return "cancelled"
        try:
            engine.book_slot(slot, nickname=f"N{i}", phone=f"000000020{i}")
            return "booked"
        except engine.SlotFull:
            return "full"

    results = _run_parallel(8, work)
    slot.refresh_from_db()
    active = Booking.objects.filter(slot=slot, status="booked").count()
    # Whatever the interleaving, the counter equals the real number of seats held.
    assert slot.booked_count == active <= slot.capacity
    assert results.count("cancelled") == 4
    assert active == results.count("booked")


def test_parallel_transfers_and_joins_into_one_service_get_unique_numbers(consult, pharmacy):
    tickets = [engine.issue_ticket(consult) for _ in range(4)]

    def work(i):
        if i < 4:
            return engine.transfer_ticket(tickets[i], pharmacy).number
        return engine.issue_ticket(pharmacy).number

    _run_parallel(8, work)
    numbers = sorted(Ticket.objects.filter(service=pharmacy).values_list("number", flat=True))
    assert numbers == list(range(1, 9))
