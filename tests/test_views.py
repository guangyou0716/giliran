"""HTTP flows: public pages, staff console, manager CRUD, history search, access control."""

import pytest
from django.urls import reverse

from queues import engine
from queues.models import Counter, Service, Ticket

from .conftest import PASSWORD

S = Ticket.Status
pytestmark = pytest.mark.django_db


# ---- public -------------------------------------------------------------


def test_home_lists_branches(client, branch):
    r = client.get(reverse("home"))
    assert r.status_code == 200
    assert "Test Clinic" in r.content.decode()


def test_join_creates_ticket_and_redirects(client, branch, consult):
    r = client.post(reverse("join", args=[branch.slug]), {"service": consult.pk, "nickname": "Mei"})
    t = Ticket.objects.get()
    assert r.status_code == 302 and r.url == reverse("ticket", args=[t.token])
    assert (t.code, t.nickname, t.source, t.priority) == ("A-001", "Mei", "self", False)


def test_join_ignores_priority_field_from_public(client, branch, consult):
    client.post(reverse("join", args=[branch.slug]), {"service": consult.pk, "priority": "on"})
    assert Ticket.objects.get().priority is False


def test_join_requires_service_from_this_branch(client, branch, consult, other_branch):
    foreign = Service.objects.create(branch=other_branch, name="X", prefix="X")
    r = client.post(reverse("join", args=[branch.slug]), {"service": foreign.pk})
    assert r.status_code == 200
    assert Ticket.objects.count() == 0
    r = client.post(reverse("join", args=[branch.slug]), {})
    assert r.status_code == 200 and Ticket.objects.count() == 0


def test_join_shows_waiting_counts(client, branch, consult):
    engine.issue_ticket(consult)
    engine.issue_ticket(consult)
    assert "2 waiting" in client.get(reverse("join", args=[branch.slug])).content.decode()


def test_join_closed_branch(client, branch, consult):
    branch.is_open = False
    branch.save()
    r = client.get(reverse("join", args=[branch.slug]))
    assert "not taking new tickets" in r.content.decode()
    r = client.post(reverse("join", args=[branch.slug]), {"service": consult.pk}, follow=True)
    assert Ticket.objects.count() == 0
    assert "not taking new tickets" in r.content.decode()


def test_unknown_branch_404(client, db):
    assert client.get(reverse("join", args=["nope"])).status_code == 404
    assert client.get(reverse("board", args=["nope"])).status_code == 404


def test_ticket_page_and_cancel(client, consult, room1):
    engine.issue_ticket(consult)
    t = engine.issue_ticket(consult)
    r = client.get(reverse("ticket", args=[t.token]))
    body = r.content.decode()
    assert "A-002" in body and "Cancel my ticket" in body
    r = client.post(reverse("ticket_cancel", args=[t.token]), follow=True)
    t.refresh_from_db()
    assert t.status == S.CANCELLED
    assert "Your ticket was cancelled." in r.content.decode()
    r = client.post(reverse("ticket_cancel", args=[t.token]), follow=True)
    assert "can no longer be cancelled" in r.content.decode()


def test_cancel_requires_post(client, consult):
    t = engine.issue_ticket(consult)
    assert client.get(reverse("ticket_cancel", args=[t.token])).status_code == 405


def test_board_page(client, branch, consult, room1):
    engine.issue_ticket(consult)
    engine.call_next(room1)
    body = client.get(reverse("board", args=[branch.slug])).content.decode()
    assert "A-001" in body and "Room 1" in body and "Now serving" in body


# ---- access control -------------------------------------------------------


@pytest.mark.parametrize(
    "name,args",
    [
        ("staff:dashboard", []),
        ("staff:history", ["test-clinic"]),
        ("manage:settings", ["test-clinic"]),
        ("manage:service_create", ["test-clinic"]),
    ],
)
def test_anonymous_redirected_to_login(client, branch, name, args):
    r = client.get(reverse(name, args=args))
    assert r.status_code == 302 and r.url.startswith(reverse("login"))


def test_anonymous_cannot_use_counter(client, room1):
    r = client.post(reverse("staff:counter_action", args=[room1.pk]), {"action": "call_next"})
    assert r.status_code == 302 and r.url.startswith(reverse("login"))


def test_staff_of_other_branch_forbidden(client, outsider, branch, room1):
    client.force_login(outsider)
    assert client.get(reverse("staff:counter", args=[room1.pk])).status_code == 403
    assert client.get(reverse("staff:history", args=[branch.slug])).status_code == 403
    assert client.get(reverse("manage:settings", args=[branch.slug])).status_code == 403
    r = client.post(reverse("staff:counter_action", args=[room1.pk]), {"action": "call_next"})
    assert r.status_code == 403


