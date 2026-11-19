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

![Manager reports: totals, per service table and per hour bar table](docs/screenshots/reports.png)

Screenshots are taken by the Playwright end-to-end test from a local run on synthetic seed data
(`GILIRAN_SCREENSHOTS=docs/screenshots pytest -m e2e`).

## Features

- **Join from the phone** (`/b/<branch>/join/`): choose a service, optional nickname (no IC / phone collected),
  get a ticket like `A-007`. Numbers restart at 1 per service per Malaysia-time day.
- **Live ticket page** (`/t/<secret token>/`): status, people ahead, estimated wait, "Please go to Room 1"
  when called, cancel while waiting. Updates are pushed live by Server-Sent Events, with polling as the fallback;
  works without JavaScript (reload). See [Live updates](#live-updates).
- **"Almost your turn" alert**: when 2 or fewer people are ahead (`GILIRAN_ALMOST_AHEAD`) and again when the
  number is called (or called again), the page shows a banner and announces it to screen readers; after the
  customer ticks *Play a sound and notify me*, it also plays a short chime and shows a notification.
- **Counter console** for staff: call next (priority tickets first, then first come first served), call again,
  start serving, done, no-show; add walk-ins at the desk with a priority flag (elderly, disabled, pregnant, urgent).
- **Display board** (`/b/<branch>/board/`) for a TV: now serving per counter, next numbers, waiting per service;
  updated live like the ticket page.
- **Appointments** (`/b/<branch>/book/`): pick a service, a day (next 7 days) and a free time slot, enter a
  nickname and a phone number (no account). The secret booking link (`/a/<token>/`) shows the reference, lets the
  customer cancel (frees the seat) and check in from 30 minutes before the slot; check-in turns the booking into a
  queue ticket. Staff see the day's bookings (`/staff/b/<branch>/appointments/`, search by reference, nickname or last
  4 phone digits; phones are shown masked) and can check customers in at the counter. Missed bookings become
  no-shows 10 minutes after their time (`python manage.py expire_bookings` from cron; the staff page also does it).
- **Slot management** for managers: generate slots for a service and day (from / until / minutes per slot / seats),
  change seats (never below what is booked) or close a slot, delete slots without bookings.
