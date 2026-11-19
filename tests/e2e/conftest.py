import os

import pytest

# Playwright's sync API runs an event loop in the test thread; the Django ORM calls
# made by fixtures in that thread are still synchronous and safe here.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")


@pytest.fixture(autouse=True)
def fast_polling(settings):
    settings.GILIRAN_POLL_MS = 400


@pytest.fixture
def screenshot_dir():
    path = os.environ.get("GILIRAN_SCREENSHOTS")
    if path:
        os.makedirs(path, exist_ok=True)
    return path
