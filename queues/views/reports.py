"""Manager report page and CSV export (branch-scoped)."""

from django.contrib.auth.decorators import login_required
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, render

from ..forms import ReportFilterForm
from ..models import Branch
from ..permissions import require_manager
from ..reports import build_report, report_csv


@login_required
def reports(request, slug):
    branch = get_object_or_404(Branch, slug=slug)
    require_manager(request.user, branch)
    params = request.GET.copy()
    want_csv = params.pop("format", [""])[0] == "csv"
    form = ReportFilterForm(params or None, branch=branch)
    report = None
    if not form.is_bound:
        report = build_report(branch, form.fields["start"].initial, form.fields["end"].initial)
    elif form.is_valid():
        data = form.cleaned_data
        report = build_report(branch, data["start"], data["end"], service=data["service"])
    if want_csv and report is not None:
        name = f"giliran-{branch.slug}-{report.start.isoformat()}-{report.end.isoformat()}.csv"
        response = HttpResponse(
            "\ufeff" + report_csv(report), content_type="text/csv; charset=utf-8"
        )
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        return response
    csv_params = params.copy()
    if report is not None and not form.is_bound:
        csv_params["start"] = report.start.isoformat()
        csv_params["end"] = report.end.isoformat()
    csv_params["format"] = "csv"
    return render(
        request,
        "queues/manage/reports.html",
        {
            "branch": branch,
            "form": form,
            "report": report,
            "csv_query": csv_params.urlencode(),
        },
    )
