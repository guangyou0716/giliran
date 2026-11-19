"""Server-Sent Events streams for the ticket page and the display board."""

import json
import uuid

import pytest
from django.urls import reverse

from queues import engine, streams
from queues.models import Ticket

pytestmark = pytest.mark.django_db


class FakeClock:
    """Replaces time.monotonic/sleep in queues.streams; runs callbacks at given times."""

    def __init__(self):
        self.now = 1000.0
        self.at = {}
        self.sleeps = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        for when in sorted(self.at):
            if when <= self.now - 1000.0:
                self.at.pop(when)()


@pytest.fixture
def clock(monkeypatch, settings):
    settings.GILIRAN_SSE_MAX_SECONDS = 10
    settings.GILIRAN_SSE_TICK_SECONDS = 1
    settings.GILIRAN_SSE_HEARTBEAT_SECONDS = 4
    settings.GILIRAN_SSE_REFRESH_SECONDS = 100
    fake = FakeClock()
    monkeypatch.setattr(streams, "monotonic", fake.monotonic)
    monkeypatch.setattr(streams, "sleep", fake.sleep)
    return fake


def events(response) -> list[tuple[str, object]]:
    """Parse an SSE body into (kind, value): ("data", dict), ("event", name), ("ping", None)."""
    body = b"".join(response.streaming_content).decode()
    assert body.endswith("\n\n")
    out = []
    for block in body.split("\n\n")[:-1]:
        fields = dict(line.split(": ", 1) for line in block.split("\n"))
        if "" in fields:
            out.append(("ping", None))
        elif "event" in fields:
            out.append(("event", fields["event"]))
        else:
            out.append(("data", json.loads(fields["data"])))
    return out


def data_events(evts):
    return [v for k, v in evts if k == "data"]


def test_ticket_stream_headers_first_payload_and_bye(client, clock, consult, room1):
    engine.issue_ticket(consult)
    t = engine.issue_ticket(consult, nickname="Secret Name")
    r = client.get(reverse("api:ticket_stream", args=[t.token]))
    assert r.status_code == 200
    assert r.streaming
    assert r["Content-Type"] == "text/event-stream; charset=utf-8"
    assert "no-cache" in r["Cache-Control"]
    assert r["X-Accel-Buffering"] == "no"
    body_events = events(r)
    api = client.get(reverse("api:ticket", args=[t.token])).json()
    assert body_events[0] == ("data", api)
    assert body_events[-1] == ("event", "bye")
    assert len(data_events(body_events)) == 1  # nothing changed
    assert clock.now - 1000.0 == 10  # closed at GILIRAN_SSE_MAX_SECONDS


def test_retry_hint_and_heartbeats(client, clock, consult):
    t = engine.issue_ticket(consult)
    r = client.get(reverse("api:ticket_stream", args=[t.token]))
    body = b"".join(r.streaming_content).decode()
    assert body.startswith(f"retry: {streams.RETRY_MS}\ndata: {{")
    # 10 s stream, heartbeat every 4 s of silence -> at 4 s and 8 s.
    assert body.count(": ping\n\n") == 2
    assert "Secret" not in body


def test_change_is_pushed_and_equals_api(client, clock, consult, room1):
    first = engine.issue_ticket(consult)
    t = engine.issue_ticket(consult)
    snapshots = {}

    def call_first():
        engine.call_next(room1)
        snapshots["api"] = client.get(reverse("api:ticket", args=[t.token])).json()

    clock.at[3] = call_first
    payloads = data_events(events(client.get(reverse("api:ticket_stream", args=[t.token]))))
    assert [p["ahead"] for p in payloads] == [1, 0]
    assert payloads[1] == snapshots["api"]
    assert first.code != t.code


def test_ticket_stream_ends_when_ticket_finishes(client, clock, consult, room1):
    t = engine.issue_ticket(consult)

    def finish():
        called = engine.call_next(room1)
        engine.apply_action(called, "done")

    clock.at[2] = finish
    evts = events(client.get(reverse("api:ticket_stream", args=[t.token])))
    assert [p["status"] for p in data_events(evts)] == ["waiting", "done"]
    assert evts[-1] == ("event", "end")
    assert ("event", "bye") not in evts
    assert clock.now - 1000.0 < 10


