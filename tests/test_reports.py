"""Manager reports: numbers from a small worked table, invariants, access, CSV.

Worked table (branch "Test Clinic", day D = Monday 2026-03-02, Malaysia time;
minutes after the hour; wait = first call - created, service = done - start):

  ticket  svc  created  1st call  start  end    status     wait  service
  A-001   A    09:00    09:10     09:11  09:19  done        10    8
  A-002   A    09:20    09:40     09:41  09:53  done        20   12
  A-003   A    10:05    10:35     -      10:40  no_show     30    -
  A-004   A    10:10    -         -      10:15  cancelled    -    -
  P-001   P    09:30    09:34     09:34  09:37  done         4    3
  P-002   P    10:00    10:06     -      10:07  done (no start) 6  -

  bookings (service A): 11:00 no_show; 11:00 still "booked" but slot long past (a
  missed appointment the expiry job has not marked yet); 09:00 cancelled.
  Outside the range / branch (must be ignored): a ticket on D-1, a no-show booking on
  D+1, a ticket at another branch on D.

Expected
  A: tickets 4, served 2, waits [10, 20, 30] -> avg 20, median 20; service [8, 12] ->
     avg 10; no-shows 1 ticket + 2 bookings = 3; rate 3 / (4 + 2) = 50.0 %;
     left queue 1; bookings cancelled 1.
  P: tickets 2, served 2, waits [4, 6] -> avg 5, median 5; service [3] -> 3;
     no-shows 0, rate 0.0 %.
  09:00: A-001, A-002, P-001 -> tickets 3, served 3, waits [10, 20, 4] -> avg 11.33,
     median 10; bookings cancelled 1.
  10:00: A-003, A-004, P-002 -> tickets 3, served 1, waits [30, 6] -> avg 18,
     median 18; no-shows 1; left 1.
  11:00: 0 tickets, 2 booking no-shows -> rate 2 / (0 + 2) = 100 %.
  Total: tickets 6, served 4, waits [10, 20, 30, 4, 6] -> avg 14, median 10;
     service [8, 12, 3] -> 7.67; no-shows 3; rate 3 / (6 + 2) = 37.5 %.
"""

import csv
import io
from datetime import date, timedelta

import pytest
from django.urls import reverse

from queues import engine
from queues.forms import ReportFilterForm
from queues.models import Booking, Service, Slot, Ticket
from queues.reports import MAX_RANGE_DAYS, build_report, report_csv, safe_cell

from .conftest import myt

pytestmark = pytest.mark.django_db

D = date(2026, 3, 2)
NOW = myt(2026, 3, 2, 18, 0)
S = Ticket.Status


def at(hh, mm, day=D):
    return myt(day.year, day.month, day.day, hh, mm)


def ticket(service, number, created, status, called=None, start=None, end=None, day=D):
    return Ticket.objects.create(
        branch=service.branch,
        service=service,
        day=day,
        number=number,
        code=engine.format_code(service.prefix, number),
        status=status,
        created_at=created,
        called_at=called,
        first_called_at=called,
        serving_at=start,
        finished_at=end,
    )


def booking(service, starts_at, status, ref, phone="000-000 0001"):
    slot, _ = Slot.objects.get_or_create(
        service=service,
        starts_at=starts_at,
        defaults={"ends_at": starts_at + timedelta(minutes=30), "capacity": 5},
    )
    return Booking.objects.create(slot=slot, ref=ref, nickname="Test", phone=phone, status=status)


@pytest.fixture
def worked(branch, other_branch, consult, pharmacy):
    a, p = consult, pharmacy
    ticket(a, 1, at(9, 0), S.DONE, at(9, 10), at(9, 11), at(9, 19))
    ticket(a, 2, at(9, 20), S.DONE, at(9, 40), at(9, 41), at(9, 53))
    ticket(a, 3, at(10, 5), S.NO_SHOW, at(10, 35), None, at(10, 40))
    ticket(a, 4, at(10, 10), S.CANCELLED, None, None, at(10, 15))
    ticket(p, 1, at(9, 30), S.DONE, at(9, 34), at(9, 34), at(9, 37))
    ticket(p, 2, at(10, 0), S.DONE, at(10, 6), None, at(10, 7))
    booking(a, at(11, 0), Booking.Status.NO_SHOW, "NS0001", phone="000-000 0001")
    booking(a, at(11, 0), Booking.Status.BOOKED, "BK0001", phone="000-000 0002")
    booking(a, at(9, 0), Booking.Status.CANCELLED, "CX0001")
    # Outside the range or the branch.
    prev = D - timedelta(days=1)
    ticket(a, 1, at(9, 0, prev), S.DONE, at(9, 5, prev), at(9, 6, prev), at(9, 9, prev), day=prev)
    booking(a, at(11, 0, D + timedelta(days=1)), Booking.Status.NO_SHOW, "NS0002")
    elsewhere = Service.objects.create(branch=other_branch, name="Desk", prefix="A")
    ticket(elsewhere, 1, at(9, 0), S.NO_SHOW, at(9, 1), None, at(9, 2))
    return branch


