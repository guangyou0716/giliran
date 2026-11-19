"""Browser end-to-end: Server-Sent Events push, polling fallback, and the
"almost your turn" alert (chime stubbed: no real audio in CI).
"""

import pytest
from playwright.sync_api import expect

from queues import engine
from queues.models import Ticket
from tests.conftest import PASSWORD

pytestmark = [pytest.mark.e2e, pytest.mark.django_db(transaction=True)]

# Fake WebAudio + Notification so the test can count chimes and notifications.
STUB_ALERTS = """
window.__oscillators = 0;
window.__notifications = [];
function FakeParam() {}
FakeParam.prototype.setValueAtTime = function () {};
FakeParam.prototype.exponentialRampToValueAtTime = function () {};
function FakeAudioContext() { this.currentTime = 0; this.destination = {}; }
FakeAudioContext.prototype.resume = function () { return Promise.resolve(); };
FakeAudioContext.prototype.createOscillator = function () {
  window.__oscillators += 1;
  return { type: "", frequency: new FakeParam(), connect: function () {},
           start: function () {}, stop: function () {} };
};
FakeAudioContext.prototype.createGain = function () {
  return { gain: new FakeParam(), connect: function () {} };
};
window.AudioContext = FakeAudioContext;
window.webkitAudioContext = FakeAudioContext;
function FakeNotification(title, opts) { window.__notifications.push(title + " | " + opts.body); }
FakeNotification.permission = "granted";
FakeNotification.requestPermission = function () { return Promise.resolve("granted"); };
window.Notification = FakeNotification;
// Pretend the tab is in the background so the notification path runs.
Object.defineProperty(document, "visibilityState", { get: function () { return "hidden"; } });
"""


def _login(context, base, room):
    staff = context.new_page()
    staff.goto(f"{base}/accounts/login/")
    staff.get_by_label("Username").fill("boss")
    staff.get_by_label("Password").fill(PASSWORD)
    staff.get_by_role("button", name="Log in").click()
    expect(staff.get_by_role("heading", name="Staff dashboard")).to_be_visible()
    staff.goto(f"{base}/staff/counters/{room.pk}/")
    return staff


def test_sse_push_and_almost_your_turn(
    live_server, settings, browser, consult, room1, manager_user
):
    settings.GILIRAN_POLL_MS = 600_000  # updates below can only arrive by the stream
    settings.GILIRAN_ALMOST_AHEAD = 2
    for _ in range(3):
        engine.issue_ticket(consult)
    mine = engine.issue_ticket(consult)  # A-004, 3 people ahead
    base = live_server.url

    phone = browser.new_context(viewport={"width": 390, "height": 780}, locale="en-US")
    phone.add_init_script(STUB_ALERTS)
    customer = phone.new_page()
    customer.goto(f"{base}/t/{mine.token}/")
    ticket = customer.locator("#ticket")
    expect(ticket).to_have_attribute("data-transport", "sse")
    expect(customer.locator('[data-field="ahead"]')).to_have_text("3")
    banner = customer.locator("[data-almost-banner]")
    expect(banner).to_be_hidden()
    announcer = customer.locator("[data-announcer]")

    toggle = customer.get_by_label("Play a sound and notify me when it is almost my turn")
    toggle.check()
    expect(ticket).to_have_attribute("data-sound", "on")
    assert customer.evaluate("window.__oscillators") == 2  # confirmation chime

    staff = _login(browser.new_context(viewport={"width": 1280, "height": 800}), base, room1)
    staff.get_by_role("button", name="Call next").click()  # A-001 -> 2 ahead of us
    expect(customer.locator('[data-field="ahead"]')).to_have_text("2")
    expect(banner).to_be_visible()
    expect(banner).to_have_text("Almost your turn! Please stay near the counters.")
    expect(announcer).to_have_text("Almost your turn. People ahead: 2.")
    expect(ticket).to_have_attribute("data-alert-count", "1")
    assert customer.evaluate("window.__oscillators") == 4
    assert customer.evaluate("window.__notifications") == [
        "🔔 A-004 · giliran | Almost your turn. People ahead: 2."
    ]

    # Another step closer: the page updates, but the alert does not repeat.
    engine.apply_action(Ticket.objects.get(code="A-001"), "done")
    engine.call_next(room1)
    expect(customer.locator('[data-field="ahead"]')).to_have_text("1")
    expect(ticket).to_have_attribute("data-alert-count", "1")
    assert customer.evaluate("window.__oscillators") == 4

    staff.reload()
    staff.get_by_role("button", name="Done").click()
    staff.get_by_role("button", name="Call next").click()  # A-003
    staff.get_by_role("button", name="Done").click()
    staff.get_by_role("button", name="Call next").click()  # ours
    expect(staff.locator('[data-testid="current-code"]')).to_have_text("A-004")
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Called")
    expect(customer.locator('[data-field="counter"]')).to_have_text("Room 1")
    expect(banner).to_be_hidden()
    expect(announcer).to_have_text("Your number A-004 is being called. Please go to Room 1.")
    expect(ticket).to_have_attribute("data-alert-count", "2")
    assert customer.evaluate("window.__oscillators") == 8  # double chime when called
    expect(customer).to_have_title("🔔 A-004 · giliran")

    staff.get_by_role("button", name="Call again").click()
    expect(ticket).to_have_attribute("data-alert-count", "3")
    expect(ticket).to_have_attribute("data-last-alert", "called:1")

    # The stream outlives several server-side closes ("bye" -> reconnect) and still works.
    staff.get_by_role("button", name="Start serving").click()
    staff.get_by_role("button", name="Done").click()
    expect(customer.locator('[data-field="status_label"]')).to_have_text("Done")
    expect(ticket).to_have_attribute("data-transport", "sse")


def test_no_eventsource_falls_back_to_polling(live_server, browser, consult, room1):
    t = engine.issue_ticket(consult)
    context = browser.new_context(locale="en-US")
    context.add_init_script("delete window.EventSource;")
    page = context.new_page()
    page.goto(f"{live_server.url}/t/{t.token}/")
    expect(page.locator("#ticket")).to_have_attribute("data-transport", "poll")
    engine.call_next(room1)
    expect(page.locator('[data-field="status_label"]')).to_have_text("Called")


def test_stream_error_falls_back_to_polling(live_server, browser, branch, consult, room1):
    engine.issue_ticket(consult)
    context = browser.new_context(locale="en-US")
    context.route("**/stream/", lambda route: route.fulfill(status=503, body="down"))
    tv = context.new_page()
    tv.goto(f"{live_server.url}/b/{branch.slug}/board/")
    board = tv.locator("#board")
    expect(board).to_have_attribute("data-transport", "poll")
    engine.call_next(room1)
    expect(tv.locator('[data-list="serving"]')).to_contain_text("A-001")
    expect(board).to_have_attribute("data-live", "ok")


def test_board_updates_by_stream(live_server, browser, settings, branch, consult, room1):
    settings.GILIRAN_POLL_MS = 600_000
    engine.issue_ticket(consult)
    tv = browser.new_context(locale="en-US").new_page()
    tv.goto(f"{live_server.url}/b/{branch.slug}/board/")
    expect(tv.locator("#board")).to_have_attribute("data-transport", "sse")
    expect(tv.locator('[data-list="next"]')).to_contain_text("A-001")
    engine.call_next(room1)
    expect(tv.locator('[data-list="serving"]')).to_contain_text("A-001")
    expect(tv.locator('[data-list="next"]')).not_to_contain_text("A-001")
