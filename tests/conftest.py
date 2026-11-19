from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from django.contrib.auth import get_user_model
from django.utils import translation

from queues.models import Branch, Counter, Membership, Service

MYT = ZoneInfo("Asia/Kuala_Lumpur")
PASSWORD = "test-pass-123"


def myt(*args) -> datetime:
    """Aware datetime in Malaysia time, e.g. myt(2026, 11, 19, 9, 0)."""
    return datetime(*args, tzinfo=MYT)


@pytest.fixture
def branch(db):
    return Branch.objects.create(name="Test Clinic", slug="test-clinic")


@pytest.fixture
def other_branch(db):
    return Branch.objects.create(name="Other Branch", slug="other")


@pytest.fixture
def consult(branch):
    return Service.objects.create(branch=branch, name="Consultation", prefix="A", avg_minutes=5)


@pytest.fixture
def pharmacy(branch):
    return Service.objects.create(branch=branch, name="Pharmacy", prefix="P", avg_minutes=2)


@pytest.fixture
def room1(branch, consult):
    counter = Counter.objects.create(branch=branch, name="Room 1")
    counter.services.set([consult])
    return counter


@pytest.fixture
def room2(branch, consult, pharmacy):
    counter = Counter.objects.create(branch=branch, name="Room 2")
    counter.services.set([consult, pharmacy])
    return counter


def make_user(username, branch=None, role=None, **extra):
    user = get_user_model().objects.create_user(username=username, password=PASSWORD, **extra)
    if branch is not None:
        Membership.objects.create(user=user, branch=branch, role=role)
    return user


@pytest.fixture
def staff_user(branch):
    return make_user("staff", branch, Membership.Role.STAFF)


@pytest.fixture
def manager_user(branch):
    return make_user("boss", branch, Membership.Role.MANAGER)


@pytest.fixture
def outsider(other_branch):
    return make_user("outsider", other_branch, Membership.Role.MANAGER)


@pytest.fixture
def staff_client(client, staff_user):
    client.force_login(staff_user)
    return client


@pytest.fixture
def manager_client(client, manager_user):
    client.force_login(manager_user)
    return client


@pytest.fixture(autouse=True)
def english_by_default():
    """Requests activate a language on the test thread; start each test in English."""
    translation.activate("en")
    yield
    translation.deactivate()


@pytest.fixture(autouse=True)
def fresh_rate_limit_cache():
    """Rate limit counters live in the cache; every test starts with empty ones."""
    from django.core.cache import cache

    cache.clear()
    yield
    cache.clear()
