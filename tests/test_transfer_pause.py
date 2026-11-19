"""Ticket transfer between services and counter pause (engine + staff console)."""

from datetime import timedelta

import pytest
from django.test import Client
from django.urls import reverse

from queues import engine
from queues.models import Counter, Service, Ticket, TicketEvent

from .conftest import myt

S = Ticket.Status
pytestmark = pytest.mark.django_db
NOW = myt(2026, 11, 19, 10, 0)


# ---- transfer ---------------------------------------------------------------------------


def test_transfer_waiting_ticket_gets_new_number_and_keeps_queue_time(consult, pharmacy, room2):
    engine.issue_ticket(pharmacy, now=NOW)  # P-001, earlier
    a1 = engine.issue_ticket(consult, now=NOW + timedelta(minutes=1))  # A-001
    p2 = engine.issue_ticket(pharmacy, now=NOW + timedelta(minutes=2))  # P-002
    moved = engine.transfer_ticket(a1, pharmacy, now=NOW + timedelta(minutes=5))
    assert (moved.service, moved.code, moved.number) == (pharmacy, "P-003", 3)
    assert moved.status == S.WAITING and moved.created_at == a1.created_at
    # Still ordered by arrival time: after P-001 (earlier), before P-002 (later).
    order = [t.code for t in engine.waiting_order(Ticket.objects.filter(service=pharmacy), NOW)]
    assert order == ["P-001", "P-003", "P-002"]
    assert p2.created_at > moved.created_at


def test_transfer_keeps_priority_and_never_reuses_numbers(consult, pharmacy):
    t = engine.issue_ticket(consult, priority=True, now=NOW)
    moved = engine.transfer_ticket(t, pharmacy, now=NOW)
    assert moved.priority is True
    again = engine.issue_ticket(pharmacy, now=NOW)
    assert again.code == "P-002"
    # Moving back takes a fresh number as well; the unique (service, day, number) holds.
    back = engine.transfer_ticket(moved, consult, now=NOW)
    assert back.code == "A-002"


def test_transfer_called_ticket_frees_counter_and_resets_service(consult, pharmacy, room2):
    engine.issue_ticket(consult, now=NOW)
    called = engine.call_next(room2, now=NOW)
    engine.apply_action(called, "start", now=NOW)
    moved = engine.transfer_ticket(called, pharmacy, now=NOW + timedelta(minutes=3))
    assert moved.status == S.WAITING and moved.counter is None
    assert moved.called_at is None and moved.first_called_at is None and moved.serving_at is None
    assert engine.active_ticket(room2) is None
    # The counter can take the next ticket and the moved one is callable again.
    again = engine.call_next(room2, now=NOW + timedelta(minutes=4))
    assert again.pk == moved.pk and again.first_called_at is not None


def test_transfer_is_audited(consult, pharmacy, room2, staff_user):
    engine.issue_ticket(consult, now=NOW)
    called = engine.call_next(room2, now=NOW)
    moved = engine.transfer_ticket(called, pharmacy, actor=staff_user, now=NOW)
    event = TicketEvent.objects.filter(ticket=moved).latest("id")
    assert (event.action, event.from_status, event.to_status) == ("transfer", "called", "waiting")
    assert event.note == "A-001 -> P-001"
    assert event.actor == staff_user and event.counter == room2


@pytest.mark.parametrize("status", [S.DONE, S.NO_SHOW, S.CANCELLED])
def test_finished_tickets_cannot_be_transferred(consult, pharmacy, status):
    t = engine.issue_ticket(consult, now=NOW)
    Ticket.objects.filter(pk=t.pk).update(status=status)
    with pytest.raises(engine.InvalidTransition):
        engine.transfer_ticket(t, pharmacy, now=NOW)


