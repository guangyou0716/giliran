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

Appointments
------------
* A ``Slot`` has ``capacity`` seats. ``book_slot`` locks the slot row
  (``SELECT ... FOR UPDATE``), checks the seats, and increments ``booked_count``
  with a conditional ``UPDATE ... WHERE booked_count < capacity``. The check
  constraint ``booked_count <= capacity`` is the last line of defence: the
  database rejects any write that would overbook, whatever the code does.
* Booking statuses::

      booked --check_in--> checked_in (creates a WAITING ticket)
      booked --cancel--> cancelled (frees the seat)
      booked --(not checked in by start + grace)--> no_show
      no_show --staff check_in, same day--> checked_in (ordered like a walk-in)

* Fair interleaving (``waiting_order``). Every waiting ticket gets a rank:

  0. priority tickets (elderly, disabled, pregnant, urgent);
  1. booked tickets whose slot time has come (checked in no later than
     ``start + GILIRAN_BOOKING_GRACE_MIN``);
  2. everyone else.

  Inside a rank tickets go by queue time (slot start for on-time bookings, join
  time for walk-ins), then by id. A booked customer who arrives early waits as
  if they had joined at their slot time, so they never pass walk-ins who were
  already there; from the slot time on they are called before regular walk-ins.
  A late check-in (after the grace window) is queued like a walk-in from the
  moment of check-in. Managers keep walk-ins moving by giving slots fewer seats
  than the counters can serve.