def test_finished_ticket_sends_state_then_end(client, clock, consult):
    t = engine.issue_ticket(consult)
    engine.apply_action(t, "cancel")
    evts = events(client.get(reverse("api:ticket_stream", args=[t.token])))
    assert evts == [
        ("data", client.get(reverse("api:ticket", args=[t.token])).json()),
        ("event", "end"),
    ]
    assert clock.sleeps == 0


def test_deleted_ticket_ends_stream(client, clock, consult):
    t = engine.issue_ticket(consult)
    clock.at[2] = lambda: Ticket.objects.filter(pk=t.pk).delete()
    evts = events(client.get(reverse("api:ticket_stream", args=[t.token])))
    assert evts[-1] == ("event", "end")


def test_stream_uses_request_language(client, clock, consult):
    t = engine.issue_ticket(consult)
    r = client.get(reverse("api:ticket_stream", args=[t.token]), HTTP_ACCEPT_LANGUAGE="ms")
    assert data_events(events(r))[0]["status_label"] == "Menunggu"


def test_board_stream_equals_board_json_and_hides_private_fields(
    client, clock, branch, consult, room1
):
    engine.issue_ticket(consult, nickname="Secret Name")
    engine.issue_ticket(consult)
    snapshots = {}

    def call():
        engine.call_next(room1)
        snapshots["api"] = client.get(reverse("api:board", args=[branch.slug])).json()

    clock.at[5] = call
    r = client.get(reverse("api:board_stream", args=[branch.slug]))
    assert r["Content-Type"] == "text/event-stream; charset=utf-8"
    raw = b"".join(r.streaming_content).decode()
    assert "Secret Name" not in raw and "token" not in raw and "nickname" not in raw
    payloads = [
        json.loads(line[len("data: ") :]) for line in raw.split("\n") if line.startswith('data: {"')
    ]
    assert payloads[0]["next"] == ["A-001", "A-002"]
    assert payloads[-1] == snapshots["api"]
    assert [s["code"] for s in payloads[-1]["serving"]] == ["A-001"]
    assert payloads[-1]["next"] == ["A-002"]


def test_unknown_token_404(client, clock, db):
    assert client.get(reverse("api:ticket_stream", args=[uuid.uuid4()])).status_code == 404


def test_unknown_branch_404(client, clock, db):
    assert client.get(reverse("api:board_stream", args=["nope"])).status_code == 404


def test_streams_are_get_only(client, clock, branch, consult):
    t = engine.issue_ticket(consult)
    assert client.post(reverse("api:ticket_stream", args=[t.token])).status_code == 405
    assert client.post(reverse("api:board_stream", args=[branch.slug])).status_code == 405


def test_streams_can_be_disabled(client, settings, branch, consult):
    t = engine.issue_ticket(consult)
    settings.GILIRAN_SSE = False
    assert client.get(reverse("api:ticket_stream", args=[t.token])).status_code == 404
    assert client.get(reverse("api:board_stream", args=[branch.slug])).status_code == 404
    page = client.get(reverse("ticket", args=[t.token])).content.decode()
    assert "data-stream-url" not in page
    assert 'data-poll-url="/api/tickets/' in page
    assert (
        "data-stream-url" not in client.get(reverse("board", args=[branch.slug])).content.decode()
    )


def test_pages_advertise_streams(client, branch, consult):
    t = engine.issue_ticket(consult)
    page = client.get(reverse("ticket", args=[t.token])).content.decode()
    assert f'data-stream-url="/api/tickets/{t.token}/stream/"' in page
    board = client.get(reverse("board", args=[branch.slug])).content.decode()
    assert f'data-stream-url="/api/branches/{branch.slug}/board/stream/"' in board


# --- event_stream unit tests (no HTTP) -----------------------------------------------------


def run(clock, compute, marker, **kw):
    kw = {"max_seconds": 10, "tick": 1, "heartbeat": 4, "refresh": 100} | kw
    return list(streams.event_stream(compute, marker, **kw))


