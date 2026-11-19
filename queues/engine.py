"""Queue engine: every ticket state change goes through here.

Rules
-----
* Ticket numbers restart at 1 per service per local (Asia/Kuala_Lumpur) day. The
  per-day ``DailySequence`` row is locked while a number is taken, so two
  customers joining at the same moment never get the same number.
* ``call_next`` picks, among today's WAITING tickets of the counter's services,
  priority tickets first, then the lowest id (first come, first served). On
  PostgreSQL the candidate row is locked with ``SKIP LOCKED`` so two counters
  pressing "Call next" together get different tickets.
* A counter holds at most one CALLED/SERVING ticket (also a DB constraint).
* Allowed transitions::

      waiting --call--> called --start--> serving --done--> done
      called  --recall--> called      called --done--> done
      called  --no_show--> no_show    waiting --cancel--> cancelled
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import Branch, Counter, DailySequence, Service, Ticket, TicketEvent

S = Ticket.Status


class QueueError(Exception):
    """A queue operation that cannot be done in the current state."""


class InvalidTransition(QueueError):
    pass


class ServiceUnavailable(QueueError):
    pass


class CounterBusy(QueueError):
    pass


# action -> (allowed from statuses, to status)
TRANSITIONS: dict[str, tuple[tuple[str, ...], str]] = {
    "call": ((S.WAITING,), S.CALLED),
    "recall": ((S.CALLED,), S.CALLED),
    "start": ((S.CALLED,), S.SERVING),
    "done": ((S.CALLED, S.SERVING), S.DONE),
    "no_show": ((S.CALLED,), S.NO_SHOW),
    "cancel": ((S.WAITING,), S.CANCELLED),
}

STAFF_ACTIONS = ("recall", "start", "done", "no_show")


def local_day(now: datetime | None = None) -> date:
    return timezone.localdate(now or timezone.now())


def queue_order():
    """Order in which waiting tickets are called."""
    return ("-priority", "id")


def _log(ticket: Ticket, action: str, from_status: str, actor, counter, at: datetime) -> None:
    TicketEvent.objects.create(
        ticket=ticket,
        action=action,
        from_status=from_status,
        to_status=ticket.status,
        counter=counter,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        at=at,
    )


def format_code(prefix: str, number: int) -> str:
    return f"{prefix}-{number:03d}"


@transaction.atomic
def issue_ticket(
    service: Service,
    *,
    nickname: str = "",
    priority: bool = False,
    source: str = Ticket.Source.SELF,
    actor=None,
    now: datetime | None = None,
) -> Ticket:
    """Take the next number for ``service`` today and create a WAITING ticket."""
    now = now or timezone.now()
    branch = service.branch
    if not service.is_active:
        raise ServiceUnavailable(_("This service is not taking tickets right now."))
    if source == Ticket.Source.SELF and not branch.is_open:
        raise ServiceUnavailable(_("This branch is not taking new tickets right now."))
    day = local_day(now)
    DailySequence.objects.get_or_create(service=service, day=day)
    seq = DailySequence.objects.select_for_update().get(service=service, day=day)
    seq.last_number += 1
    seq.save(update_fields=["last_number"])
    ticket = Ticket.objects.create(
        branch=branch,
        service=service,
        day=day,
        number=seq.last_number,
        code=format_code(service.prefix, seq.last_number),
        nickname=nickname.strip()[:40],
        priority=priority,
        source=source,
        created_at=now,
    )
    _log(ticket, "issue", "", actor, None, now)
    return ticket


def active_ticket(counter: Counter) -> Ticket | None:
    return (
        Ticket.objects.filter(counter=counter, status__in=Ticket.ACTIVE_STATUSES)
        .select_related("service")
        .first()
    )


def waiting_for_counter(counter: Counter, *, day: date | None = None):
    day = day or local_day()
    return (
        Ticket.objects.filter(
            branch_id=counter.branch_id,
            service__in=counter.services.all(),
            status=S.WAITING,
            day=day,
        )
        .select_related("service")
        .order_by(*queue_order())
    )


def call_next(counter: Counter, *, actor=None, now: datetime | None = None) -> Ticket | None:
    """Call the next waiting ticket to ``counter``. Returns None when nobody is waiting."""
    now = now or timezone.now()
    with transaction.atomic():
        # Serialise "Call next" presses at the same counter.
        counter = Counter.objects.select_for_update().get(pk=counter.pk)
        if not counter.is_active:
            raise ServiceUnavailable(_("This counter is not active."))
        if active_ticket(counter) is not None:
            raise CounterBusy(_("Finish or mark the current ticket as no-show first."))
        ticket = (
            waiting_for_counter(counter, day=local_day(now))
            .select_for_update(skip_locked=True, of=("self",))
            .first()
        )
        if ticket is None:
            return None
        ticket.status = S.CALLED
        ticket.counter = counter
        ticket.called_at = now
        ticket.save(update_fields=["status", "counter", "called_at"])
        _log(ticket, "call", S.WAITING, actor, counter, now)
        return ticket


def apply_action(ticket: Ticket, action: str, *, actor=None, now: datetime | None = None) -> Ticket:
    """Apply a non-call transition (recall, start, done, no_show, cancel)."""
    if action not in TRANSITIONS or action == "call":
        raise InvalidTransition(_("Unknown action."))
    now = now or timezone.now()
    allowed_from, to_status = TRANSITIONS[action]
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().get(pk=ticket.pk)
        if ticket.status not in allowed_from:
            raise InvalidTransition(
                _("Ticket %(code)s is %(status)s; cannot %(action)s.")
                % {"code": ticket.code, "status": ticket.get_status_display(), "action": action}
            )
        from_status = ticket.status
        ticket.status = to_status
        fields = ["status"]
        if action == "recall":
            ticket.recall_count += 1
            ticket.called_at = now
            fields += ["recall_count", "called_at"]
        elif action == "start":
            ticket.serving_at = now
            fields.append("serving_at")
        elif action in ("done", "no_show", "cancel"):
            ticket.finished_at = now
            fields.append("finished_at")
        ticket.save(update_fields=fields)
        _log(ticket, action, from_status, actor, ticket.counter, now)
        return ticket


def people_ahead(ticket: Ticket) -> int | None:
    """Waiting tickets of the same service that will be called before this one.

    Counters that also serve other services may call those in between, so for
    shared counters this is a lower bound (the estimate says "~").
    """
    if ticket.status != S.WAITING:
        return None
    return (
        Ticket.objects.filter(service_id=ticket.service_id, day=ticket.day, status=S.WAITING)
        .filter(
            Q(priority=True, id__lt=ticket.id)
            if ticket.priority
            else Q(priority=True) | Q(priority=False, id__lt=ticket.id)
        )
        .count()
    )


def estimate_wait_minutes(ahead: int, avg_minutes: int, counters: int) -> int | None:
    """Rough wait: people ahead x average minutes, shared by the counters serving it.

    Returns None when no active counter serves the service (no estimate possible).
    """
    if counters <= 0:
        return None
    return math.ceil(ahead * avg_minutes / counters)


@dataclass(frozen=True)
class TicketView:
    ticket: Ticket
    ahead: int | None
    eta_minutes: int | None

    @property
    def eta_text(self) -> str:
        """Human text for the estimate, same wording as the live page's JS."""
        if self.eta_minutes is None:
            return "—"
        if self.eta_minutes == 0:
            return _("You are next")
        return _("~{n} min").replace("{n}", str(self.eta_minutes))

    def as_dict(self) -> dict:
        t = self.ticket
        return {
            "code": t.code,
            "status": t.status,
            "status_label": str(t.get_status_display()),
            "service": t.service.name,
            "branch": t.branch.name,
            "priority": t.priority,
            "ahead": self.ahead,
            "eta_minutes": self.eta_minutes,
            "counter": t.counter.name if t.counter_id else None,
            "recall_count": t.recall_count,
            "called_at": t.called_at.isoformat() if t.called_at else None,
        }