"""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Case, F, IntegerField, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone
from django.utils.translation import gettext as _

from .models import (
    Booking,
    Branch,
    Counter,
    DailySequence,
    Service,
    Slot,
    Ticket,
    TicketEvent,
)

S = Ticket.Status


class QueueError(Exception):
    """A queue operation that cannot be done in the current state."""


class InvalidTransition(QueueError):
    pass


class ServiceUnavailable(QueueError):
    pass


class CounterBusy(QueueError):
    pass


class CounterPaused(QueueError):
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


def waiting_order(queryset, now: datetime | None = None):
    """Order waiting tickets in the order they will be called (see module docstring)."""
    now = now or timezone.now()
    return queryset.annotate(
        queue_rank=Case(
            When(priority=True, then=Value(0)),
            When(appointment_at__lte=now, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        ),
        queue_at=Coalesce("appointment_at", "created_at"),
    ).order_by("queue_rank", "queue_at", "id")


def _log(
    ticket: Ticket, action: str, from_status: str, actor, counter, at: datetime, note: str = ""
) -> None:
    TicketEvent.objects.create(
        note=note,
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
    appointment_at: datetime | None = None,
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
        appointment_at=appointment_at,
    )
    _log(ticket, "issue", "", actor, None, now)
    return ticket


def active_ticket(counter: Counter) -> Ticket | None:
    return (
        Ticket.objects.filter(counter=counter, status__in=Ticket.ACTIVE_STATUSES)
        .select_related("service")
        .first()
    )


def waiting_for_counter(counter: Counter, *, now: datetime | None = None):
    now = now or timezone.now()
    tickets = Ticket.objects.filter(
        branch_id=counter.branch_id,
        service__in=counter.services.all(),
        status=S.WAITING,
        day=local_day(now),
    ).select_related("service")
    return waiting_order(tickets, now)


def call_next(counter: Counter, *, actor=None, now: datetime | None = None) -> Ticket | None:
    """Call the next waiting ticket to ``counter``. Returns None when nobody is waiting."""
    now = now or timezone.now()
    with transaction.atomic():
        # Serialise "Call next" presses at the same counter.
        counter = Counter.objects.select_for_update().get(pk=counter.pk)
        if not counter.is_active:
            raise ServiceUnavailable(_("This counter is not active."))
        if counter.is_paused:
            raise CounterPaused(_("This counter is paused. Resume it to call the next ticket."))
        if active_ticket(counter) is not None:
            raise CounterBusy(_("Finish or mark the current ticket as no-show first."))
        ticket = (
            waiting_for_counter(counter, now=now)
            .select_for_update(skip_locked=True, of=("self",))
            .first()
        )
        if ticket is None:
            return None
        ticket.status = S.CALLED
        ticket.counter = counter
        ticket.called_at = now
        ticket.first_called_at = now
        ticket.save(update_fields=["status", "counter", "called_at", "first_called_at"])
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


def set_paused(counter: Counter, paused: bool) -> Counter:
    """Pause (break) or resume a counter. A counter holding a ticket cannot pause.

    A paused counter is not offered the next ticket (``call_next``), is shown as paused on
    the console and the display board, and does not count in wait estimates.
    """
    with transaction.atomic():
        counter = Counter.objects.select_for_update().get(pk=counter.pk)
        if paused and active_ticket(counter) is not None:
            raise CounterBusy(_("Finish or mark the current ticket as no-show before pausing."))
        if counter.is_paused != paused:
            counter.is_paused = paused
            counter.save(update_fields=["is_paused"])
        return counter


TRANSFERABLE = (S.WAITING, S.CALLED, S.SERVING)


def transfer_ticket(
    ticket: Ticket,
    target: Service,
    *,
    actor=None,
    now: datetime | None = None,
) -> Ticket:
    """Move a ticket of today to another service of the same branch (staff action).

    The ticket goes back to WAITING in the target queue with a new number and code from the
    target's daily sequence (numbers stay unique per service and day), the counter it held
    is freed. Its queue time (``created_at``), priority and appointment time are kept, so it
    keeps its place in line by arrival time: it does not go to the back and does not jump
    ahead of people who arrived earlier. Calls and service times of the earlier service are
    cleared (``first_called_at`` is set again when the target service calls it), so waits in
    reports run from arrival to the final call. Recorded as a ``transfer`` TicketEvent
    (actor, old counter, note "A-003 -> P-004").
    """
    now = now or timezone.now()
    with transaction.atomic():
        ticket = Ticket.objects.select_for_update().select_related("service").get(pk=ticket.pk)
        if ticket.status not in TRANSFERABLE:
            raise InvalidTransition(
                _("Ticket %(code)s is %(status)s; it cannot be transferred.")
                % {"code": ticket.code, "status": ticket.get_status_display()}
            )
        if ticket.day != local_day(now):
            raise InvalidTransition(_("Only tickets of today can be transferred."))
        if target.branch_id != ticket.branch_id:
            raise ServiceUnavailable(_("Choose a service of this branch."))
        if target.pk == ticket.service_id:
            raise ServiceUnavailable(_("The ticket is already in this service."))
        if not target.is_active:
            raise ServiceUnavailable(_("This service is not taking tickets right now."))
        old_code, old_status, old_counter = ticket.code, ticket.status, ticket.counter
        DailySequence.objects.get_or_create(service=target, day=ticket.day)
        seq = DailySequence.objects.select_for_update().get(service=target, day=ticket.day)
        seq.last_number += 1
        seq.save(update_fields=["last_number"])
        ticket.service = target
        ticket.number = seq.last_number
        ticket.code = format_code(target.prefix, seq.last_number)
        ticket.status = S.WAITING
        ticket.counter = None
        ticket.recall_count = 0
        ticket.called_at = ticket.first_called_at = ticket.serving_at = None
        ticket.save(
            update_fields=[
                "service",
                "number",
                "code",
                "status",
                "counter",
                "recall_count",
                "called_at",
                "first_called_at",
                "serving_at",
            ]
        )
        _log(
            ticket, "transfer", old_status, actor, old_counter, now, f"{old_code} -> {ticket.code}"
        )
        return ticket


def people_ahead(ticket: Ticket, *, now: datetime | None = None) -> int | None:
    """Waiting tickets of the same service that will be called before this one.

    Counters that also serve other services may call those in between, so for
    shared counters this is a lower bound (the estimate says "~").
    """
    if ticket.status != S.WAITING:
        return None
    same_queue = Ticket.objects.filter(
        service_id=ticket.service_id, day=ticket.day, status=S.WAITING
    )
    order = list(waiting_order(same_queue, now).values_list("id", flat=True))
    # One service's waiting list for one day is small; the index is the position.
    return order.index(ticket.id) if ticket.id in order else None


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
            "appointment_at": t.appointment_at.isoformat() if t.appointment_at else None,
        }


def ticket_view(ticket: Ticket, *, now: datetime | None = None) -> TicketView:
    ahead = people_ahead(ticket, now=now)
    eta = None
    if ahead is not None:
        counters = ticket.service.counters.filter(is_active=True, is_paused=False).count()
        eta = estimate_wait_minutes(ahead, ticket.service.avg_minutes, counters)
    return TicketView(ticket, ahead, eta)


def board(branch: Branch, *, now: datetime | None = None, next_limit: int = 6) -> dict:
    """Data for the waiting-room display."""
    day = local_day(now)
    today = Ticket.objects.filter(branch=branch, day=day).select_related("service", "counter")
    serving = today.filter(status__in=Ticket.ACTIVE_STATUSES).order_by("-called_at", "-id")
    waiting = waiting_order(today.filter(status=S.WAITING), now)
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
        "paused": [c.name for c in branch.counters.filter(is_active=True, is_paused=True)],
        "next": [t.code for t in waiting[:next_limit]],
        "services": services,
        "waiting_total": waiting.count(),
    }


# --- Appointments -------------------------------------------------------------------------


class BookingError(QueueError):
    """A booking operation that cannot be done (slot full, window closed, ...)."""


class SlotFull(BookingError):
    pass


B = Booking.Status
REF_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I


def grace() -> timedelta:
    return timedelta(minutes=settings.GILIRAN_BOOKING_GRACE_MIN)


def checkin_opens(slot: Slot) -> datetime:
    return slot.starts_at - timedelta(minutes=settings.GILIRAN_CHECKIN_EARLY_MIN)


def checkin_closes(slot: Slot) -> datetime:
    """Last moment for an on-time check-in (and for booking the slot)."""
    return slot.starts_at + grace()


def normalize_phone(raw: str) -> str:
    """Keep digits and a leading +: "000-000 0101" -> "0000000101"."""
    raw = raw.strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    return ("+" + digits) if raw.startswith("+") else digits


def bookable_slots(service: Service, day: date, *, now: datetime | None = None):
    """Open slots of ``service`` on local ``day`` that can still be booked, with seats left."""
    now = now or timezone.now()
    return (
        Slot.objects.filter(
            service=service,
            is_active=True,
            starts_at__date=day,
            starts_at__gt=now - grace(),
            booked_count__lt=F("capacity"),
        )
        .select_related("service")
        .order_by("starts_at")
    )


def _new_ref() -> str:
    return "".join(secrets.choice(REF_ALPHABET) for _ in range(6))


def book_slot(slot: Slot, *, nickname: str, phone: str, now: datetime | None = None) -> Booking:
    """Take one seat in ``slot``. Raises SlotFull / BookingError; never overbooks."""
    now = now or timezone.now()
    phone = normalize_phone(phone)
    with transaction.atomic():
        # Serialise bookings of this slot: concurrent requests queue on this row lock.
        slot = (
            Slot.objects.select_for_update(of=("self",)).select_related("service").get(pk=slot.pk)
        )
        if not slot.is_active or not slot.service.is_active:
            raise BookingError(_("This time is not open for booking."))
        if now > checkin_closes(slot):
            raise BookingError(_("This time has already passed."))
        if Booking.objects.filter(
            slot=slot, phone=phone, status__in=[B.BOOKED, B.CHECKED_IN]
        ).exists():
            raise BookingError(_("This phone number already has a booking at this time."))
        # Conditional increment: a no-op when the slot is full, even without the lock.
        taken = Slot.objects.filter(pk=slot.pk, booked_count__lt=F("capacity")).update(
            booked_count=F("booked_count") + 1
        )
        if not taken:
            raise SlotFull(_("Sorry, this time was just fully booked. Please pick another."))
        for _attempt in range(5):
            try:
                with transaction.atomic():
                    return Booking.objects.create(
                        slot=slot,
                        ref=_new_ref(),
                        nickname=nickname.strip()[:40],
                        phone=phone,
                        created_at=now,
                    )
            except IntegrityError:  # pragma: no cover - 32^6 refs; retry on a clash
                continue
        raise BookingError(_("Could not create the booking. Please try again."))


def cancel_booking(booking: Booking, *, now: datetime | None = None) -> Booking:
    """Customer cancels a booking that is not checked in yet; the seat is freed."""
    now = now or timezone.now()
    with transaction.atomic():
        slot = Slot.objects.select_for_update().get(pk=booking.slot_id)
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
        if booking.status != B.BOOKED or now > checkin_closes(slot):
            raise BookingError(_("This booking can no longer be cancelled."))
        booking.status = B.CANCELLED
        booking.cancelled_at = now
        booking.save(update_fields=["status", "cancelled_at"])
        Slot.objects.filter(pk=slot.pk).update(booked_count=F("booked_count") - 1)
        booking.slot = Slot.objects.get(pk=slot.pk)
        return booking


def expire_bookings(*, now: datetime | None = None, branch: Branch | None = None) -> int:
    """Mark bookings not checked in by slot start + grace as no-show. Returns how many."""
    now = now or timezone.now()
    late = Booking.objects.filter(status=B.BOOKED, slot__starts_at__lt=now - grace())
    if branch is not None:
        late = late.filter(slot__service__branch=branch)
    return late.update(status=B.NO_SHOW)


def purge_phones(days: int | None = None, *, now: datetime | None = None) -> int:
    """Blank the phone number of bookings whose slot started more than ``days`` days ago.

    ``days`` defaults to ``GILIRAN_PHONE_RETENTION_DAYS``. Idempotent: rows that are already
    blank are not touched, so a second run returns 0. Only the phone is removed; the reference,
    nickname, status and times stay for reports. Tickets store no phone number at all (a
    customer joining the queue gives a nickname at most), so there is nothing to purge there.
    Returns how many bookings were changed.
    """
    days = settings.GILIRAN_PHONE_RETENTION_DAYS if days is None else days
    if days < 1:
        raise ValueError("retention must be at least 1 day")
    now = now or timezone.now()
    old = Booking.objects.filter(slot__starts_at__lt=now - timedelta(days=days)).exclude(phone="")
    return old.update(phone="")


def check_in(
    booking: Booking, *, by_staff: bool = False, actor=None, now: datetime | None = None
) -> Ticket:
    """Turn a booking into a WAITING ticket.

    Customers (via their link) can check in from ``GILIRAN_CHECKIN_EARLY_MIN`` before
    the slot until start + grace. Staff can check in any booked or no-show booking on
    the slot's day; after the grace window the ticket is ordered like a walk-in.
    """
    now = now or timezone.now()
    with transaction.atomic():
        booking = (
            Booking.objects.select_for_update(of=("self",))
            .select_related("slot__service__branch")
            .get(pk=booking.pk)
        )
        slot = booking.slot
        on_time = now <= checkin_closes(slot)
        if booking.status not in (B.BOOKED, B.NO_SHOW):
            raise BookingError(_("This booking cannot be checked in."))
        if not by_staff and (booking.status == B.NO_SHOW or not on_time):
            raise BookingError(
                _("The check-in time for this booking has passed. Please ask at the counter.")
            )
        if local_day(now) != local_day(slot.starts_at):
            raise BookingError(_("Bookings can only be checked in on the day of the appointment."))
        if not by_staff and now < checkin_opens(slot):
            raise BookingError(
                _("Check-in opens at %(time)s.")
                % {"time": timezone.localtime(checkin_opens(slot)).strftime("%H:%M")}
            )
        ticket = issue_ticket(
            slot.service,
            nickname=booking.nickname,
            source=Ticket.Source.BOOKING,
            actor=actor,
            now=now,
            appointment_at=slot.starts_at if on_time else None,
        )
        booking.status = B.CHECKED_IN
        booking.checked_in_at = now
        booking.ticket = ticket
        booking.save(update_fields=["status", "checked_in_at", "ticket"])
        return ticket
