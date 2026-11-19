"""Manager reports: tickets, waiting time, service time and no-shows for a date range.

Definitions (all days and hours are local Asia/Kuala_Lumpur time):

* A ticket belongs to the day it was issued (``Ticket.day``) and to the hour of
  ``created_at`` (join time, or check-in time for appointments).
* **Served**: tickets that ended ``done``.
* **Wait**: ``first_called_at - created_at`` for every ticket that was called
  (whatever happened next). Recalls do not move ``first_called_at``.
* **Service time**: ``finished_at - serving_at`` for ``done`` tickets that were
  started ("Start serving"); tickets closed straight from "called" have no
  service time.
* **No-shows**: tickets marked no-show plus bookings whose customer never checked
  in (status no-show, or still "booked" after start + grace, before the expiry job
  has run). Bookings count on their slot's day and hour.
* **No-show rate** = no-shows / (tickets + booking no-shows): every expected visit
  either got a ticket or was a booking that never turned into one.
* **Left queue**: tickets the customer cancelled. **Bookings cancelled**: bookings
  cancelled ahead of time (they free the seat, so they are not no-shows).

Database work is a fixed number of queries whatever the range: one grouped
aggregate over tickets (per service and hour), one list of waiting times for the
median, and one grouped aggregate over bookings. Totals per service, per hour and
overall are rolled up from those groups in Python.
"""

from __future__ import annotations

import csv
import io
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db.models import Count, DurationField, ExpressionWrapper, F, Q, Sum
from django.db.models.functions import ExtractHour
from django.utils import timezone

from .engine import grace
from .models import Booking, Branch, Service, Ticket

T = Ticket.Status
B = Booking.Status
MAX_RANGE_DAYS = 92


@dataclass
class Stats:
    tickets: int = 0
    served: int = 0
    ticket_no_shows: int = 0
    left_queue: int = 0
    booking_no_shows: int = 0
    bookings_cancelled: int = 0
    wait_count: int = 0
    wait_total: timedelta = field(default_factory=timedelta)
    service_count: int = 0
    service_total: timedelta = field(default_factory=timedelta)
    waits: list[float] = field(default_factory=list)  # seconds, for the median

    def add(self, other: Stats) -> None:
        self.tickets += other.tickets
        self.served += other.served
        self.ticket_no_shows += other.ticket_no_shows
        self.left_queue += other.left_queue
        self.booking_no_shows += other.booking_no_shows
        self.bookings_cancelled += other.bookings_cancelled
        self.wait_count += other.wait_count
        self.wait_total += other.wait_total
        self.service_count += other.service_count
        self.service_total += other.service_total
        self.waits.extend(other.waits)

    @property
    def is_empty(self) -> bool:
        return not (self.tickets or self.booking_no_shows or self.bookings_cancelled)

    @property
    def no_shows(self) -> int:
        return self.ticket_no_shows + self.booking_no_shows

    @property
    def no_show_rate(self) -> float | None:
        """Percentage, or None when nothing was expected."""
        expected = self.tickets + self.booking_no_shows
        return 100 * self.no_shows / expected if expected else None

    @property
    def avg_wait_min(self) -> float | None:
        if not self.wait_count:
            return None
        return self.wait_total.total_seconds() / 60 / self.wait_count

    @property
    def median_wait_min(self) -> float | None:
        return statistics.median(self.waits) / 60 if self.waits else None

    @property
    def avg_service_min(self) -> float | None:
        if not self.service_count:
            return None
        return self.service_total.total_seconds() / 60 / self.service_count


@dataclass
class Report:
    branch: Branch
    start: date
    end: date
    services: list[tuple[Service, Stats]]
    hours: list[tuple[int, Stats]]
    total: Stats

    @property
    def busiest_hour_tickets(self) -> int:
        return max((s.tickets for _, s in self.hours), default=0)

    @property
    def hour_rows(self):
        """(hour, stats, bar width 0-100) for the bar table."""
        top = self.busiest_hour_tickets
        for hour, stats in self.hours:
            yield hour, stats, round(100 * stats.tickets / top) if top else 0


def _seconds(value) -> float:
    """Durations come back as timedelta (PostgreSQL) or microseconds (some SQLite paths)."""
    if isinstance(value, timedelta):
        return value.total_seconds()
    return float(value) / 1_000_000


def _duration(value) -> timedelta:
    if value is None:
        return timedelta()
    return value if isinstance(value, timedelta) else timedelta(microseconds=value)


