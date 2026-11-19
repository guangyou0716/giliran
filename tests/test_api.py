"""JSON API used by the live pages."""

import uuid

import pytest
from django.urls import reverse

from queues import engine

pytestmark = pytest.mark.django_db


def test_ticket_status_json(client, consult, room1, room2):
    engine.issue_ticket(consult)
    t = engine.issue_ticket(consult, nickname="Secret Name")
    r = client.get(reverse("api:ticket", args=[t.token]))
    assert r.status_code == 200
    assert r["Cache-Control"].startswith("max-age=0")
    data = r.json()
    assert data == {
        "code": "A-002",
        "status": "waiting",
        "status_label": "Waiting",
        "service": "Consultation",
        "branch": "Test Clinic",
        "priority": False,
        "ahead": 1,
        "eta_minutes": 3,  # 1 ahead x 5 min / 2 counters = 2.5 -> 3
        "counter": None,
        "recall_count": 0,
        "called_at": None,
    }
    assert "Secret Name" not in r.content.decode()


def test_ticket_status_after_call(client, consult, room1):
    t = engine.issue_ticket(consult)
    engine.call_next(room1)
    data = client.get(reverse("api:ticket", args=[t.token])).json()
    assert (data["status"], data["counter"], data["ahead"], data["eta_minutes"]) == (
        "called",
        "Room 1",
        None,
        None,
    )
    assert data["called_at"] is not None


def test_ticket_status_translated(client, consult):
    t = engine.issue_ticket(consult)
    r = client.get(reverse("api:ticket", args=[t.token]), HTTP_ACCEPT_LANGUAGE="ms")
    assert r.json()["status_label"] == "Menunggu"
    r = client.get(reverse("api:ticket", args=[t.token]), HTTP_ACCEPT_LANGUAGE="zh-hans")
    assert r.json()["status_label"] == "等候中"


def test_eta_text_matches_js_wording(consult, room1):
    from django.utils import translation

    tickets = [engine.issue_ticket(consult) for _ in range(3)]
    assert engine.ticket_view(tickets[0]).eta_text == "You are next"
    assert engine.ticket_view(tickets[2]).eta_text == "~10 min"  # 2 ahead x 5 min / 1 counter
    with translation.override("zh-hans"):
        assert engine.ticket_view(tickets[2]).eta_text == "约 10 分钟"
    room1.is_active = False
    room1.save()
    assert engine.ticket_view(tickets[2]).eta_text == "—"


def test_unknown_token_404(client, db):
    assert client.get(reverse("api:ticket", args=[uuid.uuid4()])).status_code == 404


def test_api_is_read_only(client, consult, branch):
    t = engine.issue_ticket(consult)
    assert client.post(reverse("api:ticket", args=[t.token])).status_code == 405
    assert client.post(reverse("api:board", args=[branch.slug])).status_code == 405


def test_board_json(client, branch, consult, room1):
    engine.issue_ticket(consult)
    engine.issue_ticket(consult)
    engine.call_next(room1)
    data = client.get(reverse("api:board", args=[branch.slug])).json()
    assert data["branch"] == "Test Clinic"
    assert [s["code"] for s in data["serving"]] == ["A-001"]
    assert data["next"] == ["A-002"]
    assert data["waiting_total"] == 1