def test_staff_cannot_manage(staff_client, branch, consult):
    assert staff_client.get(reverse("manage:settings", args=[branch.slug])).status_code == 403
    r = staff_client.post(
        reverse("manage:service_edit", args=[branch.slug, consult.pk]),
        {"name": "Hacked", "prefix": "A", "avg_minutes": 1},
    )
    assert r.status_code == 403
    consult.refresh_from_db()
    assert consult.name == "Consultation"


def test_superuser_is_manager_everywhere(client, branch):
    from .conftest import make_user

    admin = make_user("root", is_superuser=True, is_staff=True)
    client.force_login(admin)
    assert client.get(reverse("manage:settings", args=[branch.slug])).status_code == 200


def test_user_without_branch_sees_hint(client, db):
    from .conftest import make_user

    client.force_login(make_user("lonely"))
    assert "not assigned to any branch" in client.get(reverse("staff:dashboard")).content.decode()


def test_login_flow(client, staff_user):
    r = client.post(reverse("login"), {"username": "staff", "password": PASSWORD})
    assert r.status_code == 302 and r.url == reverse("staff:dashboard")
    r = client.post(reverse("login"), {"username": "staff", "password": "wrong"})
    assert "Wrong username or password." in r.content.decode()


# ---- staff console --------------------------------------------------------


def _act(client, counter, action):
    return client.post(
        reverse("staff:counter_action", args=[counter.pk]), {"action": action}, follow=True
    )


def test_counter_console_flow(staff_client, consult, room1):
    engine.issue_ticket(consult)
    r = staff_client.get(reverse("staff:counter", args=[room1.pk]))
    assert "A-001" in r.content.decode() and "Call next" in r.content.decode()
    r = _act(staff_client, room1, "call_next")
    assert "Called A-001." in r.content.decode()
    t = Ticket.objects.get()
    assert (t.status, t.counter_id) == (S.CALLED, room1.pk)
    _act(staff_client, room1, "start")
    t.refresh_from_db()
    assert t.status == S.SERVING
    _act(staff_client, room1, "done")
    t.refresh_from_db()
    assert t.status == S.DONE and t.events.filter(actor__username="staff").count() == 3
    r = _act(staff_client, room1, "call_next")
    assert "Nobody is waiting." in r.content.decode()


def test_counter_errors_are_messages(staff_client, consult, room1):
    r = _act(staff_client, room1, "done")
    assert "No ticket at this counter." in r.content.decode()
    r = _act(staff_client, room1, "explode")
    assert "Unknown action." in r.content.decode()
    engine.issue_ticket(consult)
    engine.issue_ticket(consult)
    _act(staff_client, room1, "call_next")
    r = _act(staff_client, room1, "call_next")
    assert "Finish or mark the current ticket" in r.content.decode()


def test_counter_action_requires_post(staff_client, room1):
    assert staff_client.get(reverse("staff:counter_action", args=[room1.pk])).status_code == 405


def test_walk_in_priority_and_safe_redirect(staff_client, branch, consult, room1):
    url = reverse("staff:walk_in", args=[branch.slug])
    back = reverse("staff:counter", args=[room1.pk])
    r = staff_client.post(url, {"service": consult.pk, "priority": "on", "next": back})
    assert r.status_code == 302 and r.url == back
    t = Ticket.objects.get()
    assert (t.priority, t.source) == (True, "desk")
    r = staff_client.post(url, {"service": consult.pk, "next": "https://evil.example/staff/"})
    assert r.url == reverse("staff:dashboard")
    r = staff_client.post(url, {"service": consult.pk, "next": "//evil.example/staff/"})
    assert r.url == reverse("staff:dashboard")
    r = staff_client.post(url, {"next": back}, follow=True)
    assert "Choose a service" in r.content.decode()


# ---- history search / filter ---------------------------------------------


def test_history_search_and_filters(staff_client, branch, consult, pharmacy, room1):
    engine.issue_ticket(consult, nickname="Siti")  # A-001
    engine.issue_ticket(consult, nickname="Ravi")  # A-002
    engine.issue_ticket(pharmacy, nickname="Siti Aminah")  # P-001
    engine.call_next(room1)  # A-001 called
    url = reverse("staff:history", args=[branch.slug])

    def codes(params):
        r = staff_client.get(url, params)
        assert r.status_code == 200
        return [t.code for t in r.context["page"]]

    assert codes({}) == ["P-001", "A-002", "A-001"]
    assert codes({"q": "siti"}) == ["P-001", "A-001"]
    assert codes({"q": "a-00"}) == ["A-002", "A-001"]
    assert codes({"status": "called"}) == ["A-001"]
    assert codes({"service": pharmacy.pk}) == ["P-001"]
    assert codes({"q": "siti", "service": consult.pk}) == ["A-001"]
    assert codes({"day": "2001-01-01"}) == []
    today = engine.local_day().isoformat()
    assert len(codes({"day": today})) == 3
    # An invalid filter value is ignored (and reported); the valid ones still apply.
    assert len(codes({"status": "bogus"})) == 3
    assert codes({"status": "bogus", "q": "ravi"}) == ["A-002"]


