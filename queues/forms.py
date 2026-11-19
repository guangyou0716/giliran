from django import forms
from django.utils.translation import gettext_lazy as _

from .models import Counter, Service, Ticket


class JoinForm(forms.Form):
    service = forms.ModelChoiceField(
        queryset=Service.objects.none(),
        widget=forms.RadioSelect,
        empty_label=None,
        label=_("What do you need?"),
    )
    nickname = forms.CharField(
        label=_("Nickname (optional)"),
        max_length=40,
        required=False,
        help_text=_("Shown only to staff. Please do not enter your IC number."),
    )

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = branch.services.filter(is_active=True)


class WalkInForm(JoinForm):
    priority = forms.BooleanField(
        label=_("Priority (elderly, disabled, pregnant, urgent)"), required=False
    )


class ServiceForm(forms.ModelForm):
    class Meta:
        model = Service
        fields = ["name", "prefix", "avg_minutes", "is_active"]

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.branch = branch
        self.instance.branch = branch

    def clean_prefix(self):
        prefix = self.cleaned_data["prefix"].strip().upper()
        clash = Service.objects.filter(branch=self.branch, prefix=prefix).exclude(
            pk=self.instance.pk
        )
        if clash.exists():
            raise forms.ValidationError(_("Another service already uses this prefix."))
        return prefix

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = Service.objects.filter(branch=self.branch, name__iexact=name).exclude(
            pk=self.instance.pk
        )
        if clash.exists():
            raise forms.ValidationError(_("Another service already has this name."))
        return name


class CounterForm(forms.ModelForm):
    class Meta:
        model = Counter
        fields = ["name", "services", "is_active"]
        widgets = {"services": forms.CheckboxSelectMultiple}

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.branch = branch
        self.instance.branch = branch
        self.fields["services"].queryset = branch.services.all()

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        clash = Counter.objects.filter(branch=self.branch, name__iexact=name).exclude(
            pk=self.instance.pk
        )
        if clash.exists():
            raise forms.ValidationError(_("Another counter already has this name."))
        return name


class TicketFilterForm(forms.Form):
    q = forms.CharField(label=_("Search"), required=False, max_length=40)
    status = forms.ChoiceField(
        label=_("Status"),
        required=False,
        choices=[("", _("All statuses")), *Ticket.Status.choices],
    )
    service = forms.ModelChoiceField(
        label=_("Service"),
        required=False,
        queryset=Service.objects.none(),
        empty_label=_("All services"),
    )
    day = forms.DateField(
        label=_("Day"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = branch.services.all()
