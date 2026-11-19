"""Blank phone numbers of old bookings (data retention).

Run daily from cron. Bookings whose slot started more than N days ago lose their phone number
(N = ``--days`` or ``GILIRAN_PHONE_RETENTION_DAYS``, default 30); reference, nickname, status and
times stay so reports keep working. Safe to run again and again: rows that are already blank are
not touched. Tickets hold no phone number, so only bookings are affected.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from queues import engine


class Command(BaseCommand):
    help = "Blank the phone number of bookings older than GILIRAN_PHONE_RETENTION_DAYS days."

    def add_arguments(self, parser):
        parser.add_argument(
            "--days",
            type=int,
            default=None,
            help=f"Retention in days (default {settings.GILIRAN_PHONE_RETENTION_DAYS}).",
        )

    def handle(self, *args, **opts):
        try:
            n = engine.purge_phones(opts["days"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"Blanked the phone number of {n} booking(s).")