def stats_for(report, prefix):
    return next(s for service, s in report.services if service.prefix == prefix)


def test_worked_table_per_service(worked):
    report = build_report(worked, D, D, now=NOW)
    a, p = stats_for(report, "A"), stats_for(report, "P")
    assert (a.tickets, a.served, a.ticket_no_shows, a.booking_no_shows) == (4, 2, 1, 2)
    assert (a.avg_wait_min, a.median_wait_min, a.avg_service_min) == (20, 20, 10)
    assert (a.no_shows, a.no_show_rate, a.left_queue, a.bookings_cancelled) == (3, 50, 1, 1)
    assert (p.tickets, p.served, p.avg_wait_min, p.median_wait_min) == (2, 2, 5, 5)
    assert (p.avg_service_min, p.no_shows, p.no_show_rate, p.left_queue) == (3, 0, 0, 0)


def test_worked_table_per_hour_and_total(worked):
    report = build_report(worked, D, D, now=NOW)
    hours = dict(report.hours)
    assert list(hours) == [9, 10, 11]
    h9, h10, h11 = hours[9], hours[10], hours[11]
    assert (h9.tickets, h9.served, h9.median_wait_min, h9.bookings_cancelled) == (3, 3, 10, 1)
    assert h9.avg_wait_min == pytest.approx(34 / 3)
    assert (h10.tickets, h10.served, h10.avg_wait_min, h10.median_wait_min) == (3, 1, 18, 18)
    assert (h10.no_shows, h10.left_queue) == (1, 1)
    assert (h11.tickets, h11.booking_no_shows, h11.no_show_rate) == (0, 2, 100)
    assert h11.avg_wait_min is None and h11.median_wait_min is None
    t = report.total
    assert (t.tickets, t.served, t.avg_wait_min, t.median_wait_min) == (6, 4, 14, 10)
    assert t.avg_service_min == pytest.approx(23 / 3)
    assert (t.no_shows, t.no_show_rate, t.left_queue, t.bookings_cancelled) == (3, 37.5, 1, 1)


COUNT_FIELDS = (
    "tickets",
    "served",
    "ticket_no_shows",
    "booking_no_shows",
    "left_queue",
    "bookings_cancelled",
    "wait_count",
    "service_count",
)


def assert_rollups_add_up(report):
    for name in COUNT_FIELDS:
        total = getattr(report.total, name)
        assert sum(getattr(s, name) for _, s in report.services) == total, name
        assert sum(getattr(s, name) for _, s in report.hours) == total, name
    t = report.total
    assert len(t.waits) == t.wait_count
    assert sum(t.waits) == pytest.approx(t.wait_total.total_seconds())
    # Every ticket ended in exactly one way (all tickets here are finished).
    assert t.served + t.ticket_no_shows + t.left_queue == t.tickets


def test_invariants_worked_table(worked):
    assert_rollups_add_up(build_report(worked, D, D, now=NOW))


def test_invariants_on_seeded_history():
    from django.core.management import call_command

    from queues.models import Branch

    call_command("seed_demo", password="seed-pass-123", tickets=0, verbosity=0)
    branch = Branch.objects.get(slug="seri-mawar")
    today = engine.local_day()
    report = build_report(branch, today - timedelta(days=14), today - timedelta(days=1))
    assert report.total.tickets > 100
    assert report.total.booking_no_shows >= 14  # one missed appointment per past day
    assert report.total.avg_wait_min > 0 and report.total.avg_service_min > 0
    assert_rollups_add_up(report)


def test_service_filter(worked, pharmacy):
    report = build_report(worked, D, D, service=pharmacy, now=NOW)
    assert [s.prefix for s, _ in report.services] == ["P"]
    assert (report.total.tickets, report.total.booking_no_shows) == (2, 0)
    assert [h for h, _ in report.hours] == [9, 10]


def test_range_includes_previous_day(worked):
    report = build_report(worked, D - timedelta(days=1), D, now=NOW)
    assert report.total.tickets == 7
    # waits sorted [4, 5, 6, 10, 20, 30] -> median (6 + 10) / 2 = 8
    assert report.total.median_wait_min == 8