- **Transfer a ticket** to another service (staff, at the counter console, for the ticket being served or any waiting
  one): see [Transfers, pause, limits and privacy](#transfers-pause-limits-and-privacy).
- **Pause a counter** (break): a paused counter is not offered the next ticket; the console, dashboard and display
  board show it as paused.
- **Ticket history** with search (ticket code / nickname), filters (status, service, day) and pagination.
- **Reports** for managers (`/manage/b/<branch>/reports/`): for a date range (up to 92 days, Malaysia time) and
  optionally one service: tickets, served, average and median wait, average service time, no-shows and no-show
  rate, customers who left the queue and bookings cancelled; one table per service and a bar table per hour of
  day; **CSV export** (see [Reports](#reports)).
- **Manager pages**: create / edit services (prefix, average minutes, active) and counters (which services each serves).
- **Roles per branch**: `manager` and `staff` memberships; Django superuser = manager everywhere. Staff of one
  branch cannot see or act on another branch.
- **Audit trail**: every state change is a `TicketEvent` (who, which counter, from -> to, when).
- **i18n**: all UI strings in English, Bahasa Melayu and Simplified Chinese (a test fails if one is missing);
  language switcher in the header.
- **Accessibility**: semantic forms and tables, `aria-live` status regions, skip link, visible focus,
  44 px touch targets, reduced-motion support; an [axe-core audit](#accessibility-audit) runs in the browser tests.
- **Abuse and privacy controls**: per-IP rate limit on joining and booking (HTTP 429 with a friendly page) and a
  phone-number retention purge for old bookings.

## Live updates

The ticket page and the display board open a [Server-Sent Events](https://developer.mozilla.org/docs/Web/API/Server-sent_events)
stream (`EventSource`):

| Page | Stream | Same JSON as |
|---|---|---|
| `/t/<token>/` | `/api/tickets/<token>/stream/` (only with the secret ticket token) | `/api/tickets/<token>/` |
| `/b/<branch>/board/` | `/api/branches/<branch>/board/stream/` (public board fields only: codes, counters, counts) | `/api/branches/<branch>/board/` |

- The stream sends the current state at once. Then, every 1.5 s (`GILIRAN_SSE_TICK_SECONDS`), it reads a cheap change
  marker (the newest ticket event id of the branch: every issue / call / done / cancel writes one) and rebuilds the
  payload only when the marker moved, or every 10 s anyway (`GILIRAN_SSE_REFRESH_SECONDS`, because estimates also
  depend on the clock and on counter settings). A message is sent only when the payload changed; a `: ping` comment
  every 15 s of silence keeps proxies from closing the connection.
- Streams are bounded for plain sync Django: after 60 s (`GILIRAN_SSE_MAX_SECONDS`) the server sends `event: bye`
  and closes, and the page reconnects at once. A finished ticket (done, no-show, cancelled) gets `event: end` and the
  page stops. Responses carry `Cache-Control: no-cache` and `X-Accel-Buffering: no` (nginx does not buffer them).
- **Fallback**: without `EventSource`, when the server refuses the stream (e.g. `GILIRAN_SSE=0` -> 404, or a proxy
  error), or after 3 failed reconnects in a row, the page polls the JSON endpoint every `GILIRAN_POLL_MS` (5000)
  instead. Without JavaScript the page still shows the state on reload.
- **Deployment**: each open stream holds a worker thread for up to a minute. Run gunicorn with threads, e.g.
  `gunicorn config.wsgi --worker-class gthread --workers 2 --threads 32`, size the threads for the expected number
  of open ticket pages and boards, or set `GILIRAN_SSE=0` to use polling only.
  The streams are plain synchronous generators for WSGI; under ASGI Django would buffer them, so set
  `GILIRAN_SSE=0` if you serve the app with an ASGI server.

**Sound and notifications.** Browsers only play audio after a user gesture, so the ticket page has a checkbox
*Play a sound and notify me when it is almost my turn*. Ticking it creates the audio context (a short test chime, made
with WebAudio oscillators, no audio file) and asks for notification permission. Alerts fire once per change (2 or
fewer ahead, called, called again), not on every update, and not again after a reload: a single chime when it is
almost your turn, a double chime when called, a vibration where the phone supports it, a notification while the tab
is in the background (if allowed; some mobile browsers do not allow page notifications), and an `aria-live`
announcement such as "Your number A-004 is being called. Please go to Room 1." The banner and the announcement work
even when sound is off.

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

## Reports

All days and hours are Malaysia time. A ticket counts on the day and hour it was taken (check-in time for
appointments); a booking counts on its slot's day and hour.

| Number | Definition |
|---|---|
| Wait | first call - ticket taken (or checked in). `Ticket.first_called_at` is set once, so "Call again" does not reset it |
| Service time | Done - Start serving, for tickets that were started |
| No-shows | tickets marked no-show + bookings never checked in (no-show, or still "booked" after start + grace) |
| No-show rate | no-shows / (tickets + missed bookings) |
| Left queue / bookings cancelled | tickets the customer cancelled / bookings cancelled in advance (seat freed) |

The page runs a fixed number of queries whatever the range or number of services: one grouped aggregate over
tickets (per service x hour: counts, summed waits and service times), one list of waits for the medians and one
grouped aggregate over bookings; totals are rolled up in Python (a test pins the query count). The CSV
(`?format=csv`) follows RFC 4180 (CRLF, quoted fields), starts with a UTF-8 BOM so Excel shows Malay and Chinese
names correctly, keeps English column names for scripts, and prefixes any text cell starting with `= + - @`,
tab or CR with `'` so a spreadsheet never runs it as a formula. `seed_demo` writes 14 days of synthetic history
(fixed random seed) so the page has data on a fresh install.

## Transfers, pause, limits and privacy

**Transfer between services.** On the counter console every waiting ticket and the current ticket has *Transfer to
another service* (staff or manager of the branch; today's tickets only; not for finished tickets). The ticket goes back
to *waiting* in the other service with a **new number and code** from that service's daily sequence (numbers stay unique
per service and day), and the counter it held is freed. It **keeps its queue time**, priority and appointment time, so
it takes its place by arrival time: it neither goes to the back nor jumps ahead of people who arrived before it. Calls and
service times of the first service are cleared (the wait in reports runs from arrival to the final call). Every
transfer is a `TicketEvent` with action `transfer`, the actor, the old counter and a note such as `A-003 -> P-004`; the
ticket page and board follow within a second or two because the live streams watch those events (the ticket page
switches to the new number and alerts again when it is called).

**Pause a counter.** *Pause counter* (not possible while it holds a ticket) stops "Call next" there until *Resume
counter*; the console shows a *Paused* badge, the dashboard lists it as paused and the display board shows
"Paused: Room 1". A paused counter does not count in wait estimates (no active counter left means no estimate). The
board picks the change up at its next refresh (10 s at most).

**Rate limit.** `POST` to the public join (`/b/<branch>/join/`) and booking (`/b/<branch>/book/`) forms is limited per
client IP: `GILIRAN_RATE_LIMIT_MAX` (10) requests per `GILIRAN_RATE_LIMIT_WINDOW` (60) seconds, counted per
endpoint in the Django cache; past that the visitor gets HTTP 429 with a friendly page and a `Retry-After` header.
Viewing pages is never limited; `GILIRAN_RATE_LIMIT_MAX=0` turns it off. The default cache is process memory, so the
limit is per worker process (the Docker image runs one process with many threads); use a shared cache backend if you run
several. Behind a reverse proxy set `GILIRAN_TRUST_PROXY=1` so the last `X-Forwarded-For` entry (added by your proxy) is
the client address; do not enable it when clients can reach the app directly.

**Phone retention.** Booking phone numbers are the only phone numbers stored (tickets keep at most a nickname).
`python manage.py purge_phones` blanks the phone of every booking whose slot started more than
`GILIRAN_PHONE_RETENTION_DAYS` (30) days ago (or `--days N`); reference, nickname, status and times stay so reports keep
working. It is idempotent (already blank rows are not touched; the second run reports 0), so run it daily from cron:
`15 3 * * * cd /app && python manage.py purge_phones`. Blank phones show as "—" on staff screens.

## Docker Compose

Needs Docker with Compose v2. Web (Django + gunicorn, static files by WhiteNoise) and PostgreSQL 16:

```bash
cp .env.example .env
# edit .env: set DJANGO_SECRET_KEY (python -c "import secrets; print(secrets.token_urlsafe(50))"),
# and for demo data GILIRAN_SEED_DEMO=1 plus GILIRAN_DEMO_PASSWORD=choose-a-demo-password
docker compose up --build
```

Open http://localhost:8000/ (`GILIRAN_PORT` changes the port). The `web` container waits for the database healthcheck,
applies migrations, optionally seeds the synthetic demo data (`GILIRAN_SEED_DEMO=1`, idempotent) and starts gunicorn
with one process and 32 threads (`GUNICORN_THREADS`; every open live stream uses a thread). Its healthcheck calls
`/healthz/` (answers 200 `ok` when the database responds, 503 otherwise). Data lives in the `giliran-db` volume;
`docker compose down -v` deletes it. Notes: Compose sets `DJANGO_SECURE_COOKIES=0` so login works over plain
`http://localhost`; behind HTTPS set it to 1 and set `DJANGO_ALLOWED_HOSTS` and `DJANGO_CSRF_TRUSTED_ORIGINS`.
Run the daily purge with `docker compose exec web python manage.py purge_phones` (cron on the host). CI builds the image,
starts the stack and smoke-tests it on every push. No credentials are baked into the image (the `build-only` key used
for `collectstatic` exists only in that build layer).

## Accessibility audit

`pytest -m e2e` runs [axe-core](https://github.com/dequelabs/axe-core) (shipped with the `axe-playwright-python` dev
dependency) in Chromium on the key pages: home, join, booking (choose and times), booking link, ticket, display board,
rate-limit page and staff login in English, Bahasa Melayu and Simplified Chinese, plus the staff dashboard and counter
console. The test fails on any **serious or critical** violation and prints the rule, selector and fix hint. The first
run found four real problems that are fixed: a select without a label in the walk-in form, low contrast of the counter name on the
board, a `<ul role="status">` that broke list semantics, and (found by the transfer test) the ticket page not
updating its number after a transfer. Automated checks find only part of all accessibility problems, so keyboard and
screen reader checks are still worth doing by hand.

## Stack

Python 3.12+ · Django 5.2 · PostgreSQL 16 (SQLite for quick local runs) · gunicorn + WhiteNoise · Docker Compose ·
server-rendered templates + plain CSS + a small vanilla JS file (no CDN, no build step) · pytest + pytest-django ·
Playwright + axe-core · ruff · GitHub Actions.

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
and the next two days, plus a few bookings with clearly fake phone numbers (`000-000 01xx`), and 14 days of finished tickets and past
appointments for the reports.
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
pytest -m "not e2e"                      # unit + integration (engine, views, API, streams, reports, i18n, seed, concurrency)
python -m playwright install chromium
pytest -m e2e                            # browser: join -> call -> board -> history; book -> check in -> call;
                                         # manager reports -> filter -> CSV download; live push (SSE) with the
                                         # "almost your turn" alert (audio stubbed) and the polling fallback;
                                         # pause / resume and ticket transfer; axe accessibility audit
```

Set `DATABASE_URL` to a PostgreSQL database to also run the concurrency tests (they are skipped on SQLite).

## Roadmap

1. ~~Appointments~~ (done): slots with capacity, booking link, check-in into the queue, no-shows.
2. ~~Reports~~ (done): waits, service time and no-show rate per service / hour, CSV export.
3. ~~Live updates~~ (done): Server-Sent Events with polling fallback; "almost your turn" alert and chime.
4. ~~Docker Compose, ticket transfer, counter pause, rate limits, phone retention purge, axe audit~~ (done; the MVP
   is feature complete).

Ideas beyond the MVP: SMS / WhatsApp "you are next" messages, per-service opening hours, a shared cache for the rate
limit across processes, optional CAPTCHA, a daily trend chart in reports.

## License

MIT
