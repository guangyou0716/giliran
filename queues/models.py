import uuid

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class Branch(models.Model):
    """A physical location (clinic, pharmacy, service centre) with its own queues."""

    name = models.CharField(_("name"), max_length=120)
    slug = models.SlugField(_("slug"), unique=True)
    is_open = models.BooleanField(
        _("open for new tickets"),
        default=True,
        help_text=_("When off, customers cannot take new tickets from their phone."),
    )

    class Meta:
        ordering = ["name"]
        verbose_name = _("branch")
        verbose_name_plural = _("branches")

    def __str__(self) -> str:
        return self.name


class Membership(models.Model):
    """Gives a user a role at one branch. Superusers act as managers everywhere."""

    class Role(models.TextChoices):
        MANAGER = "manager", _("Manager")
        STAFF = "staff", _("Counter staff")

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="memberships"
    )
    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(_("role"), max_length=16, choices=Role.choices, default=Role.STAFF)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "branch"], name="unique_membership_per_branch")
        ]
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")

    def __str__(self) -> str:
        return f"{self.user} @ {self.branch} ({self.role})"


prefix_validator = RegexValidator(r"^[A-Z]{1,3}$", _("Use 1 to 3 capital letters, e.g. A or RX."))


class Service(models.Model):
    """A queue inside a branch, e.g. Consultation (A) or Pharmacy (P)."""

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="services")
    name = models.CharField(_("name"), max_length=80)
    prefix = models.CharField(
        _("ticket prefix"),
        max_length=3,
        validators=[prefix_validator],
        help_text=_("Letters printed before the ticket number, e.g. A for A-007."),
    )
    avg_minutes = models.PositiveSmallIntegerField(
        _("average minutes per customer"),
        default=5,
        validators=[MinValueValidator(1), MaxValueValidator(240)],
    )
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["branch", "prefix"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "prefix"], name="unique_service_prefix"),
            models.UniqueConstraint(fields=["branch", "name"], name="unique_service_name"),
        ]
        verbose_name = _("service")
        verbose_name_plural = _("services")

    def __str__(self) -> str:
        return f"{self.prefix} · {self.name}"


class Counter(models.Model):
    """A desk / room that calls tickets from one or more services."""

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="counters")
    name = models.CharField(_("name"), max_length=40)
    services = models.ManyToManyField(Service, related_name="counters", verbose_name=_("services"))
    is_active = models.BooleanField(_("active"), default=True)

    class Meta:
        ordering = ["branch", "name"]
        constraints = [
            models.UniqueConstraint(fields=["branch", "name"], name="unique_counter_name")
        ]
        verbose_name = _("counter")
        verbose_name_plural = _("counters")

    def __str__(self) -> str:
        return self.name


class DailySequence(models.Model):
    """Last issued ticket number per service per local (MYT) day; row-locked when issuing."""

    service = models.ForeignKey(Service, on_delete=models.CASCADE, related_name="sequences")
    day = models.DateField()
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["service", "day"], name="unique_sequence_per_day")
        ]

    def __str__(self) -> str:
        return f"{self.service} {self.day}: {self.last_number}"


class Ticket(models.Model):
    class Status(models.TextChoices):
        WAITING = "waiting", _("Waiting")
        CALLED = "called", _("Called")
        SERVING = "serving", _("Being served")
        DONE = "done", _("Done")
        NO_SHOW = "no_show", _("No show")
        CANCELLED = "cancelled", _("Cancelled")

    class Source(models.TextChoices):
        SELF = "self", _("Phone (QR)")
        DESK = "desk", _("Front desk")
        BOOKING = "booking", _("Appointment")

    ACTIVE_STATUSES = (Status.CALLED, Status.SERVING)
    OPEN_STATUSES = (Status.WAITING, Status.CALLED, Status.SERVING)

    branch = models.ForeignKey(Branch, on_delete=models.CASCADE, related_name="tickets")
    service = models.ForeignKey(Service, on_delete=models.PROTECT, related_name="tickets")
    day = models.DateField(_("day"))
    number = models.PositiveIntegerField(_("number"))
    code = models.CharField(_("ticket"), max_length=12)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    nickname = models.CharField(_("nickname"), max_length=40, blank=True)
    priority = models.BooleanField(
        _("priority"),
        default=False,
        help_text=_("Elderly, disabled, pregnant or urgent: called before normal tickets."),
    )
    source = models.CharField(max_length=8, choices=Source.choices, default=Source.SELF)
    status = models.CharField(
        _("status"), max_length=12, choices=Status.choices, default=Status.WAITING
    )
    counter = models.ForeignKey(
        Counter, on_delete=models.SET_NULL, null=True, blank=True, related_name="tickets"
    )
    recall_count = models.PositiveSmallIntegerField(default=0)
    created_at = models.DateTimeField(_("created"), default=timezone.now)
    called_at = models.DateTimeField(null=True, blank=True)
    # When the ticket was first called; ``called_at`` moves on every recall, so waiting
    # time (reports) is measured to this one.
    first_called_at = models.DateTimeField(null=True, blank=True)
    serving_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    appointment_at = models.DateTimeField(
        _("appointment time"),
        null=True,
        blank=True,
    )

    class Meta:
        ordering = ["-day", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["service", "day", "number"], name="unique_ticket_number_per_day"
            ),
            # A counter works on at most one ticket at a time.
            models.UniqueConstraint(
                fields=["counter"],
                condition=Q(status__in=["called", "serving"]),
                name="one_active_ticket_per_counter",
            ),
        ]
        indexes = [
            models.Index(fields=["branch", "day", "status"], name="ticket_branch_day_status"),
            models.Index(fields=["service", "day", "status"], name="ticket_service_day_status"),
        ]
        verbose_name = _("ticket")
        verbose_name_plural = _("tickets")

    def __str__(self) -> str:
        return self.code

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES


