# giliran

[![CI](https://github.com/guangyou0716/giliran/actions/workflows/ci.yml/badge.svg)](https://github.com/guangyou0716/giliran/actions/workflows/ci.yml)

**Walk-in queue + live "now serving" display for small clinics, pharmacies and service counters.**
English · Bahasa Melayu · 简体中文. *Giliran* is Malay for "turn".

Customers scan a QR code, pick a service and get a number on their phone (no account, no app). The page shows
how many people are ahead and an estimated wait, and updates by itself when they are called. Staff run
each counter from a tablet or PC ("Call next", "Call again", "No show", "Done"), and a TV in the waiting room shows
which number goes to which room.

| Phone: take a number | Phone: called | Counter console |
|---|---|---|
| ![Join page on a phone](docs/screenshots/join-phone.png) | ![Ticket page after being called](docs/screenshots/ticket-called-phone.png) | ![Staff counter console](docs/screenshots/counter-console.png) |

![Waiting-room display board](docs/screenshots/display-board.png)

Screenshots are taken by the Playwright end-to-end test from a local run on synthetic seed data
(`GILIRAN_SCREENSHOTS=docs/screenshots pytest -m e2e`).

## Features (v0.1)

- **Join from the phone** (`/b/<branch>/join/`): choose a service, optional nickname (no IC / phone collected),
  get a ticket like `A-007`. Numbers restart at 1 per service per Malaysia-time day.
- **Live ticket page** (`/t/<secret token>/`): status, people ahead, estimated wait, "Please go to Room 1"
  when called, cancel while waiting. Updates by polling a JSON endpoint; works without JavaScript (reload).
- **Counter console** for staff: call next (priority tickets first, then first come first served), call again,
  start serving, done, no-show; add walk-ins at the desk with a priority flag (elderly, disabled, pregnant, urgent).
- **Display board** (`/b/<branch>/board/`) for a TV: now serving per counter, next numbers, waiting per service.
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

Both locking rules have concurrency tests that run real threads against PostgreSQL in CI.

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

Open http://127.0.0.1:8000/ . Demo accounts (all synthetic, password = `GILIRAN_DEMO_PASSWORD`):

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
pytest -m e2e                            # browser: join -> call -> live update -> board -> history
```

Set `DATABASE_URL` to a PostgreSQL database to also run the concurrency tests (they are skipped on SQLite).

## Roadmap

1. Appointments: bookable slots per service with capacity, check-in that turns a booking into a ticket.
2. Reports: waits, service time and no-show rate per service / hour, CSV export.
3. Server-Sent Events instead of polling; "almost your turn" notification and chime.
4. Docker Compose (web + PostgreSQL), transfer a ticket between services, counter pause, axe accessibility audit.

## License

MIT