def test_transfer_rejects_same_service_other_branch_inactive_and_old_day(
    consult, pharmacy, other_branch
):
    t = engine.issue_ticket(consult, now=NOW)
    with pytest.raises(engine.ServiceUnavailable):
        engine.transfer_ticket(t, consult, now=NOW)
    foreign = Service.objects.create(branch=other_branch, name="X", prefix="X")
    with pytest.raises(engine.ServiceUnavailable):
        engine.transfer_ticket(t, foreign, now=NOW)
    pharmacy.is_active = False
    pharmacy.save()
    with pytest.raises(engine.ServiceUnavailable):
        engine.transfer_ticket(t, pharmacy, now=NOW)
    pharmacy.is_active = True
    pharmacy.save()
    with pytest.raises(engine.InvalidTransition):
        engine.transfer_ticket(t, pharmacy, now=NOW + timedelta(days=1))
    assert Ticket.objects.get(pk=t.pk).code == "A-001"  # nothing changed


def test_transfer_updates_stream_marker_and_ticket_page(consult, pharmacy, client):
    from queues import streams

    t = engine.issue_ticket(consult)
    before = streams.branch_marker(t.branch_id, t.day)
    engine.transfer_ticket(t, pharmacy)
    assert streams.branch_marker(t.branch_id, t.day) > before
    html = client.get(reverse("ticket", args=[t.token])).content.decode()
    assert "P-001" in html
    data = client.get(reverse("api:ticket", args=[t.token])).json()
    assert (data["code"], data["status"], data["service"]) == ("P-001", "waiting", "Pharmacy")


def test_transfer_view_staff_moves_ticket(staff_client, consult, pharmacy, room2):
    t = engine.issue_ticket(consult)
    r = staff_client.post(
        reverse("staff:ticket_transfer", args=[t.pk]),
        {"service": pharmacy.pk, "next": reverse("staff:counter", args=[room2.pk])},
    )
    assert r.status_code == 302 and r.url == reverse("staff:counter", args=[room2.pk])
    t.refresh_from_db()
    assert (t.code, t.service) == ("P-001", pharmacy)
    page = staff_client.get(r.url, follow=True).content.decode()
    assert "moved to Pharmacy as P-001" in page


def test_transfer_view_requires_branch_staff_and_post(
    client, staff_client, outsider, consult, pharmacy
):
    t = engine.issue_ticket(consult)
    url = reverse("staff:ticket_transfer", args=[t.pk])
    assert staff_client.get(url).status_code == 405
    anonymous = Client()
    r = anonymous.post(url, {"service": pharmacy.pk})
    assert r.status_code == 302 and "/accounts/login/" in r.url
    foreign_staff = Client()
    foreign_staff.force_login(outsider)
    assert foreign_staff.post(url, {"service": pharmacy.pk}).status_code == 403
    t.refresh_from_db()
    assert t.code == "A-001"


def test_transfer_view_refuses_foreign_or_missing_service(
    staff_client, consult, other_branch, pharmacy
):
    foreign = Service.objects.create(branch=other_branch, name="X", prefix="X")
    t = engine.issue_ticket(consult)
    url = reverse("staff:ticket_transfer", args=[t.pk])
    for payload in ({"service": foreign.pk}, {"service": "abc"}, {}):
        r = staff_client.post(url, payload, follow=True)
        assert "Choose a service of this branch." in r.content.decode()
    t.refresh_from_db()
    assert t.code == "A-001"


def test_transfer_view_ignores_offsite_next(staff_client, consult, pharmacy):
    t = engine.issue_ticket(consult)
    r = staff_client.post(
        reverse("staff:ticket_transfer", args=[t.pk]),
        {"service": pharmacy.pk, "next": "https://evil.example/staff/"},
    )
    assert r.url == reverse("staff:dashboard")


def test_console_shows_transfer_form_for_current_and_waiting(
    staff_client, consult, pharmacy, room2
):
    engine.issue_ticket(consult)
    engine.issue_ticket(consult)
    engine.call_next(room2)
    html = staff_client.get(reverse("staff:counter", args=[room2.pk])).content.decode()
    assert html.count("Transfer to another service") == 2
    assert f'value="{pharmacy.pk}"' in html


# ---- pause ------------------------------------------------------------------------------


