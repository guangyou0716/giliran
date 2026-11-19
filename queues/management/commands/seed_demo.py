"""Create synthetic demo data: 2 branches, services, counters, users and some tickets.

All names are made up. Passwords come from --password or GILIRAN_DEMO_PASSWORD;
if neither is given a random one is generated and printed once.
"""

import os
import secrets
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from queues import engine
from queues.models import Branch, Counter, Membership, Service, Ticket

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
    },
    {
        "name": "Demo Service Centre Cempaka",
        "slug": "cempaka",
        "services": [("S", "Service desk", 6), ("C", "Collection", 2)],
        "counters": [("Counter 1", ["S", "C"]), ("Counter 2", ["S"])],
        "manager": "manager2",
        "staff": ["staff3"],
    },
]

NICKNAMES = ["", "Ah Meng", "Siti", "Ravi", "", "Mei Ling", "Pak Cik", "", "Aina", "Kumar"]


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

            if opts["no_tickets"]:
                continue
            today = engine.local_day()
            if Ticket.objects.filter(branch=branch, day=today).exists():
                continue
            self._tickets(branch, list(services.values()), opts["tickets"])

        self.stdout.write(self.style.SUCCESS("Demo data ready."))
        self.stdout.write("Accounts: admin (superuser), manager, staff1, staff2, manager2, staff3")
        if used_generated:
            self.stdout.write(f"Generated password for new demo accounts (shown once): {password}")

    def _tickets(self, branch, services, count):
        start = timezone.now() - timedelta(minutes=count * 4)
        for i in range(count):
            service = services[i % len(services)]
            engine.issue_ticket(
                service,
                nickname=NICKNAMES[i % len(NICKNAMES)],
                priority=(i % 7 == 3),
                source=Ticket.Source.DESK if i % 3 == 0 else Ticket.Source.SELF,
                now=start + timedelta(minutes=i * 4),
            )
        # Serve the first few so the board and history have something to show.
        for counter in branch.counters.filter(is_active=True)[:2]:
            ticket = engine.call_next(counter)
            if ticket:
                engine.apply_action(ticket, "start")
                engine.apply_action(ticket, "done")
            engine.call_next(counter)
