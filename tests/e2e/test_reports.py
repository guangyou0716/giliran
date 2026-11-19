"""Browser end-to-end for manager reports: a manager opens Reports from the dashboard,
sees the per-hour bar table built from the seeded history, filters one service and
downloads the CSV. Counter staff get 403.

Run: pytest -m e2e   (GILIRAN_SCREENSHOTS=docs/screenshots saves the README screenshot)
"""

import csv
import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from playwright.sync_api import expect

from queues import engine
from queues.models import Branch
from queues.reports import build_report

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

PASSWORD = "e2e-pass-123"


def _login(page, base, username):
    page.goto(f"{base}/accounts/login/")
    page.get_by_label("Username").fill(username)
    page.get_by_label("Password").fill(PASSWORD)
    page.get_by_role("button", name="Log in").click()


def test_manager_reports_filter_and_csv(live_server, browser, screenshot_dir):
    call_command("seed_demo", password=PASSWORD, tickets=0, verbosity=0)
    base = live_server.url
    branch = Branch.objects.get(slug="seri-mawar")
    today = engine.local_day()
    start, end = today - timedelta(days=13), today
    expected = build_report(branch, start, end)

    ctx = browser.new_context(viewport={"width": 1280, "height": 900}, locale="en-US")
    page = ctx.new_page()
    _login(page, base, "manager")
    page.get_by_role("link", name="Reports").first.click()
    expect(page.get_by_role("heading", name="Reports", exact=True)).to_be_visible()

    page.get_by_label("From", exact=True).fill(start.isoformat())
    page.get_by_label("To", exact=True).fill(end.isoformat())
    page.get_by_role("button", name="Show").click()
    tickets_card = page.locator(".report-stats div").filter(has_text="Tickets").locator("dd")
    expect(tickets_card).to_have_text(str(expected.total.tickets))
    hour_rows = page.locator("table[aria-labelledby='by-hour'] tbody tr")
    expect(hour_rows.first).to_contain_text("09:00")
    assert page.locator(".bar > span[style='width: 100%']").count() >= 1
    if screenshot_dir:
        page.screenshot(path=f"{screenshot_dir}/reports.png", full_page=True)

    page.get_by_label("Service", exact=True).select_option(label="P · Pharmacy")
    page.get_by_role("button", name="Show").click()
    pharmacy = next(s for svc, s in expected.services if svc.prefix == "P")
    expect(tickets_card).to_have_text(str(pharmacy.tickets))

    with page.expect_download() as info:
        page.get_by_role("link", name="Download CSV").click()
    download = info.value
    assert download.suggested_filename == (
        f"giliran-seri-mawar-{start.isoformat()}-{end.isoformat()}.csv"
    )
    with open(download.path(), encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(io.StringIO(fh.read(), newline="")))
    assert [r["key"] for r in rows if r["group"] == "service"] == ["P Pharmacy"]
    assert rows[-1]["group"] == "total" and rows[-1]["tickets"] == str(pharmacy.tickets)

    staff = browser.new_context(locale="en-US").new_page()
    _login(staff, base, "staff1")
    response = staff.goto(f"{base}/manage/b/seri-mawar/reports/")
    assert response.status == 403