def test_stale_booked_counts_only_after_grace(worked):
    # At 11:05 the 11:00 "booked" appointment is still inside the 10 minute grace.
    report = build_report(worked, D, D, now=at(11, 5))
    assert report.total.booking_no_shows == 1


def test_hours_are_malaysia_time(branch, consult):
    # 23:30 MYT is 15:30 UTC; it must count at 23:00 on D.
    ticket(consult, 1, at(23, 30), S.WAITING)
    report = build_report(branch, D, D, now=NOW)
    assert [h for h, _ in report.hours] == [23]


def test_empty_report(branch, consult):
    report = build_report(branch, D, D, now=NOW)
    assert report.hours == [] and report.total.is_empty
    assert report.total.no_show_rate is None and report.total.avg_wait_min is None


def test_fixed_number_of_queries(worked, branch, django_assert_num_queries):
    for i in range(8):
        service = Service.objects.create(branch=branch, name=f"Extra {i}", prefix=f"X{chr(65 + i)}")
        ticket(service, 1, at(9 + i, 0), S.DONE, at(9 + i, 3), at(9 + i, 4), at(9 + i, 9))
    # services, ticket aggregate, wait list, booking aggregate
    with django_assert_num_queries(4):
        report = build_report(branch, D, D, now=NOW)
    assert len(report.services) == 10


def test_recall_keeps_first_call_time(branch, consult, room1):
    t = engine.issue_ticket(consult, now=at(9, 0))
    engine.call_next(room1, now=at(9, 10))
    engine.apply_action(t, "recall", now=at(9, 15))
    t.refresh_from_db()
    assert t.called_at == at(9, 15) and t.first_called_at == at(9, 10)
    engine.apply_action(t, "start", now=at(9, 16))
    engine.apply_action(t, "done", now=at(9, 20))
    assert build_report(branch, D, D, now=NOW).total.avg_wait_min == 10


def test_migration_backfills_first_called_at(branch, consult, room1):
    import importlib

    from django.apps import apps

    migration = importlib.import_module("queues.migrations.0003_reports_first_called_at")
    t1 = engine.issue_ticket(consult, now=at(9, 0))
    t2 = engine.issue_ticket(consult, now=at(9, 1))
    engine.call_next(room1, now=at(9, 10))
    engine.apply_action(t1, "recall", now=at(9, 12))
    # t2: called before the audit log existed (no "call" event) -> falls back to called_at.
    Ticket.objects.filter(pk=t2.pk).update(called_at=at(9, 30))
    Ticket.objects.update(first_called_at=None)
    migration.backfill_first_called_at(apps, None)
    t1.refresh_from_db()
    t2.refresh_from_db()
    assert t1.first_called_at == at(9, 10) and t2.first_called_at == at(9, 30)


# --- form -------------------------------------------------------------------------------------


def test_form_rejects_reversed_and_too_long_ranges(branch):
    reversed_ = ReportFilterForm({"start": "2026-03-05", "end": "2026-03-01"}, branch=branch)
    assert not reversed_.is_valid()
    end = D + timedelta(days=MAX_RANGE_DAYS)
    too_long = ReportFilterForm({"start": D.isoformat(), "end": end.isoformat()}, branch=branch)
    assert not too_long.is_valid()
    end = D + timedelta(days=MAX_RANGE_DAYS - 1)
    longest = ReportFilterForm({"start": D.isoformat(), "end": end.isoformat()}, branch=branch)
    assert longest.is_valid()


def test_form_only_offers_own_branch_services(branch, other_branch, consult):
    other = Service.objects.create(branch=other_branch, name="Desk", prefix="D")
    form = ReportFilterForm(
        {"start": "2026-03-01", "end": "2026-03-02", "service": other.pk}, branch=branch
    )
    assert not form.is_valid() and "service" in form.errors


# --- views and access -------------------------------------------------------------------------


def url(branch, **params):
    from urllib.parse import urlencode

    base = reverse("manage:reports", args=[branch.slug])
    return f"{base}?{urlencode(params)}" if params else base


def test_manager_sees_report(worked, manager_client):
    r = manager_client.get(url(worked, start=D.isoformat(), end=D.isoformat()))
    assert r.status_code == 200
    body = r.content.decode()
    assert "By hour of day" in body and "37.5" in body and "14.0" in body
    assert 'style="width: 100%"' in body  # busiest hour bar


def test_default_range_is_last_seven_days(branch, consult, manager_client):
    r = manager_client.get(url(branch))
    report = r.context["report"]
    assert (report.end - report.start).days == 6 and report.end == engine.local_day()
    assert f"start={report.start.isoformat()}" in r.context["csv_query"]