def build_report(
    branch: Branch,
    start: date,
    end: date,
    *,
    service: Service | None = None,
    now: datetime | None = None,
) -> Report:
    now = now or timezone.now()
    tz = timezone.get_default_timezone()

    tickets = Ticket.objects.filter(branch=branch, day__gte=start, day__lte=end)
    services = list(branch.services.order_by("prefix"))
    if service is not None:
        tickets = tickets.filter(service=service)
        services = [service]
    tickets = tickets.annotate(hour=ExtractHour("created_at", tzinfo=tz)).order_by()

    groups: dict[tuple[int, int], Stats] = {}

    def group(service_id: int, hour: int) -> Stats:
        return groups.setdefault((service_id, hour), Stats())

    wait = ExpressionWrapper(F("first_called_at") - F("created_at"), output_field=DurationField())
    service_time = ExpressionWrapper(
        F("finished_at") - F("serving_at"), output_field=DurationField()
    )
    timed = Q(status=T.DONE, serving_at__isnull=False, finished_at__isnull=False)
    rows = tickets.values("service_id", "hour").annotate(
        n=Count("id"),
        served=Count("id", filter=Q(status=T.DONE)),
        no_shows=Count("id", filter=Q(status=T.NO_SHOW)),
        left=Count("id", filter=Q(status=T.CANCELLED)),
        wait_n=Count("id", filter=Q(first_called_at__isnull=False)),
        wait_sum=Sum(wait, filter=Q(first_called_at__isnull=False)),
        service_n=Count("id", filter=timed),
        service_sum=Sum(service_time, filter=timed),
    )
    for row in rows:
        stats = group(row["service_id"], row["hour"])
        stats.tickets = row["n"]
        stats.served = row["served"]
        stats.ticket_no_shows = row["no_shows"]
        stats.left_queue = row["left"]
        stats.wait_count = row["wait_n"]
        stats.wait_total = _duration(row["wait_sum"])
        stats.service_count = row["service_n"]
        stats.service_total = _duration(row["service_sum"])

    waits = (
        tickets.filter(first_called_at__isnull=False)
        .annotate(wait=wait)
        .values_list("service_id", "hour", "wait")
    )
    for service_id, hour, value in waits:
        group(service_id, hour).waits.append(_seconds(value))

    range_start = datetime.combine(start, time.min, tzinfo=tz)
    range_end = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    missed = Q(status=B.NO_SHOW) | Q(status=B.BOOKED, slot__starts_at__lt=now - grace())
    bookings = Booking.objects.filter(
        slot__service__branch=branch,
        slot__starts_at__gte=range_start,
        slot__starts_at__lt=range_end,
    )
    if service is not None:
        bookings = bookings.filter(slot__service=service)
    booking_rows = (
        bookings.filter(missed | Q(status=B.CANCELLED))
        .annotate(hour=ExtractHour("slot__starts_at", tzinfo=tz))
        .order_by()
        .values("slot__service_id", "hour")
        .annotate(
            missed=Count("id", filter=missed), cancelled=Count("id", filter=Q(status=B.CANCELLED))
        )
    )
    for row in booking_rows:
        stats = group(row["slot__service_id"], row["hour"])
        stats.booking_no_shows = row["missed"]
        stats.bookings_cancelled = row["cancelled"]

    per_service = {s.pk: Stats() for s in services}
    per_hour: dict[int, Stats] = {}
    total = Stats()
    for (service_id, hour), stats in groups.items():
        if service_id in per_service:
            per_service[service_id].add(stats)
        per_hour.setdefault(hour, Stats()).add(stats)
        total.add(stats)

    hours = []
    if per_hour:
        for hour in range(min(per_hour), max(per_hour) + 1):
            hours.append((hour, per_hour.get(hour, Stats())))
    return Report(
        branch=branch,
        start=start,
        end=end,
        services=[(s, per_service[s.pk]) for s in services],
        hours=hours,
        total=total,
    )


# --- CSV ------------------------------------------------------------------------------------

CSV_HEADER = [
    "group",
    "key",
    "tickets",
    "served",
    "avg_wait_min",
    "median_wait_min",
    "avg_service_min",
    "ticket_no_shows",
    "booking_no_shows",
    "no_show_rate_pct",
    "left_queue",
    "bookings_cancelled",
]
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def safe_cell(value) -> str:
    """Text for one CSV cell; text that a spreadsheet would run as a formula gets a ' prefix."""
    if value is None:
        return ""
    if isinstance(value, float):
        # Same rounding as the page's floatformat (half up), so CSV and screen agree.
        return str(Decimal(repr(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
    text = str(value)
    if text.startswith(FORMULA_START):
        return "'" + text
    return text


def _csv_row(group: str, key: str, s: Stats) -> list[str]:
    values = [
        group,
        key,
        s.tickets,
        s.served,
        s.avg_wait_min,
        s.median_wait_min,
        s.avg_service_min,
        s.ticket_no_shows,
        s.booking_no_shows,
        s.no_show_rate,
        s.left_queue,
        s.bookings_cancelled,
    ]
    return [safe_cell(v) for v in values]


def report_csv(report: Report) -> str:
    """RFC 4180 CSV (CRLF line ends, fields quoted when needed). Header names stay English."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(CSV_HEADER)
    for service, stats in report.services:
        writer.writerow(_csv_row("service", f"{service.prefix} {service.name}", stats))
    for hour, stats in report.hours:
        writer.writerow(_csv_row("hour", f"{hour:02d}:00", stats))
    writer.writerow(
        _csv_row("total", f"{report.start.isoformat()}..{report.end.isoformat()}", report.total)
    )
    return out.getvalue()
