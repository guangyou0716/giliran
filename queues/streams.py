"""Server-Sent Events for the live pages, built for plain sync Django (WSGI/gunicorn).

Each stream is a bounded ``StreamingHttpResponse``: it sends the current state at once,
then every ``GILIRAN_SSE_TICK_SECONDS`` checks a cheap change marker (the newest
``TicketEvent`` id of the branch and day, one indexed query) and only rebuilds the
payload when the marker moved or every ``GILIRAN_SSE_REFRESH_SECONDS`` (estimates also depend on the
clock and on counter settings, which leave no ticket event). A payload is sent only when
it differs from the last one sent; otherwise a heartbeat comment keeps proxies from
closing the idle connection. After ``GILIRAN_SSE_MAX_SECONDS`` the server sends a
``bye`` event and closes, and the browser opens a fresh stream, so no worker is held
for longer than that.

Wire format (https://html.spec.whatwg.org/multipage/server-sent-events.html):
    retry: 3000            reconnect delay hint for the browser
    data: {...}            same JSON as the matching /api/ endpoint
    : ping                 heartbeat comment
    event: end             the ticket is finished, do not reconnect
    event: bye             stream ends normally, reconnect now
"""

import json
from collections.abc import Callable, Iterator
from datetime import date
from time import monotonic, sleep

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Max
from django.http import StreamingHttpResponse
from django.utils.cache import add_never_cache_headers

from .models import TicketEvent

RETRY_MS = 3000
HEARTBEAT = ": ping\n\n"
BYE = "event: bye\ndata: {}\n\n"
END = "event: end\ndata: {}\n\n"


def data_event(payload: dict) -> str:
    """One SSE message; JSON is single-line, so one ``data:`` line suffices."""
    return "data: " + json.dumps(payload, cls=DjangoJSONEncoder) + "\n\n"


def branch_marker(branch_id: int, day: date) -> int:
    """Cheap change marker: every ticket change (issue, call, done, ...) logs an event.

    Limited to one branch and day, so it uses the (branch, day, status) ticket index and
    the event foreign-key index instead of scanning the whole history.
    """
    events = TicketEvent.objects.filter(ticket__branch_id=branch_id, ticket__day=day)
    return events.aggregate(m=Max("id"))["m"] or 0


def event_stream(
    compute: Callable[[], dict | None],
    marker: Callable[[], object],
    *,
    finished: Callable[[dict], bool] = lambda payload: False,
    max_seconds: float | None = None,
    tick: float | None = None,
    heartbeat: float | None = None,
    refresh: float | None = None,
) -> Iterator[str]:
    """Yield SSE text chunks. ``compute`` returns the payload, or None when it is gone."""
    max_seconds = settings.GILIRAN_SSE_MAX_SECONDS if max_seconds is None else max_seconds
    tick = settings.GILIRAN_SSE_TICK_SECONDS if tick is None else tick
    heartbeat = settings.GILIRAN_SSE_HEARTBEAT_SECONDS if heartbeat is None else heartbeat
    refresh = settings.GILIRAN_SSE_REFRESH_SECONDS if refresh is None else refresh

    started = monotonic()
    last_marker = marker()
    last_payload = compute()
    if last_payload is None:
        yield END
        return
    yield f"retry: {RETRY_MS}\n" + data_event(last_payload)
    if finished(last_payload):
        yield END
        return
    last_built = last_sent = started
    while True:
        sleep(tick)
        now = monotonic()
        if now - started >= max_seconds:
            break
        current = marker()
        if current != last_marker or now - last_built >= refresh:
            last_marker, last_built = current, now
            payload = compute()
            if payload is None:
                yield END
                return
            if payload != last_payload:
                last_payload, last_sent = payload, now
                yield data_event(payload)
                if finished(payload):
                    yield END
                    return
                continue
        if now - last_sent >= heartbeat:
            last_sent = now
            yield HEARTBEAT
    yield BYE


def sse_response(stream: Iterator[str]) -> StreamingHttpResponse:
    response = StreamingHttpResponse(stream, content_type="text/event-stream; charset=utf-8")
    add_never_cache_headers(response)  # includes no-cache
    response["X-Accel-Buffering"] = "no"  # nginx: do not buffer the stream
    return response