def test_paused_counter_is_not_offered_tickets(consult, room1):
    engine.issue_ticket(consult)
    engine.set_paused(room1, True)
    with pytest.raises(engine.CounterPaused):
        engine.call_next(room1)
    assert Ticket.objects.get().status == S.WAITING
    engine.set_paused(room1, False)
    assert engine.call_next(room1).code == "A-001"


def test_other_counter_still_calls_while_one_is_paused(consult, room1, room2):
    engine.issue_ticket(consult)
    engine.set_paused(room1, True)
    assert engine.call_next(room2).code == "A-001"


def test_cannot_pause_with_a_ticket_and_pause_is_idempotent(consult, room1):
    engine.issue_ticket(consult)
    engine.call_next(room1)
    with pytest.raises(engine.CounterBusy):
        engine.set_paused(room1, True)
    room1.refresh_from_db()
    assert room1.is_paused is False
    engine.apply_action(engine.active_ticket(room1), "done")
    engine.set_paused(room1, True)
    engine.set_paused(room1, True)
    engine.set_paused(room1, False)
    engine.set_paused(room1, False)
    room1.refresh_from_db()
    assert room1.is_paused is False


def test_paused_counter_leaves_wait_estimate(consult, room1, room2):
    for _ in range(4):
        engine.issue_ticket(consult)
    last = Ticket.objects.order_by("-id").first()
    two = engine.ticket_view(last)  # ahead 3, avg 5, 2 counters -> ceil(7.5) = 8
    assert two.eta_minutes == 8
    engine.set_paused(room1, True)
    one = engine.ticket_view(last)  # 1 counter -> 15
    assert one.eta_minutes == 15
    engine.set_paused(room2, True)
    assert engine.ticket_view(last).eta_minutes is None  # nobody serving: no estimate


def test_board_lists_paused_counters(consult, room1, room2, client, branch):
    assert engine.board(branch)["paused"] == []
    engine.set_paused(room1, True)
    assert engine.board(branch)["paused"] == ["Room 1"]
    assert client.get(reverse("api:board", args=[branch.slug])).json()["paused"] == ["Room 1"]
    html = client.get(reverse("board", args=[branch.slug])).content.decode()
    assert "Paused: Room 1" in html
    engine.set_paused(room1, False)
    html = client.get(reverse("board", args=[branch.slug])).content.decode()
    assert "Paused: " not in html


def test_inactive_counter_is_not_listed_as_paused(consult, room1, branch):
    engine.set_paused(room1, True)
    Counter.objects.filter(pk=room1.pk).update(is_active=False)
    assert engine.board(branch)["paused"] == []


def test_pause_resume_via_console(staff_client, consult, room1):
    engine.issue_ticket(consult)
    url = reverse("staff:counter_action", args=[room1.pk])
    r = staff_client.post(url, {"action": "pause"}, follow=True)
    html = r.content.decode()
    assert "Counter paused." in html and "Resume counter" in html
    assert "Call next" not in html.replace("Calls", "")
    assert "Paused" in html
    r = staff_client.post(url, {"action": "call_next"}, follow=True)
    assert "paused" in r.content.decode().lower()
    assert Ticket.objects.get().status == S.WAITING
    r = staff_client.post(url, {"action": "resume"}, follow=True)
    assert "Counter resumed." in r.content.decode() and "Pause counter" in r.content.decode()
    staff_client.post(url, {"action": "call_next"})
    assert Ticket.objects.get().status == S.CALLED


def test_pause_refused_while_serving_shows_error(staff_client, consult, room1):
    engine.issue_ticket(consult)
    engine.call_next(room1)
    r = staff_client.post(
        reverse("staff:counter_action", args=[room1.pk]), {"action": "pause"}, follow=True
    )
    assert "before pausing" in r.content.decode()
    room1.refresh_from_db()
    assert not room1.is_paused


def test_dashboard_marks_paused_counter(staff_client, room1):
    engine.set_paused(room1, True)
    assert "Paused" in staff_client.get(reverse("staff:dashboard")).content.decode()
