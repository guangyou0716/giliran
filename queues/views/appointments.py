"""Appointments: customer booking pages, staff check-in list, manager slot pages."""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Prefetch, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from .. import engine
from ..forms import (
    AppointmentFilterForm,
    BookingForm,
    BookingSearchForm,
    SlotForm,
    SlotGenerateForm,
)
from ..models import Booking, Branch, Slot
from ..permissions import require_manager, require_staff
from ..ratelimit import rate_limited

# --- Customer pages (no account) ------------------------------------------------------------


def booking_days(now=None):
    today = timezone.localdate(now)
    return [today + timedelta(days=i) for i in range(max(settings.GILIRAN_BOOKING_DAYS, 1))]


@rate_limited("book")
def book(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    now = timezone.now()
    search = BookingSearchForm(request.GET or None, branch=branch, days=booking_days(now))
    form = None
    slots = None
    if search.is_valid():
        slots = engine.bookable_slots(
            search.cleaned_data["service"], search.cleaned_data["day"], now=now
        )
        form = BookingForm(request.POST or None, slots=slots)
        if request.method == "POST" and form.is_valid():
            try:
                booking = engine.book_slot(
                    form.cleaned_data["slot"],
                    nickname=form.cleaned_data["nickname"],
                    phone=form.cleaned_data["phone"],
                )
            except engine.BookingError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, _("Booked. Keep this page: it is your booking link."))
                return redirect("booking", token=booking.token)
    elif request.method == "POST":
        messages.error(request, _("Choose a service and a day first."))
    return render(
        request,
        "queues/book.html",
        {
            "branch": branch,
            "search": search,
            "form": form,
            "slots": list(slots) if slots is not None else None,
        },
    )


def _booking_state(booking: Booking, now) -> dict:
    slot = booking.slot
    booked = booking.status == Booking.Status.BOOKED
    open_from = engine.checkin_opens(slot)
    closes = engine.checkin_closes(slot)
    return {
        "can_check_in": booked and open_from <= now <= closes,
        "check_in_later": booked and now < open_from,
        "check_in_opens": open_from,
        "missed": (booked and now > closes) or booking.status == Booking.Status.NO_SHOW,
        "can_cancel": booked and now <= closes,
    }


def booking(request, token):
    b = get_object_or_404(
        Booking.objects.select_related("slot__service__branch", "ticket"), token=token
    )
    return render(
        request,
        "queues/booking.html",
        {"b": b, **_booking_state(b, timezone.now())},
    )


@require_POST
def booking_cancel(request, token):
    b = get_object_or_404(Booking, token=token)
    try:
        engine.cancel_booking(b)
    except engine.BookingError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, _("Your booking was cancelled."))
    return redirect("booking", token=token)


@require_POST
def booking_check_in(request, token):
    b = get_object_or_404(Booking, token=token)
    try:
        ticket = engine.check_in(b)
    except engine.QueueError as exc:
        messages.error(request, str(exc))
        return redirect("booking", token=token)
    messages.success(request, _("You are checked in. This is your queue number."))
    return redirect("ticket", token=ticket.token)


# --- Staff: today's bookings and check-in ---------------------------------------------------


