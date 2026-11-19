import pytest
from django.db import connection
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_healthz_ok(client):
    r = client.get(reverse("healthz"))
    assert r.status_code == 200 and r.content == b"ok"


def test_healthz_reports_database_failure(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("down")

    monkeypatch.setattr(connection, "cursor", boom)
    r = client.get(reverse("healthz"))
    assert r.status_code == 503


def test_loopback_is_always_an_allowed_host(settings):
    # The container healthcheck calls 127.0.0.1 from inside the container.
    assert "127.0.0.1" in settings.ALLOWED_HOSTS or "*" in settings.ALLOWED_HOSTS
