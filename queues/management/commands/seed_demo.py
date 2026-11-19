"""Create synthetic demo data: 2 branches, services, counters, users, some tickets,
appointment slots (today + 2 days), a few bookings, and 14 days of finished history
(tickets and past appointments) so the manager reports have something to show.

All names are made up. Passwords come from --password or GILIRAN_DEMO_PASSWORD;
if neither is given a random one is generated and printed once.
"""

import os
import random
import secrets
from datetime import datetime, time, timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from queues import engine
from queues.models import (
    Booking,
    Branch,
    Counter,
    DailySequence,
    Membership,
    Service,
    Slot,
    Ticket,
    TicketEvent,
)

DEMO = [
    {
        "name": "Klinik Demo Seri Mawar",
        "slug": "seri-mawar",
        "services": [("A", "Consultation", 8), ("P", "Pharmacy", 3), ("B", "Blood test", 5)],
        "counters": [
            ("Room 1", ["A"]),
            ("Room 2", ["A", "B"]),
            ("Pharmacy counter", ["P"]),
        ],
        "manager": "manager",
        "staff": ["staff1", "staff2"],
        "bookable": "A",
    },
    {
        "name": "Demo Service Centre Cempaka",
        "slug": "cempaka",
        "services": [("S", "Service desk", 6), ("C", "Collection", 2)],
        "counters": [("Counter 1", ["S", "C"]), ("Counter 2", ["S"])],
        "manager": "manager2",
        "staff": ["staff3"],
        "bookable": "S",
    },
]

NICKNAMES = ["", "Ah Meng", "Siti", "Ravi", "", "Mei Ling", "Pak Cik", "", "Aina", "Kumar"]
BOOKERS = ["Mak Cik Ros", "Jason", "Puan Nora", "Wei Jie"]
# Clearly fake numbers (no real Malaysian number starts with 000).
FAKE_PHONES = ["000-000 0101", "000-000 0102", "000-000 0103", "000-000 0104"]
SLOT_DAYS = 3  # today and the next two days
SLOT_START, SLOT_END, SLOT_MINUTES, SLOT_SEATS = time(9, 0), time(17, 0), 30, 2
BOOKED_TIMES = [time(9, 0), time(9, 30), time(10, 0), time(11, 0)]
HISTORY_DAYS = 14
# Relative arrival weight per opening hour 09:00-16:00 (morning peak, lunch dip).
HOUR_WEIGHTS = {9: 5, 10: 6, 11: 4, 12: 2, 13: 2, 14: 4, 15: 3, 16: 2}