def test_invalid_range_shows_error(branch, manager_client):
    r = manager_client.get(url(branch, start="2026-03-05", end="2026-03-01"))
    assert r.status_code == 200 and r.context["report"] is None
    assert "on or before the end date" in r.content.decode()
    csv_r = manager_client.get(url(branch, start="2026-03-05", end="2026-03-01", format="csv"))
    assert csv_r["Content-Type"].startswith("text/html")


def test_staff_denied(branch, staff_client):
    assert staff_client.get(url(branch)).status_code == 403
    assert staff_client.get(url(branch, format="csv")).status_code == 403


def test_manager_of_other_branch_denied(branch, client, outsider):
    client.force_login(outsider)
    assert client.get(url(branch)).status_code == 403


def test_anonymous_redirected_to_login(branch, client):
    r = client.get(url(branch))
    assert r.status_code == 302 and "/login" in r["Location"]


def test_links_shown_to_managers_only(branch, manager_client, client, staff_user):
    report_url = reverse("manage:reports", args=[branch.slug])
    assert report_url in manager_client.get(reverse("staff:dashboard")).content.decode()
    assert (
        report_url
        in manager_client.get(reverse("manage:settings", args=[branch.slug])).content.decode()
    )
    client.force_login(staff_user)
    assert report_url not in client.get(reverse("staff:dashboard")).content.decode()


@pytest.mark.parametrize("lang,text", [("ms", "Laporan"), ("zh-hans", "报表")])
def test_report_page_translated(branch, consult, manager_client, lang, text):
    r = manager_client.get(url(branch), HTTP_ACCEPT_LANGUAGE=lang)
    assert f"<h1>{text}</h1>" in r.content.decode()


# --- CSV --------------------------------------------------------------------------------------


def test_csv_download(worked, manager_client):
    r = manager_client.get(url(worked, start=D.isoformat(), end=D.isoformat(), format="csv"))
    assert r.status_code == 200
    assert r["Content-Type"] == "text/csv; charset=utf-8"
    assert r["Content-Disposition"] == (
        'attachment; filename="giliran-test-clinic-2026-03-02-2026-03-02.csv"'
    )
    text = r.content.decode("utf-8")
    assert text.startswith("\ufeff")  # BOM so Excel reads Malay/Chinese names as UTF-8
    body = text[1:]
    assert body.endswith("\r\n") and "\n" not in body.replace("\r\n", "")
    rows = list(csv.DictReader(io.StringIO(body, newline="")))
    by_key = {(row["group"], row["key"]): row for row in rows}
    assert [row["group"] for row in rows] == ["service"] * 2 + ["hour"] * 3 + ["total"]
    total = by_key[("total", "2026-03-02..2026-03-02")]
    assert total["tickets"] == "6" and total["no_show_rate_pct"] == "37.5"
    assert total["avg_wait_min"] == "14.0" and total["avg_service_min"] == "7.7"
    assert by_key[("hour", "11:00")]["avg_wait_min"] == ""  # nothing to average
    assert by_key[("service", "A Consultation")]["booking_no_shows"] == "2"


def test_csv_quotes_and_formula_guard(branch, manager_user):
    evil = Service.objects.create(branch=branch, name='=HYPERLINK("x"), "q"', prefix="E")
    ticket(evil, 1, at(9, 0), S.DONE, at(9, 1), at(9, 2), at(9, 3))
    text = report_csv(build_report(branch, D, D, now=NOW))
    line = next(line for line in text.split("\r\n") if line.startswith("service,"))
    # The key starts with the prefix letter, so it is not a formula; quoting is RFC 4180.
    assert line.startswith('service,"E =HYPERLINK(""x""), ""q""",')
    row = next(csv.reader(io.StringIO(line)))
    assert row[1] == 'E =HYPERLINK("x"), "q"'


@pytest.mark.parametrize("raw", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_safe_cell_neutralises_formulas(raw):
    assert safe_cell(raw) == "'" + raw


def test_safe_cell_plain_values():
    assert safe_cell(None) == "" and safe_cell(3) == "3" and safe_cell(0.0) == "0.0"
    assert safe_cell("Klinik 1") == "Klinik 1" and safe_cell(7.0) == "7.0"


@pytest.mark.parametrize("value", [2.25, 7.25, 12.35, 0.05, 23 / 3, 34 / 3, 100.0])
def test_csv_numbers_match_page_rounding(value):
    from queues.templatetags.report_tags import num1

    assert safe_cell(value) == num1(value)
