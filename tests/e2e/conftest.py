import os
import threading
import time

import pytest

# Playwright's sync API runs an event loop in the test thread; the Django ORM calls
# made by fixtures in that thread are still synchronous and safe here.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


@pytest.fixture(autouse=True)
def fast_polling(settings):
    settings.GILIRAN_POLL_MS = 400
    # Short live streams so server threads finish quickly (browsers reconnect on "bye").
    settings.GILIRAN_SSE_MAX_SECONDS = 4
    settings.GILIRAN_SSE_TICK_SECONDS = 0.3
    settings.GILIRAN_SSE_HEARTBEAT_SECONDS = 1


@pytest.fixture
def screenshot_dir():
    path = os.environ.get("GILIRAN_SCREENSHOTS")
    if path:
        os.makedirs(path, exist_ok=True)
    return path


@pytest.fixture(autouse=True)
def close_live_streams(browser):
    """Close every browser context after a test and wait for the live server's request
    threads (open event streams) to end, so no thread still holds a database connection
    when the test database is flushed or dropped."""
    yield
    for context in list(browser.contexts):
        context.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and any(
        "process_request_thread" in t.name for t in threading.enumerate()
    ):
        time.sleep(0.1)