class TicketEvent(models.Model):
    """Audit trail of every state change."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=16)
    from_status = models.CharField(max_length=12, blank=True)
    to_status = models.CharField(max_length=12)
    counter = models.ForeignKey(Counter, on_delete=models.SET_NULL, null=True, blank=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    at = models.DateTimeField()

    class Meta:
        ordering = ["at", "id"]

    def __str__(self) -> str:
        return f"{self.ticket} {self.action} {self.from_status}->{self.to_status}"


class Slot(models.Model):
    """A bookable appointment time for one service, with a fixed number of seats.

    ``booked_count`` counts bookings that hold a seat (booked, checked in or no-show).
    It only changes inside ``engine.book_slot`` / ``engine.cancel_booking`` while the
    slot row is locked, and the check constraint ``slot_not_overbooked`` makes the
    database itself refuse any write that would put more bookings than seats.
    """

    service = models.ForeignKey(
        Service, on_delete=models.CASCADE, related_name="slots", verbose_name=_("service")
    )
    starts_at = models.DateTimeField(_("starts at"))
    ends_at = models.DateTimeField(_("ends at"))
    capacity = models.PositiveSmallIntegerField(
        _("capacity"),
        default=1,
        validators=[MinValueValidator(1), MaxValueValidator(50)],
        help_text=_("How many people can book this time."),
    )
    booked_count = models.PositiveSmallIntegerField(_("booked"), default=0, editable=False)
    is_active = models.BooleanField(_("open for booking"), default=True)

    class Meta:
        ordering = ["starts_at", "service__prefix"]
        constraints = [
            models.UniqueConstraint(fields=["service", "starts_at"], name="unique_slot_start"),
            models.CheckConstraint(
                condition=Q(booked_count__lte=models.F("capacity")), name="slot_not_overbooked"
            ),
            models.CheckConstraint(condition=Q(capacity__gte=1), name="slot_capacity_positive"),
            models.CheckConstraint(
                condition=Q(ends_at__gt=models.F("starts_at")), name="slot_ends_after_start"
            ),
        ]
        verbose_name = _("appointment slot")
        verbose_name_plural = _("appointment slots")

    def __str__(self) -> str:
        start = timezone.localtime(self.starts_at)
        return f"{self.service.prefix} {start:%Y-%m-%d %H:%M}"

    @property
    def seats_left(self) -> int:
        return max(self.capacity - self.booked_count, 0)


phone_validator = RegexValidator(
    r"^\+?[0-9]{9,15}$", _("Enter a phone number with 9 to 15 digits, e.g. 01X-XXX XXXX.")
)


class Booking(models.Model):
    """A customer's appointment in a slot. Managed through a secret link (``token``)."""

    class Status(models.TextChoices):
        BOOKED = "booked", _("Booked")
        CHECKED_IN = "checked_in", _("Checked in")
        CANCELLED = "cancelled", _("Cancelled")
        NO_SHOW = "no_show", _("No show")

    slot = models.ForeignKey(
        Slot, on_delete=models.PROTECT, related_name="bookings", verbose_name=_("slot")
    )
    ref = models.CharField(_("booking reference"), max_length=8, unique=True)
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    nickname = models.CharField(_("nickname"), max_length=40)
    phone = models.CharField(_("phone"), max_length=16, validators=[phone_validator])
    status = models.CharField(
        _("status"), max_length=12, choices=Status.choices, default=Status.BOOKED
    )
    ticket = models.OneToOneField(
        Ticket,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="booking",
        verbose_name=_("ticket"),
    )
    created_at = models.DateTimeField(_("created"), default=timezone.now)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["slot__starts_at", "id"]
        constraints = [
            # One phone number cannot hold two seats in the same slot.
            models.UniqueConstraint(
                fields=["slot", "phone"],
                condition=Q(status__in=["booked", "checked_in"]),
                name="one_active_booking_per_phone_per_slot",
            ),
        ]
        verbose_name = _("booking")
        verbose_name_plural = _("bookings")

    def __str__(self) -> str:
        return self.ref

    @property
    def phone_masked(self) -> str:
        """Only the last 4 digits, for screens other people can see."""
        return "•••• " + self.phone[-4:]
