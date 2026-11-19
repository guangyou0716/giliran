"""Read-only JSON endpoints used by the live pages (and usable by other clients)."""

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .. import engine
from ..models import Branch, Ticket


@never_cache
@require_GET
def ticket_status(request, token):
    t = get_object_or_404(
        Ticket.objects.select_related("service", "branch", "counter"), token=token
    )
    return JsonResponse(engine.ticket_view(t).as_dict())


@never_cache
@require_GET
def board(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    return JsonResponse(engine.board(branch))
