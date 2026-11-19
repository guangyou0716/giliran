# giliran

[![CI](https://github.com/guangyou0716/giliran/actions/workflows/ci.yml/badge.svg)](https://github.com/guangyou0716/giliran/actions/workflows/ci.yml)

**Walk-in queue + live "now serving" display for small clinics, pharmacies and service counters.**
English · Bahasa Melayu · 简体中文. *Giliran* is Malay for "turn".

Customers scan a QR code, pick a service and get a number on their phone (no account, no app), or book a
time slot ahead and check in when they arrive. The page shows
how many people are ahead and an estimated wait, and updates by itself when they are called. Staff run
each counter from a tablet or PC ("Call next", "Call again", "No show", "Done"), and a TV in the waiting room shows
which number goes to which room.

| Phone: take a number | Phone: called | Counter console |
|---|---|---|
| ![Join page on a phone](docs/screenshots/join-phone.png) | ![Ticket page after being called](docs/screenshots/ticket-called-phone.png) | ![Staff counter console](docs/screenshots/counter-console.png) |

![Waiting-room display board](docs/screenshots/display-board.png)

| Phone: book a time | Phone: booking link | Staff: today's appointments |
|---|---|---|
| ![Booking page with free time slots](docs/screenshots/book-phone.png) | ![Booking page with reference and check-in button](docs/screenshots/booking-phone.png) | ![Staff appointments list with check-in buttons](docs/screenshots/appointments-staff.png) |

Screenshots are taken by the Playwright end-to-end test from a local run on synthetic seed data
(`GILIRAN_SCREENSHOTS=docs/screenshots pytest -m e2e`).

## Features

- **Join from the phone** (`/b/<branch>/join/`): choose a service, optional nickname (no IC / phone collected),
  get a ticket like `A-007`. Numbers restart at 1 per service per Malaysia-time day.
- **Live ticket page** (`/t/<secret token>/`): status, people ahead, estimated wait, "Please go to Room 1"
  when called, cancel while waiting. Updates by polling a JSON endpoint; works without JavaScript (reload).
- **Counter console** for staff: call next (priority tickets first, then first come first served), call again,
  start serving, done, no-show; add walk-ins at the desk with a priority flag (elderly, disabled, pregnant, urgent).
- **Display board** (`/b/<branch>/board/`) for a TV: now serving per counter, next numbers, waiting per service.
- **Appointments** (`/b/<branch>/book/`): pick a service, a day (next 7 days) and a free time slot, enter a
  nickname and a phone number (no account). The secret booking link (`/a/<token>/`) shows the reference, lets the
  customer cancel (frees the seat) and check in from 30 minutes before the slot; check-in turns the booking into a
  queue ticket. Staff see the day's bookings (`/staff/b/<branch>/appointments/`, search by reference, nickname or last
  4 phone digits; phones are shown masked) and can check customers in at the counter. Missed bookings become
  no-shows 10 minutes after their time (`python manage.py expire_bookings` from cron; the staff page also does it).
- **Slot management** for managers: generate slots for a service and day (from / until / minutes per slot / seats),
  change seats (never below what is booked) or close a slot, delete slots without bookings.
- **Ticket history** with search (ticket code / nickname), filters (status, service, day) and pagination.
- **Manager pages**: create / edit services (prefix, average minutes, active) and counters (which services each serves).
- **Roles per branch**: `manager` and `staff` memberships; Django superuser = manager everywhere. Staff of one
  branch cannot see or act on another branch.
- **Audit trail**: every state change is a `TicketEvent` (who, which counter, from -> to, when).
- **i18n**: all UI strings in English, Bahasa Melayu and Simplified Chinese (a test fails if one is missing);
  language switcher in the header.
- **Accessibility**: semantic forms and tables, `aria-live` status regions, skip link, visible focus,
  44 px touch targets, reduced-motion support.

## How the queue stays correct

All state changes go through [`queues/engine.py`](queues/engine.py):

```
waiting --call--> called --start--> serving --done--> done
called  --recall--> called          called  --done--> done
called  --no_show--> no_show        waiting --cancel--> cancelled
```

- Ticket numbers come from a per-service, per-day `DailySequence` row locked with `SELECT ... FOR UPDATE`, so
  parallel joins never share a number.
- "Call next" locks the counter row and picks the next waiting ticket with `FOR UPDATE SKIP LOCKED`, so two
  counters pressing the button together get different tickets.
- A partial unique constraint guarantees a counter holds at most one called / serving ticket.
- Estimated wait = `ceil(people ahead x average minutes / active counters serving the service)`.

Appointments:

- **No overbooking.** Booking locks the slot row (`SELECT ... FOR UPDATE`) and takes a seat with a conditional
  `UPDATE ... SET booked_count = booked_count + 1 WHERE booked_count < capacity`; cancelling gives it back under
  the same lock. A database check constraint (`booked_count <= capacity`) rejects any write that would overbook,
  whatever code path makes it, and a partial unique constraint stops one phone number holding two seats in a slot.
- **Fair interleaving with walk-ins.** Waiting tickets are called in this order:
  1. priority tickets (elderly, disabled, pregnant, urgent);
  2. booked tickets whose slot time has come, if they checked in no later than 10 minutes after it;
  3. everyone else.

  Within a group: by queue time (slot time for on-time bookings, join time for walk-ins), then ticket id. Arriving
  early does not jump the queue: an early booked customer waits as if they had joined at their slot time. A late
  check-in (after the grace window, staff only) is queued like a walk-in from that moment. Managers keep walk-ins
  moving by giving slots fewer seats than the counters can serve.

The locking rules have concurrency tests that run real threads against PostgreSQL in CI (parallel joins,
parallel "Call next", 10 people booking 3 seats at once, cancels racing new bookings).

## Stack

Python 3.12+ · Django 5.2 · PostgreSQL 16 (SQLite for quick local runs) · server-rendered templates + plain CSS
+ a small vanilla JS file (no CDN, no build step) · pytest + pytest-django · Playwright · ruff · GitHub Actions.

## Run locally

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # edit, then export the variables (or set them in your shell)
# PostgreSQL:  export DATABASE_URL=postgres://giliran:giliran@localhost:5432/giliran
# or leave DATABASE_URL unset to use SQLite (db.sqlite3)
python manage.py migrate
GILIRAN_DEMO_PASSWORD=choose-a-demo-password python manage.py seed_demo
python manage.py runserver
```

Open http://127.0.0.1:8000/ . The seed also creates half-hour appointment slots (09:00-17:00, 2 seats) for today
and the next two days, plus a few bookings with clearly fake phone numbers (`000-000 01xx`).
Appointment timing is configurable with `GILIRAN_BOOKING_GRACE_MIN` (10), `GILIRAN_CHECKIN_EARLY_MIN` (30) and
`GILIRAN_BOOKING_DAYS` (7); run `python manage.py expire_bookings` every few minutes from cron to mark no-shows. Demo accounts (all synthetic, password = `GILIRAN_DEMO_PASSWORD`):

| User | Role |
|---|---|
| `admin` | superuser (manager of every branch, Django admin) |
| `manager` / `staff1`, `staff2` | manager / counter staff at *Klinik Demo Seri Mawar* |
| `manager2` / `staff3` | manager / counter staff at *Demo Service Centre Cempaka* |

Translations: `.po` files live in `locale/`; compiled `.mo` files are committed so the app runs without GNU gettext.
After editing a `.po` file run `python scripts/messages.py --write` (Babel); CI runs `python scripts/messages.py`
to check that every string is translated and the `.mo` files are current.

## Tests

```bash
ruff check . && ruff format --check .
python scripts/messages.py               # translations complete and compiled
pytest -m "not e2e"                      # unit + integration (engine, views, API, i18n, seed, concurrency)
python -m playwright install chromium
pytest -m e2e                            # browser: join -> call -> board -> history; book -> check in -> call
```

Set `DATABASE_URL` to a PostgreSQL database to also run the concurrency tests (they are skipped on SQLite).

## Roadmap

1. ~~Appointments~~ (done): slots with capacity, booking link, check-in into the queue, no-shows.
2. Reports: waits, service time and no-show rate per service / hour, CSV export.
3. Server-Sent Events instead of polling; "almost your turn" notification and chime.
4. Docker Compose (web + PostgreSQL), transfer a ticket between services, counter pause, axe accessibility audit.

## License

MIT