def test_payload_rebuilt_only_when_marker_moves(clock):
    calls = []
    marker = {"v": 1}

    def compute():
        calls.append(clock.now)
        return {"n": marker["v"] // 2}

    clock.at[3] = lambda: marker.update(v=2)  # changes payload
    clock.at[6] = lambda: marker.update(v=3)  # marker moves, payload the same
    chunks = run(clock, compute, lambda: marker["v"])
    assert len(calls) == 3  # initial + two marker moves, not every tick
    assert chunks[0] == f'retry: {streams.RETRY_MS}\ndata: {{"n": 0}}\n\n'
    assert [c for c in chunks[1:] if c.startswith("data:")] == [
        'data: {"n": 1}\n\n',
    ]
    assert chunks[-1] == streams.BYE


def test_periodic_refresh_without_marker_change(clock):
    calls = []

    def compute():
        calls.append(clock.now - 1000.0)
        return {"n": 1}

    run(clock, compute, lambda: 0, refresh=3)
    assert calls == [0, 3, 6, 9]


def test_missing_payload_ends_at_once(clock):
    assert run(clock, lambda: None, lambda: 0) == [streams.END]


def test_heartbeat_only_after_silence(clock):
    state = {"n": 0}

    def bump():
        state["n"] += 1

    clock.at[3] = bump
    chunks = run(clock, lambda: dict(state), lambda: state["n"])
    # data at 0 and 3; silence afterwards -> ping at 7; bye at 10.
    assert chunks.count(streams.HEARTBEAT) == 1
    assert len(chunks) == 4


def test_data_event_is_single_line_json():
    text = streams.data_event({"a": "line1\nline2", "b": None})
    assert text.count("\n") == 2 and text.endswith("\n\n")
    assert json.loads(text[len("data: ") :]) == {"a": "line1\nline2", "b": None}


def test_branch_marker_moves_on_every_ticket_change(branch, consult, room1):
    m0 = streams.branch_marker(branch.pk, engine.local_day())
    t = engine.issue_ticket(consult)
    m1 = streams.branch_marker(branch.pk, engine.local_day())
    engine.call_next(room1)
    m2 = streams.branch_marker(branch.pk, engine.local_day())
    engine.apply_action(Ticket.objects.get(pk=t.pk), "recall")
    m3 = streams.branch_marker(branch.pk, engine.local_day())
    assert m0 < m1 < m2 < m3


def test_branch_marker_is_per_day(branch, consult):
    from datetime import timedelta

    engine.issue_ticket(consult)
    today = engine.local_day()
    assert streams.branch_marker(branch.pk, today) > 0
    assert streams.branch_marker(branch.pk, today - timedelta(days=1)) == 0


def test_branch_marker_ignores_other_branches(branch, other_branch, consult):
    from queues.models import Service

    other = Service.objects.create(branch=other_branch, name="Other", prefix="O")
    engine.issue_ticket(consult)
    before = streams.branch_marker(branch.pk, engine.local_day())
    engine.issue_ticket(other)
    assert streams.branch_marker(branch.pk, engine.local_day()) == before


# --- "almost your turn" on the ticket page -------------------------------------------------


def test_almost_banner_rendered_at_threshold(client, settings, consult, room1):
    settings.GILIRAN_ALMOST_AHEAD = 2
    tickets = [engine.issue_ticket(consult) for _ in range(4)]

    def page(t):
        return client.get(reverse("ticket", args=[t.token])).content.decode()

    far = page(tickets[3])  # 3 ahead
    assert 'data-is-almost="0"' in far
    assert "data-almost-banner hidden" in far
    near = page(tickets[2])  # 2 ahead
    assert 'data-is-almost="1"' in near
    assert "data-almost-banner \n" in near or "data-almost-banner >" in near
    assert 'data-almost-ahead="2"' in near
    assert "Almost your turn! Please stay near the counters." in near
    assert "Play a sound and notify me when it is almost my turn" in near
    assert 'aria-live="assertive"' in near


def test_almost_strings_translated(client, consult):
    t = engine.issue_ticket(consult)
    ms = client.get(reverse("ticket", args=[t.token]), HTTP_ACCEPT_LANGUAGE="ms").content.decode()
    assert "Giliran anda hampir tiba. Orang di hadapan: {n}." in ms
    zh = client.get(
        reverse("ticket", args=[t.token]), HTTP_ACCEPT_LANGUAGE="zh-hans"
    ).content.decode()
    assert "正在叫您的号码 {code}，请前往 {counter}。" in zh
