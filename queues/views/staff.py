"""Counter staff pages (login + branch membership required)."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .. import engine
from ..forms import TicketFilterForm, WalkInForm
from ..models import Branch, Counter, Ticket
from ..permissions import MANAGER, branches_for, require_staff, role_at


@login_required
def dashboard(request):
    branches = branches_for(request.user).prefetch_related("counters__services")
    rows = []
    for branch in branches:
        rows.append(
            {
                "branch": branch,
                "role": role_at(request.user, branch),
                "board": engine.board(branch),
            }
        )
    return render(request, "queues/staff/dashboard.html", {"rows": rows, "MANAGER": MANAGER})


@login_required
def counter_console(request, pk):
    counter = get_object_or_404(Counter.objects.select_related("branch"), pk=pk)
    role = require_staff(request.user, counter.branch)
    current = engine.active_ticket(counter)
    waiting = list(engine.waiting_for_counter(counter)[:20])
    return render(
        request,
        "queues/staff/counter.html",
        {
            "counter": counter,
            "branch": counter.branch,
            "role": role,
            "current": current,
            "waiting": waiting,
            "walkin_form": WalkInForm(branch=counter.branch),
        },
    )


@login_required
@require_POST
def counter_action(request, pk):
    counter = get_object_or_404(Counter.objects.select_related("branch"), pk=pk)
    require_staff(request.user, counter.branch)
    action = request.POST.get("action", "")
    try:
        if action == "call_next":
            ticket = engine.call_next(counter, actor=request.user)
            if ticket is None:
                messages.info(request, _("Nobody is waiting."))
            else:
                messages.success(request, _("Called %(code)s.") % {"code": ticket.code})
        elif action in engine.STAFF_ACTIONS:
            current = engine.active_ticket(counter)
            if current is None:
                messages.error(request, _("No ticket at this counter."))
            else:
                engine.apply_action(current, action, actor=request.user)
        else:
            messages.error(request, _("Unknown action."))
    except engine.QueueError as exc:
        messages.error(request, str(exc))
    return redirect("staff:counter", pk=counter.pk)


@login_required
@require_POST
def walk_in(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    require_staff(request.user, branch)
    form = WalkInForm(request.POST, branch=branch)
    if form.is_valid():
        try:
            ticket = engine.issue_ticket(
                form.cleaned_data["service"],
                nickname=form.cleaned_data["nickname"],
                priority=form.cleaned_data["priority"],
                source=Ticket.Source.DESK,
                actor=request.user,
            )
        except engine.QueueError as exc:
            messages.error(request, str(exc))
        else:
            messages.success(request, _("Ticket %(code)s added.") % {"code": ticket.code})
    else:
        messages.error(request, _("Choose a service for the walk-in."))
    back = request.POST.get("next", "")
    if back.startswith("/staff/") and url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return redirect(back)
    return redirect("staff:dashboard")


@login_required
def ticket_history(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    role = require_staff(request.user, branch)
    form = TicketFilterForm(request.GET or None, branch=branch)
    tickets = Ticket.objects.filter(branch=branch).select_related("service", "counter")
    if form.is_bound:
        form.is_valid()
        # Apply every filter that validated; an invalid one is shown as a form error.
        data = form.cleaned_data
        q = (data.get("q") or "").strip()
        if q:
            tickets = tickets.filter(Q(code__icontains=q) | Q(nickname__icontains=q))
        if data.get("status"):
            tickets = tickets.filter(status=data["status"])
        if data.get("service"):
            tickets = tickets.filter(service=data["service"])
        if data.get("day"):
            tickets = tickets.filter(day=data["day"])
    page = Paginator(tickets.order_by("-day", "-id"), 25).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    return render(
        request,
        "queues/staff/history.html",
        {
            "branch": branch,
            "role": role,
            "form": form,
            "page": page,
            "querystring": params.urlencode(),
        },
    )
