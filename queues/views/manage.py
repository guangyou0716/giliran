"""Manager pages: services and counters of a branch."""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _

from ..forms import CounterForm, ServiceForm
from ..models import Branch, Counter, Service
from ..permissions import require_manager


@login_required
def settings_page(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    return render(
        request,
        "queues/manage/settings.html",
        {
            "branch": branch,
            "services": branch.services.all(),
            "counters": branch.counters.prefetch_related("services"),
        },
    )


def _edit(request, branch, form_class, instance, title):
    form = form_class(request.POST or None, instance=instance, branch=branch)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Saved."))
        return redirect("manage:settings", slug=branch.slug)
    return render(
        request,
        "queues/manage/form.html",
        {"branch": branch, "form": form, "title": title},
    )


@login_required
def service_create(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    return _edit(request, branch, ServiceForm, None, _("New service"))


@login_required
def service_edit(request, slug, pk):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    service = get_object_or_404(Service, pk=pk, branch=branch)
    return _edit(request, branch, ServiceForm, service, _("Edit service"))


@login_required
def counter_create(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    return _edit(request, branch, CounterForm, None, _("New counter"))


@login_required
def counter_edit(request, slug, pk):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    counter = get_object_or_404(Counter, pk=pk, branch=branch)
    return _edit(request, branch, CounterForm, counter, _("Edit counter"))
