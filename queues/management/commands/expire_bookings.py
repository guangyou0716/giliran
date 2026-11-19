"""Mark bookings that were not checked in by slot start + grace as no-show.

Run from cron every few minutes (the staff appointments page also does this lazily).
"""

from django.core.management.base import BaseCommand

from queues import engine


class Command(BaseCommand):
    help = "Mark missed bookings (not checked in within the grace window) as no-show."

    def handle(self, *args, **opts):
        n = engine.expire_bookings()
        self.stdout.write(f"Marked {n} booking(s) as no-show.")