def test_history_pagination_keeps_filters(staff_client, branch, consult):
    for _ in range(30):
        engine.issue_ticket(consult)
    url = reverse("staff:history", args=[branch.slug])
    r = staff_client.get(url, {"q": "A-0", "page": 2})
    page = r.context["page"]
    assert (page.number, len(page.object_list), page.paginator.count) == (2, 5, 30)
    assert "q=A-0&amp;page=1" in r.content.decode()


def test_history_is_branch_scoped(staff_client, branch, other_branch, consult):
    foreign = Service.objects.create(branch=other_branch, name="X", prefix="A")
    engine.issue_ticket(foreign)
    engine.issue_ticket(consult)
    r = staff_client.get(reverse("staff:history", args=[branch.slug]))
    assert [t.service_id for t in r.context["page"]] == [consult.pk]


# ---- manager CRUD ---------------------------------------------------------


def test_manager_creates_and_edits_service(manager_client, branch, consult):
    url = reverse("manage:service_create", args=[branch.slug])
    r = manager_client.post(
        url, {"name": "Dental", "prefix": "D", "avg_minutes": 15, "is_active": "on"}
    )
    assert r.status_code == 302
    s = Service.objects.get(prefix="D")
    assert (s.branch, s.name, s.avg_minutes, s.is_active) == (branch, "Dental", 15, True)
    edit = reverse("manage:service_edit", args=[branch.slug, s.pk])
    manager_client.post(edit, {"name": "Dental care", "prefix": "D", "avg_minutes": 20})
    s.refresh_from_db()
    assert (s.name, s.avg_minutes, s.is_active) == ("Dental care", 20, False)


@pytest.mark.parametrize(
    "data,error",
    [
        ({"name": "Other", "prefix": "A", "avg_minutes": 5}, "already uses this prefix"),
        ({"name": "consultation", "prefix": "Z", "avg_minutes": 5}, "already has this name"),
        ({"name": "Lower", "prefix": "a1", "avg_minutes": 5}, "capital letters"),
        ({"name": "Zero", "prefix": "Z", "avg_minutes": 0}, "greater than or equal to 1"),
    ],
)
def test_service_validation(manager_client, branch, consult, data, error):
    r = manager_client.post(reverse("manage:service_create", args=[branch.slug]), data)
    assert r.status_code == 200
    assert error in r.content.decode()
    assert Service.objects.filter(branch=branch).count() == 1


def test_same_prefix_allowed_in_other_branch(manager_client, branch, other_branch, consult):
    Service.objects.create(branch=other_branch, name="Consultation", prefix="B")
    r = manager_client.post(
        reverse("manage:service_create", args=[branch.slug]),
        {"name": "Billing", "prefix": "B", "avg_minutes": 2},
    )
    assert r.status_code == 302


def test_manager_creates_counter_with_services(manager_client, branch, consult, pharmacy):
    r = manager_client.post(
        reverse("manage:counter_create", args=[branch.slug]),
        {"name": "Room 9", "services": [consult.pk, pharmacy.pk], "is_active": "on"},
    )
    assert r.status_code == 302
    c = Counter.objects.get(name="Room 9")
    assert set(c.services.values_list("prefix", flat=True)) == {"A", "P"}


def test_counter_cannot_use_other_branch_service(manager_client, branch, other_branch):
    foreign = Service.objects.create(branch=other_branch, name="X", prefix="X")
    r = manager_client.post(
        reverse("manage:counter_create", args=[branch.slug]),
        {"name": "Room 9", "services": [foreign.pk]},
    )
    assert r.status_code == 200 and not Counter.objects.filter(name="Room 9").exists()


def test_manager_cannot_edit_other_branch_objects(manager_client, branch, other_branch):
    foreign = Service.objects.create(branch=other_branch, name="X", prefix="X")
    # Right branch slug but foreign object id -> 404, wrong branch -> 403.
    assert (
        manager_client.get(
            reverse("manage:service_edit", args=[branch.slug, foreign.pk])
        ).status_code
        == 404
    )
    assert (
        manager_client.get(
            reverse("manage:service_edit", args=[other_branch.slug, foreign.pk])
        ).status_code
        == 403
    )