class Command(BaseCommand):
    help = "Create synthetic demo branches, users and tickets (idempotent)."

    def add_arguments(self, parser):
        parser.add_argument("--password", help="Password for all demo accounts.")
        parser.add_argument(
            "--tickets", type=int, default=12, help="Tickets to create per branch today."
        )
        parser.add_argument(
            "--no-tickets", action="store_true", help="Only create branches, services and users."
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        given = opts.get("password") or os.environ.get("GILIRAN_DEMO_PASSWORD")
        password = given or secrets.token_urlsafe(12)
        used_generated = False
        User = get_user_model()

        admin, created = User.objects.get_or_create(
            username="admin", defaults={"is_staff": True, "is_superuser": True}
        )
        if created or given:
            admin.set_password(password)
            admin.save()
            used_generated |= not given

        for spec in DEMO:
            branch, _ = Branch.objects.get_or_create(
                slug=spec["slug"], defaults={"name": spec["name"]}
            )
            services = {}
            for prefix, name, minutes in spec["services"]:
                services[prefix], _ = Service.objects.get_or_create(
                    branch=branch, prefix=prefix, defaults={"name": name, "avg_minutes": minutes}
                )
            for name, prefixes in spec["counters"]:
                counter, _ = Counter.objects.get_or_create(branch=branch, name=name)
                counter.services.set([services[p] for p in prefixes])
            people = [(spec["manager"], Membership.Role.MANAGER)] + [
                (u, Membership.Role.STAFF) for u in spec["staff"]
            ]
            for username, role in people:
                user, created = User.objects.get_or_create(username=username)
                if created or given:
                    user.set_password(password)
                    user.save()
                    used_generated |= not given
                Membership.objects.update_or_create(
                    user=user, branch=branch, defaults={"role": role}
                )

            bookable = services[spec["bookable"]]
            self._slots(bookable)

            if opts["no_tickets"]:
                continue
            if not Booking.objects.filter(slot__service__branch=branch).exists():
                self._bookings(bookable)
            if not Ticket.objects.filter(branch=branch, day__lt=engine.local_day()).exists():
                self._history(branch, list(services.values()), bookable)
            today = engine.local_day()
            if Ticket.objects.filter(branch=branch, day=today).exists():
                continue
            self._tickets(branch, list(services.values()), opts["tickets"])

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write("Accounts: admin (superuser), manager, staff1, staff2, manager2, staff3")
        if used_generated:
            self.stdout.write(f"Generated password for new demo accounts (shown once): {password}")

    def _slots(self, service):
        tz = timezone.get_current_timezone()
        step = timedelta(minutes=SLOT_MINUTES)
        for offset in range(SLOT_DAYS):
            day = engine.local_day() + timedelta(days=offset)
            start = datetime.combine(day, SLOT_START, tzinfo=tz)
            last = datetime.combine(day, SLOT_END, tzinfo=tz)
            while start + step <= last:
                Slot.objects.get_or_create(
                    service=service,
                    starts_at=start,
                    defaults={"ends_at": start + step, "capacity": SLOT_SEATS},
                )
                start += step

    def _bookings(self, service):
        """A few bookings today and tomorrow; booked "yesterday" so past slots are allowed."""
        tz = timezone.get_current_timezone()
        for offset in (0, 1):
            day = engine.local_day() + timedelta(days=offset)
            for i, at in enumerate(BOOKED_TIMES):
                slot = Slot.objects.get(
                    service=service, starts_at=datetime.combine(day, at, tzinfo=tz)
                )
                engine.book_slot(
                    slot,
                    nickname=BOOKERS[i],
                    phone=FAKE_PHONES[i],
                    now=slot.starts_at - timedelta(days=1),
                )

    def _tickets(self, branch, services, count, now=None):
        # Tickets every 4 minutes up to now, squeezed after local midnight when it is
        # early in the day: they must all belong to today, or the "already seeded
        # today?" check above would not see them and a re-run would add them again.
        now = now or timezone.now()
        midnight = timezone.localtime(now).replace(hour=0, minute=0, second=0, microsecond=0)
        step = min(timedelta(minutes=4), (now - midnight) / max(count, 1))
        start = now - step * count
        for i in range(count):
            service = services[i % len(services)]
            engine.issue_ticket(
                service,
                nickname=NICKNAMES[i % len(NICKNAMES)],
                priority=(i % 7 == 3),
                source=Ticket.Source.DESK if i % 3 == 0 else Ticket.Source.SELF,
                now=start + step * i,
            )
        # Serve the first few so the board and history have something to show.
        for counter in branch.counters.filter(is_active=True)[:2]:
            ticket = engine.call_next(counter)
            if ticket:
                engine.apply_action(ticket, "start")
                engine.apply_action(ticket, "done")
            engine.call_next(counter)

    def _history(self, branch, services, bookable):
        """Finished tickets and past appointments for the last HISTORY_DAYS days.

        Written in bulk (a few queries per branch) with the same fields and audit
        events the engine would have produced. Fixed random seed per branch, so every
        run builds the same history.
        """
        rng = random.Random(f"giliran-demo-{branch.slug}")  # noqa: S311 - synthetic data
        tz = timezone.get_current_timezone()
        counters = {
            s.pk: list(s.counters.filter(is_active=True).order_by("name")) for s in services
        }
        hours, weights = list(HOUR_WEIGHTS), list(HOUR_WEIGHTS.values())
        tickets, sequences, days = [], [], []
        today = engine.local_day()
        for back in range(HISTORY_DAYS, 0, -1):
            day = today - timedelta(days=back)
            for service in services:
                arrivals = sorted(
                    datetime.combine(day, time(rng.choices(hours, weights)[0]), tzinfo=tz)
                    + timedelta(minutes=rng.randrange(60), seconds=rng.randrange(60))
                    for _ in range(rng.randint(5, 12))
                )
                for number, created in enumerate(arrivals, start=1):
                    tickets.append(
                        self._past_ticket(rng, service, counters[service.pk], day, number, created)
                    )
                sequences.append(DailySequence(service=service, day=day, last_number=len(arrivals)))
            days.append(day)
        DailySequence.objects.bulk_create(sequences)
        Ticket.objects.bulk_create(tickets)
        TicketEvent.objects.bulk_create(e for t in tickets for e in self._events(t))
        self._past_bookings(rng, bookable, days, tz)

    def _past_ticket(self, rng, service, counters, day, number, created):
        ticket = Ticket(
            branch=service.branch,
            service=service,
            day=day,
            number=number,
            code=engine.format_code(service.prefix, number),
            nickname=rng.choice(NICKNAMES),
            priority=rng.random() < 0.08,
            source=Ticket.Source.DESK if rng.random() < 0.3 else Ticket.Source.SELF,
            created_at=created,
        )
        roll = rng.random()
        if roll < 0.06:  # left the queue
            ticket.status = Ticket.Status.CANCELLED
            ticket.finished_at = created + timedelta(minutes=rng.randint(3, 15))
            return ticket
        ticket.counter = rng.choice(counters) if counters else None
        ticket.first_called_at = ticket.called_at = created + timedelta(
            minutes=rng.randint(1, 25), seconds=rng.randrange(60)
        )
        if roll < 0.14:  # called but never came
            ticket.status = Ticket.Status.NO_SHOW
            ticket.finished_at = ticket.called_at + timedelta(minutes=2)
            return ticket
        ticket.status = Ticket.Status.DONE
        ticket.serving_at = ticket.called_at + timedelta(seconds=rng.randint(20, 90))
        minutes = max(1.0, rng.gauss(service.avg_minutes, service.avg_minutes / 3))
        ticket.finished_at = ticket.serving_at + timedelta(minutes=minutes)
        return ticket

    @staticmethod
    def _events(ticket):
        S = Ticket.Status
        steps = [("issue", "", S.WAITING, ticket.created_at, None)]
        if ticket.status == S.CANCELLED:
            steps.append(("cancel", S.WAITING, S.CANCELLED, ticket.finished_at, None))
        else:
            steps.append(("call", S.WAITING, S.CALLED, ticket.first_called_at, ticket.counter))
            if ticket.status == S.NO_SHOW:
                steps.append(("no_show", S.CALLED, S.NO_SHOW, ticket.finished_at, ticket.counter))
            else:
                steps.append(("start", S.CALLED, S.SERVING, ticket.serving_at, ticket.counter))
                steps.append(("done", S.SERVING, S.DONE, ticket.finished_at, ticket.counter))
        return [
            TicketEvent(ticket=ticket, action=a, from_status=f, to_status=t, at=at, counter=c)
            for a, f, t, at, c in steps
        ]

    def _past_bookings(self, rng, service, days, tz):
        """Per day: one checked in (its ticket is the first served ticket of the
        bookable service), one no-show, one cancelled in advance."""
        step = timedelta(minutes=SLOT_MINUTES)
        for day in days:
            served = (
                Ticket.objects.filter(service=service, day=day, status=Ticket.Status.DONE)
                .order_by("created_at")
                .first()
            )
            plan = [(Booking.Status.NO_SHOW, None), (Booking.Status.CANCELLED, None)]
            if served is not None:
                plan.insert(0, (Booking.Status.CHECKED_IN, served))
            used = set()
            for i, (status, ticket) in enumerate(plan):
                if ticket is not None:
                    local = timezone.localtime(ticket.created_at, tz)
                    start = local.replace(minute=local.minute // 30 * 30, second=0, microsecond=0)
                else:
                    start = datetime.combine(day, time(rng.choice([10, 11, 14, 15])), tzinfo=tz)
                while start in used:
                    start += step
                used.add(start)
                slot, _ = Slot.objects.get_or_create(
                    service=service,
                    starts_at=start,
                    defaults={"ends_at": start + step, "capacity": SLOT_SEATS},
                )
                holds_seat = status != Booking.Status.CANCELLED
                Booking.objects.create(
                    slot=slot,
                    ref=engine._new_ref(),
                    nickname=BOOKERS[i % len(BOOKERS)],
                    phone=engine.normalize_phone(FAKE_PHONES[i % len(FAKE_PHONES)]),
                    status=status,
                    ticket=ticket,
                    created_at=start - timedelta(days=2),
                    checked_in_at=ticket.created_at if ticket else None,
                    cancelled_at=start - timedelta(days=1) if not holds_seat else None,
                )
                if holds_seat:
                    Slot.objects.filter(pk=slot.pk).update(booked_count=F("booked_count") + 1)
                if ticket is not None:
                    on_time = ticket.created_at <= start + engine.grace()
                    Ticket.objects.filter(pk=ticket.pk).update(
                        source=Ticket.Source.BOOKING, appointment_at=start if on_time else None
                    )
