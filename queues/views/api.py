"""Read-only JSON endpoints used by the live pages (and usable by other clients).

The ``*_stream`` views push the same JSON as Server-Sent Events (see queues/streams.py);
the pages fall back to polling the plain endpoints when a stream is unavailable.
"""

from django.conf import settings
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404
from django.utils import translation
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .. import engine, streams
from ..models import Branch, Ticket


def _ticket_payload(ticket: Ticket) -> dict:
    return engine.ticket_view(ticket).as_dict()


def _tickets():
    return Ticket.objects.select_related("service", "branch", "counter")


@never_cache
@require_GET
def ticket_status(request, token):
    return JsonResponse(_ticket_payload(get_object_or_404(_tickets(), token=token)))


@never_cache
@require_GET
def board(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    return JsonResponse(engine.board(branch))


def _require_streams():
    if not settings.GILIRAN_SSE:
        raise Http404("live streams are disabled")


@require_GET
def ticket_stream(request, token):
    """Live ticket status, only for the holder of the secret ticket token."""
    _require_streams()
    ticket = get_object_or_404(Ticket.objects.only("id", "branch_id", "day"), token=token)
    # The generator runs after the view returns, so keep the request's language.
    lang = translation.get_language()

    def compute():
        with translation.override(lang):
            fresh = _tickets().filter(pk=ticket.pk).first()
            return _ticket_payload(fresh) if fresh else None

    return streams.sse_response(
        streams.event_stream(
            compute,
            lambda: streams.branch_marker(ticket.branch_id, ticket.day),
            finished=lambda d: d["status"] not in Ticket.OPEN_STATUSES,
        )
    )


@require_GET
def board_stream(request, slug):
    """Live display board: the same public fields as the board JSON (codes, counters)."""
    _require_streams()
    branch = get_object_or_404(Branch, slug=slug)
    lang = translation.get_language()

    def compute():
        with translation.override(lang):
            return engine.board(branch)

    return streams.sse_response(
        streams.event_stream(compute, lambda: streams.branch_marker(branch.pk, engine.local_day()))
    )
