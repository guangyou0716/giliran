"""Customer-facing pages: no account needed."""

from django.conf import settings
from django.contrib import messages
from django.db import connection
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .. import engine
from ..forms import JoinForm
from ..models import Branch, Ticket
from ..ratelimit import rate_limited


def healthz(request):
    """Liveness + database check for the container healthcheck and load balancers."""
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - any database failure means "not healthy"
        return HttpResponse("database unavailable", status=503, content_type="text/plain")
    return HttpResponse("ok", content_type="text/plain")


def home(request):
    branches = Branch.objects.all()
    return render(request, "queues/home.html", {"branches": branches})


@rate_limited("join")
def join(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    form = JoinForm(request.POST or None, branch=branch)
    if request.method == "POST" and form.is_valid():
        try:
            ticket = engine.issue_ticket(
                form.cleaned_data["service"],
                nickname=form.cleaned_data["nickname"],
                source=Ticket.Source.SELF,
            )
        except engine.QueueError as exc:
            messages.error(request, str(exc))
        else:
            return redirect("ticket", token=ticket.token)
    board = engine.board(branch)
    waiting = {s["prefix"]: s["waiting"] for s in board["services"]}
    services = [(s, waiting.get(s.prefix, 0)) for s in branch.services.filter(is_active=True)]
    return render(
        request,
        "queues/join.html",
        {"branch": branch, "form": form, "services": services},
    )


def ticket(request, token):
    t = get_object_or_404(
        Ticket.objects.select_related("service", "branch", "counter"), token=token
    )
    view = engine.ticket_view(t)
    almost = settings.GILIRAN_ALMOST_AHEAD
    return render(
        request,
        "queues/ticket.html",
        {
            "t": t,
            "view": view,
            "poll_ms": settings.GILIRAN_POLL_MS,
            "sse": settings.GILIRAN_SSE,
            "almost_ahead": almost,
            "is_almost": t.status == Ticket.Status.WAITING
            and view.ahead is not None
            and view.ahead <= almost,
        },
    )


@require_POST
def ticket_cancel(request, token):
    t = get_object_or_404(Ticket, token=token)
    try:
        engine.apply_action(t, "cancel")
    except engine.QueueError:
        messages.error(request, _("This ticket can no longer be cancelled."))
    else:
        messages.success(request, _("Your ticket was cancelled."))
    return redirect("ticket", token=token)


def board(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    return render(
        request,
        "queues/board.html",
        {
            "branch": branch,
            "data": engine.board(branch),
            "poll_ms": settings.GILIRAN_POLL_MS,
            "sse": settings.GILIRAN_SSE,
        },
    )
