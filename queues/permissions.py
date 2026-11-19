"""Branch-scoped role checks.

* Superuser: manager of every branch.
* Membership(role=manager): manage services/counters + everything staff can do, at that branch.
* Membership(role=staff): run counters, add walk-ins, view ticket history, at that branch.
"""

from django.core.exceptions import PermissionDenied

from .models import Branch, Membership

MANAGER = Membership.Role.MANAGER
STAFF = Membership.Role.STAFF


def role_at(user, branch: Branch) -> str | None:
    if not user.is_authenticated or not user.is_active:
        return None
    if user.is_superuser:
        return MANAGER
    membership = Membership.objects.filter(user=user, branch=branch).only("role").first()
    return membership.role if membership else None


def require_staff(user, branch: Branch) -> str:
    role = role_at(user, branch)
    if role not in (MANAGER, STAFF):
        raise PermissionDenied
    return role


def require_manager(user, branch: Branch) -> str:
    role = role_at(user, branch)
    if role != MANAGER:
        raise PermissionDenied
    return role


def branches_for(user):
    if not user.is_authenticated:
        return Branch.objects.none()
    if user.is_superuser:
        return Branch.objects.all()
    return Branch.objects.filter(memberships__user=user).distinct()
