"""Per-IP rate limit on the public join and booking forms."""

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from queues.models import Booking, Slot, Ticket
from queues.ratelimit import client_ip

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def limit(settings):
    settings.GILIRAN_RATE_LIMIT_MAX = 3
    settings.GILIRAN_RATE_LIMIT_WINDOW = 60
    settings.GILIRAN_TRUST_PROXY = False


def join(client, branch, consult, **extra):
    return client.post(reverse("join", args=[branch.slug]), {"service": consult.pk}, **extra)


def test_join_is_limited_per_ip_with_friendly_429(client, branch, consult):
    for _ in range(3):
        assert join(client, branch, consult).status_code == 302
    r = join(client, branch, consult)
    assert r.status_code == 429
    assert r["Retry-After"] == "60"
    html = r.content.decode()
    assert "Please slow down" in html and "60 seconds" in html
    assert Ticket.objects.count() == 3  # the blocked request created nothing


def test_other_ip_is_not_affected(client, branch, consult):
    for _ in range(4):
        join(client, branch, consult, REMOTE_ADDR="10.0.0.1")
    assert join(client, branch, consult, REMOTE_ADDR="10.0.0.1").status_code == 429
    assert join(client, branch, consult, REMOTE_ADDR="10.0.0.2").status_code == 302


def test_viewing_pages_is_never_limited(client, branch, consult):
    for _ in range(10):
        assert client.get(reverse("join", args=[branch.slug])).status_code == 200
        assert client.get(reverse("book", args=[branch.slug])).status_code == 200


def test_limit_is_per_endpoint(client, branch, consult):
    for _ in range(4):
        join(client, branch, consult)
    assert join(client, branch, consult).status_code == 429
    # Booking has its own counter.
    assert client.post(reverse("book", args=[branch.slug]), {}).status_code != 429


def test_booking_is_limited(client, branch, consult):
    slot = Slot.objects.create(
        service=consult,
        starts_at=timezone.now() + timedelta(hours=3),
        ends_at=timezone.now() + timedelta(hours=4),
        capacity=50,
    )
    url = reverse("book", args=[branch.slug])
    query = f"?service={consult.pk}&day={timezone.localdate(slot.starts_at).isoformat()}"
    payload = {"slot": slot.pk, "nickname": "Guest", "phone": "000-000 0001"}
    codes = []
    for n in range(5):
        payload["phone"] = f"000-000 {n:04d}"
        codes.append(client.post(url + query, payload).status_code)
    assert codes == [302, 302, 302, 429, 429]
    assert Booking.objects.count() == 3
    assert "Please slow down" in client.post(url + query, payload).content.decode()


def test_window_expiry_resets_the_count(client, branch, consult):
    for _ in range(4):
        join(client, branch, consult)
    assert join(client, branch, consult).status_code == 429
    cache.clear()  # what the cache does when the window key expires
    assert join(client, branch, consult).status_code == 302


def test_zero_disables_the_limit(client, branch, consult, settings):
    settings.GILIRAN_RATE_LIMIT_MAX = 0
    assert all(join(client, branch, consult).status_code == 302 for _ in range(8))


def test_limit_is_configurable(client, branch, consult, settings):
    settings.GILIRAN_RATE_LIMIT_MAX = 1
    assert join(client, branch, consult).status_code == 302
    assert join(client, branch, consult).status_code == 429


def test_forwarded_header_only_trusted_behind_proxy(client, branch, consult, settings):
    headers = {"HTTP_X_FORWARDED_FOR": "203.0.113.9, 198.51.100.7"}
    # Not trusted: every request counts for REMOTE_ADDR, spoofed headers change nothing.
    for n in range(4):
        join(client, branch, consult, HTTP_X_FORWARDED_FOR=f"1.1.1.{n}")
    assert join(client, branch, consult, **headers).status_code == 429
    cache.clear()
    settings.GILIRAN_TRUST_PROXY = True
    for _ in range(4):
        join(client, branch, consult, **headers)
    assert join(client, branch, consult, **headers).status_code == 429
    # A different last hop (another client behind the same proxy) has its own budget.
    other = {"HTTP_X_FORWARDED_FOR": "203.0.113.9, 198.51.100.8"}
    assert join(client, branch, consult, **other).status_code == 302


def test_client_ip_falls_back_to_remote_addr(rf, settings):
    settings.GILIRAN_TRUST_PROXY = True
    request = rf.get("/", REMOTE_ADDR="192.0.2.5")
    assert client_ip(request) == "192.0.2.5"
    assert client_ip(rf.get("/", HTTP_X_FORWARDED_FOR=" ,", REMOTE_ADDR="192.0.2.5")) == "192.0.2.5"


def test_429_page_is_translated(client, branch, consult):
    for _ in range(3):
        join(client, branch, consult)
    for lang, text in (("ms", "Sila perlahankan"), ("zh-hans", "请稍等一下")):
        client.cookies["django_language"] = lang
        assert text in join(client, branch, consult).content.decode()