@login_required
def appointments(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    role = require_staff(request.user, branch)
    # Lazy no-show marking (the expire_bookings command does the same from cron).
    engine.expire_bookings(branch=branch)
    form = AppointmentFilterForm(request.GET or None)
    day = timezone.localdate()
    q = ""
    if form.is_valid():
        day = form.cleaned_data["day"] or day
        q = form.cleaned_data["q"].strip()
    bookings = Booking.objects.exclude(status=Booking.Status.CANCELLED).select_related("ticket")
    if q:
        match = Q(ref__iexact=q) | Q(nickname__icontains=q)
        if q.isdigit() and len(q) >= 4:
            match |= Q(phone__endswith=q)
        bookings = bookings.filter(match)
    slots = (
        Slot.objects.filter(service__branch=branch, starts_at__date=day)
        .select_related("service")
        .prefetch_related(Prefetch("bookings", queryset=bookings, to_attr="listed"))
        .order_by("starts_at", "service__prefix")
    )
    rows = [s for s in slots if s.listed or not q]
    free_slots = [s for s in rows if not s.listed and s.booked_count == 0]
    return render(
        request,
        "queues/staff/appointments.html",
        {
            "branch": branch,
            "role": role,
            "form": form,
            "day": day,
            "is_today": day == timezone.localdate(),
            "rows": rows,
            "has_bookings": any(s.listed for s in rows),
            "free_slots": free_slots,
            "q": q,
            "GRACE": settings.GILIRAN_BOOKING_GRACE_MIN,
        },
    )


@login_required
@require_POST
def staff_check_in(request, pk):
    b = get_object_or_404(Booking.objects.select_related("slot__service__branch"), pk=pk)
    branch = b.slot.service.branch
    require_staff(request.user, branch)
    try:
        ticket = engine.check_in(b, by_staff=True, actor=request.user)
    except engine.QueueError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(
            request,
            _("%(ref)s checked in: ticket %(code)s.") % {"ref": b.ref, "code": ticket.code},
        )
    day = timezone.localdate(b.slot.starts_at).isoformat()
    return redirect(reverse("staff:appointments", args=[branch.slug]) + f"?day={day}")


# --- Manager: slots -------------------------------------------------------------------------


def _slots_url(branch, day):
    return reverse("manage:slots", args=[branch.slug]) + f"?day={day.isoformat()}"


@login_required
def slots(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    filter_form = AppointmentFilterForm(request.GET or None)
    day = timezone.localdate()
    if filter_form.is_valid() and filter_form.cleaned_data["day"]:
        day = filter_form.cleaned_data["day"]
    generate = SlotGenerateForm(branch=branch, initial={"day": day})
    return _render_slots(request, branch, day, filter_form, generate)


def _render_slots(request, branch, day, filter_form, generate):
    day_slots = (
        Slot.objects.filter(service__branch=branch, starts_at__date=day)
        .select_related("service")
        .order_by("starts_at", "service__prefix")
    )
    return render(
        request,
        "queues/manage/slots.html",
        {
            "branch": branch,
            "day": day,
            "filter_form": filter_form,
            "generate": generate,
            "slots": day_slots,
        },
    )


@login_required
@require_POST
def slot_generate(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    generate = SlotGenerateForm(request.POST, branch=branch)
    if not generate.is_valid():
        day = generate.cleaned_data.get("day") or timezone.localdate()
        return _render_slots(request, branch, day, AppointmentFilterForm(), generate)
    data = generate.cleaned_data
    created = skipped = 0
    with transaction.atomic():
        for start, end in generate.slot_times():
            _slot, made = Slot.objects.get_or_create(
                service=data["service"],
                starts_at=start,
                defaults={"ends_at": end, "capacity": data["capacity"]},
            )
            created += made
            skipped += not made
    messages.success(
        request,
        _("%(created)s slots created, %(skipped)s already existed.")
        % {"created": created, "skipped": skipped},
    )
    return redirect(_slots_url(branch, data["day"]))


@login_required
def slot_edit(request, slug, pk):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    slot = get_object_or_404(Slot.objects.select_related("service"), pk=pk, service__branch=branch)
    day = timezone.localdate(slot.starts_at)
    if request.method == "POST":
        with transaction.atomic():
            # Re-read under lock so a booking cannot slip in between the check and the save.
            locked = Slot.objects.select_for_update().get(pk=slot.pk)
            form = SlotForm(request.POST, instance=locked)
            if form.is_valid():
                form.save()
                messages.success(request, _("Saved."))
                return redirect(_slots_url(branch, day))
    else:
        form = SlotForm(instance=slot)
    return render(
        request,
        "queues/manage/slot_form.html",
        {
            "branch": branch,
            "slot": slot,
            "form": form,
            "day": day,
            "has_bookings": slot.bookings.exists(),
        },
    )


@login_required
@require_POST
def slot_delete(request, slug, pk):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    slot = get_object_or_404(Slot, pk=pk, service__branch=branch)
    day = timezone.localdate(slot.starts_at)
    with transaction.atomic():
        slot = Slot.objects.select_for_update().get(pk=slot.pk)
        if slot.bookings.exists():
            messages.error(
                request, _("This slot has bookings. Close it for booking instead of deleting it.")
            )
        else:
            slot.delete()
            messages.success(request, _("Slot deleted."))
    return redirect(_slots_url(branch, day))
