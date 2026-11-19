import pytest
from django.contrib.auth import authenticate
from django.core.management import call_command

from queues.models import Branch, Counter, Membership, Service, Ticket

pytestmark = pytest.mark.django_db


def test_seed_demo_is_idempotent():
    call_command("seed_demo", password="seed-pass-123", verbosity=0)
    counts = (
        Branch.objects.count(),
        Service.objects.count(),
        Counter.objects.count(),
        Membership.objects.count(),
        Ticket.objects.count(),
    )
    assert counts == (2, 5, 5, 5, 24)
    call_command("seed_demo", password="seed-pass-123", verbosity=0)
    assert (
        Branch.objects.count(),
        Service.objects.count(),
        Counter.objects.count(),
        Membership.objects.count(),
        Ticket.objects.count(),
    ) == counts


def test_seed_roles_and_login():
    call_command("seed_demo", password="seed-pass-123", no_tickets=True, verbosity=0)
    assert Ticket.objects.count() == 0
    assert authenticate(username="manager", password="seed-pass-123") is not None
    roles = dict(Membership.objects.values_list("user__username", "role"))
    assert roles == {
        "manager": "manager",
        "staff1": "staff",
        "staff2": "staff",
        "manager2": "manager",
        "staff3": "staff",
    }


def test_seed_generates_password_only_for_new_accounts(monkeypatch, capsys):
    monkeypatch.delenv("GILIRAN_DEMO_PASSWORD", raising=False)
    call_command("seed_demo", no_tickets=True)
    out = capsys.readouterr().out
    generated = out.rsplit(": ", 1)[1].strip()
    assert "Generated password for new demo accounts" in out
    assert authenticate(username="staff3", password=generated) is not None
    # Second run creates nobody, so it must not print a password that was never applied.
    call_command("seed_demo", no_tickets=True)
    assert "Generated password" not in capsys.readouterr().out
    assert authenticate(username="staff3", password=generated) is not None
