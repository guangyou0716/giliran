"""Queue engine rules (library surface, no HTTP)."""

import pytest
from django.db import IntegrityError, transaction

from queues import engine
from queues.models import Ticket, TicketEvent

from .conftest import myt

S = Ticket.Status
pytestmark = pytest.mark.django_db


def test_numbers_are_per_service_and_formatted(consult, pharmacy):
    a1 = engine.issue_ticket(consult)
    a2 = engine.issue_ticket(consult)
    p1 = engine.issue_ticket(pharmacy)
    a3 = engine.issue_ticket(consult)
    assert [t.code for t in (a1, a2, p1, a3)] == ["A-001", "A-002", "P-001", "A-003"]
    assert [t.number for t in (a1, a2, a3)] == [1, 2, 3]
    assert all(t.status == S.WAITING for t in (a1, a2, p1, a3))


def test_numbering_restarts_at_midnight_malaysia_time(consult):
    # 23:59 and 00:01 MYT are 15:59 and 16:01 UTC on the same UTC day,
    # but different local days, so the second ticket starts again at 1.
    late = engine.issue_ticket(consult, now=myt(2026, 11, 19, 23, 59))
    early = engine.issue_ticket(consult, now=myt(2026, 11, 20, 0, 1))
    assert (str(late.day), late.code) == ("2026-11-19", "A-001")
    assert (str(early.day), early.code) == ("2026-11-20", "A-001")


def test_inactive_service_and_closed_branch(consult, branch):
    consult.is_active = False
    consult.save()
    with pytest.raises(engine.ServiceUnavailable):
        engine.issue_ticket(consult)
    consult.is_active = True
    consult.save()
    branch.is_open = False
    branch.save()
    with pytest.raises(engine.ServiceUnavailable):
        engine.issue_ticket(consult, source=Ticket.Source.SELF)
    # The front desk can still add walk-ins while self-service is closed.
    assert engine.issue_ticket(consult, source=Ticket.Source.DESK).code == "A-001"


def test_nickname_is_trimmed_and_capped(consult):
    t = engine.issue_ticket(consult, nickname="  " + "x" * 60 + "  ")
    assert t.nickname == "x" * 40


# Worked table: issue order and priority flags -> expected call order.
#   id  code   priority
#   1   A-001  no
#   2   A-002  no
#   3   A-003  yes
#   4   A-004  no
#   5   A-005  yes
# Priority first (A-003, A-005 in arrival order), then the rest in arrival order.
ISSUE = [False, False, True, False, True]
CALL_ORDER = ["A-003", "A-005", "A-001", "A-002", "A-004"]
AHEAD = {"A-001": 2, "A-002": 3, "A-003": 0, "A-004": 4, "A-005": 1}


def _issue_table(service):
    return [engine.issue_ticket(service, priority=p) for p in ISSUE]


def test_call_order_priority_then_fifo(consult, room1):
    _issue_table(consult)
    called = []
    while True:
        t = engine.call_next(room1)
        if t is None:
            break
        called.append(t.code)
        engine.apply_action(t, "done")
    assert called == CALL_ORDER


def test_people_ahead_matches_worked_table(consult):
    tickets = _issue_table(consult)
    assert {t.code: engine.people_ahead(t) for t in tickets} == AHEAD


def test_people_ahead_agrees_with_call_order(consult, room1):
    # Invariant: sorting by "people ahead" gives exactly the order call_next uses.
    tickets = _issue_table(consult)
    by_ahead = [t.code for t in sorted(tickets, key=engine.people_ahead)]
    assert by_ahead == CALL_ORDER
    assert sorted(engine.people_ahead(t) for t in tickets) == list(range(len(tickets)))


def test_people_ahead_is_none_when_not_waiting(consult, room1):
    t = engine.issue_ticket(consult)
    engine.call_next(room1)
    t.refresh_from_db()
    assert engine.people_ahead(t) is None


def test_call_next_only_takes_counter_services(consult, pharmacy, room1, room2):
    engine.issue_ticket(pharmacy)
    assert engine.call_next(room1) is None  # Room 1 serves consultation only
    t = engine.call_next(room2)
    assert t.code == "P-001" and t.counter == room2 and t.status == S.CALLED


def test_call_next_skips_previous_days(consult, room1):
    engine.issue_ticket(consult, now=myt(2020, 1, 1, 9, 0))
    assert engine.call_next(room1) is None


def test_counter_handles_one_ticket_at_a_time(consult, room1):
    engine.issue_ticket(consult)
    engine.issue_ticket(consult)
    first = engine.call_next(room1)
    with pytest.raises(engine.CounterBusy):
        engine.call_next(room1)
    engine.apply_action(first, "start")
    with pytest.raises(engine.CounterBusy):
        engine.call_next(room1)
    engine.apply_action(first, "done")
    assert engine.call_next(room1).code == "A-002"