def ticket_view(ticket: Ticket) -> TicketView:
    ahead = people_ahead(ticket)
    eta = None
    if ahead is not None:
        counters = ticket.service.counters.filter(is_active=True).count()
        eta = estimate_wait_minutes(ahead, ticket.service.avg_minutes, counters)
    return TicketView(ticket, ahead, eta)


def board(branch: Branch, *, now: datetime | None = None, next_limit: int = 6) -> dict:
    """Data for the waiting-room display."""
    day = local_day(now)
    today = Ticket.objects.filter(branch=branch, day=day).select_related("service", "counter")
    serving = today.filter(status__in=Ticket.ACTIVE_STATUSES).order_by("-called_at", "-id")
    waiting = today.filter(status=S.WAITING).order_by(*queue_order())
    services = []
    for service in branch.services.filter(is_active=True):
        services.append(
            {
                "name": service.name,
                "prefix": service.prefix,
                "waiting": waiting.filter(service=service).count(),
            }
        )
    return {
        "branch": branch.name,
        "serving": [
            {
                "code": t.code,
                "counter": t.counter.name if t.counter_id else "",
                "status": t.status,
                "status_label": str(t.get_status_display()),
                "recall_count": t.recall_count,
            }
            for t in serving
        ],
        "next": [t.code for t in waiting[:next_limit]],
        "services": services,
        "waiting_total": waiting.count(),
    }
