"""Row locking under real concurrency (PostgreSQL only; SQLite serialises writes)."""

import threading

import pytest
from django.db import connection, connections

from queues import engine
from queues.models import Counter, Ticket

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