def test_inactive_counter_cannot_call(consult, room1):
    engine.issue_ticket(consult)
    room1.is_active = False
    room1.save()
    with pytest.raises(engine.ServiceUnavailable):
        engine.call_next(room1)


def test_db_constraint_one_active_ticket_per_counter(consult, room1):
    a = engine.issue_ticket(consult)
    b = engine.issue_ticket(consult)
    engine.call_next(room1)
    b.status, b.counter = S.CALLED, room1
    with pytest.raises(IntegrityError), transaction.atomic():
        b.save()
    a.refresh_from_db()
    assert a.status == S.CALLED


ALL_STATUSES = [s for s, _ in S.choices]


@pytest.mark.parametrize("action", ["recall", "start", "done", "no_show", "cancel"])
@pytest.mark.parametrize("status", ALL_STATUSES)
def test_transition_table(consult, room1, action, status):
    t = engine.issue_ticket(consult)
    Ticket.objects.filter(pk=t.pk).update(status=status, counter=room1)
    allowed_from, to_status = engine.TRANSITIONS[action]
    if status in allowed_from:
        t = engine.apply_action(t, action)
        assert t.status == to_status
    else:
        with pytest.raises(engine.InvalidTransition):
            engine.apply_action(t, action)
        t.refresh_from_db()
        assert t.status == status


def test_unknown_action_rejected(consult):
    t = engine.issue_ticket(consult)
    for action in ("call", "explode", ""):
        with pytest.raises(engine.InvalidTransition):
            engine.apply_action(t, action)


def test_lifecycle_timestamps_and_audit_log(consult, room1, staff_user):
    t = engine.issue_ticket(consult, now=myt(2026, 11, 19, 9, 0))
    engine.call_next(room1, actor=staff_user, now=myt(2026, 11, 19, 9, 5))
    t = engine.apply_action(t, "recall", actor=staff_user, now=myt(2026, 11, 19, 9, 6))
    t = engine.apply_action(t, "start", actor=staff_user, now=myt(2026, 11, 19, 9, 7))
    t = engine.apply_action(t, "done", actor=staff_user, now=myt(2026, 11, 19, 9, 15))
    assert t.recall_count == 1
    assert t.called_at == myt(2026, 11, 19, 9, 6)
    assert t.serving_at == myt(2026, 11, 19, 9, 7)
    assert t.finished_at == myt(2026, 11, 19, 9, 15)
    events = list(
        TicketEvent.objects.filter(ticket=t).values_list("action", "from_status", "to_status")
    )
    assert events == [
        ("issue", "", "waiting"),
        ("call", "waiting", "called"),
        ("recall", "called", "called"),
        ("start", "called", "serving"),
        ("done", "serving", "done"),
    ]
    assert TicketEvent.objects.filter(ticket=t, actor=staff_user).count() == 4


@pytest.mark.parametrize(
    ("ahead", "avg", "counters", "expected"),
    [
        (0, 5, 1, 0),
        (3, 5, 2, 8),  # 15 / 2 = 7.5 -> 8
        (1, 8, 1, 8),
        (4, 3, 3, 4),
        (10, 2, 4, 5),
        (2, 5, 0, None),  # no counter serves it: no estimate
    ],
)
def test_estimate_wait_minutes(ahead, avg, counters, expected):
    assert engine.estimate_wait_minutes(ahead, avg, counters) == expected


def test_ticket_view_uses_active_counters(consult, room1, room2):
    tickets = [engine.issue_ticket(consult) for _ in range(4)]
    last = tickets[-1]
    # 3 ahead x 5 min / 2 active counters = 7.5 -> 8
    assert engine.ticket_view(last).eta_minutes == 8
    room2.is_active = False
    room2.save()
    assert engine.ticket_view(last).eta_minutes == 15
    room1.is_active = False
    room1.save()
    view = engine.ticket_view(last)
    assert (view.ahead, view.eta_minutes) == (3, None)


def test_board_payload(branch, consult, pharmacy, room1, room2):
    at = myt(2026, 11, 19, 9, 0)
    for _ in range(3):
        engine.issue_ticket(consult, now=at)
    engine.issue_ticket(pharmacy, priority=True, now=at)
    engine.call_next(room1, now=myt(2026, 11, 19, 9, 1))  # A-001
    engine.call_next(room2, now=myt(2026, 11, 19, 9, 2))  # P-001 (priority first)
    data = engine.board(branch, now=myt(2026, 11, 19, 10, 0))
    # Most recently called first.
    assert [(s["code"], s["counter"]) for s in data["serving"]] == [
        ("P-001", "Room 2"),
        ("A-001", "Room 1"),
    ]
    assert data["next"] == ["A-002", "A-003"]
    assert {s["prefix"]: s["waiting"] for s in data["services"]} == {"A": 2, "P": 0}
    # Invariant: total waiting equals the sum per service.
    assert data["waiting_total"] == sum(s["waiting"] for s in data["services"])
    # Another day's board is empty.
    assert engine.board(branch, now=myt(2026, 11, 20, 10, 0))["waiting_total"] == 0
