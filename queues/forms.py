from datetime import date, datetime, timedelta

from django import forms
from django.utils import timezone
from django.utils.formats import date_format
from django.utils.translation import gettext_lazy as _

from .engine import normalize_phone
from .models import Counter, Service, Slot, Ticket, phone_validator


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


# --- Appointments ---------------------------------------------------------------------------


class BookingSearchForm(forms.Form):
    """Step 1 of booking (GET): which service and which day."""

    service = forms.ModelChoiceField(
        queryset=Service.objects.none(),
        widget=forms.RadioSelect,
        empty_label=None,
        label=_("What do you need?"),
    )
    day = forms.TypedChoiceField(label=_("Day"), coerce=date.fromisoformat, choices=())

    def __init__(self, *args, branch, days, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = branch.services.filter(is_active=True)
        today = timezone.localdate()
        today_label = _("Today")
        # Translators: Django date format for a short day label, e.g. "Fri 20 Nov".
        day_format = str(_("D j M"))
        choices = []
        for d in days:
            label = date_format(d, day_format)
            choices.append((d.isoformat(), f"{label} · {today_label}" if d == today else label))
        self.fields["day"].choices = choices


class BookingForm(forms.Form):
    """Step 2 of booking (POST): the time, a nickname and a phone number."""

    slot = forms.ModelChoiceField(
        queryset=Slot.objects.none(),
        widget=forms.RadioSelect,
        empty_label=None,
        label=_("Choose a time"),
        error_messages={"invalid_choice": _("That time is no longer available.")},
    )
    nickname = forms.CharField(
        label=_("Nickname"),
        max_length=40,
        help_text=_("So staff can greet you. Please do not enter your IC number."),
    )
    phone = forms.CharField(
        label=_("Phone number"),
        max_length=24,
        help_text=_("Used only for this booking. Staff screens show the last 4 digits."),
        widget=forms.TextInput(attrs={"inputmode": "tel", "autocomplete": "tel"}),
    )

    def __init__(self, *args, slots, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["slot"].queryset = slots

    def clean_phone(self):
        phone = normalize_phone(self.cleaned_data["phone"])
        phone_validator(phone)
        return phone


class SlotGenerateForm(forms.Form):
    """Manager: create slots for one service on one day, every N minutes."""

    service = forms.ModelChoiceField(label=_("Service"), queryset=Service.objects.none())
    day = forms.DateField(label=_("Day"), widget=forms.DateInput(attrs={"type": "date"}))
    start = forms.TimeField(
        label=_("From"), widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )
    end = forms.TimeField(
        label=_("Until"), widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M")
    )
    every = forms.IntegerField(label=_("Minutes per slot"), min_value=5, max_value=240, initial=30)
    capacity = forms.IntegerField(
        label=_("Seats per slot"),
        min_value=1,
        max_value=50,
        initial=2,
        help_text=_("Leave room for walk-ins: booked customers are called first at their time."),
    )

    MAX_SLOTS = 96

    def __init__(self, *args, branch, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["service"].queryset = branch.services.filter(is_active=True)

    def clean(self):
        data = super().clean()
        start, end, every = data.get("start"), data.get("end"), data.get("every")
        if data.get("day") and data["day"] < timezone.localdate():
            self.add_error("day", _("Choose today or a later day."))
        if start and end and every:
            if end <= start:
                raise forms.ValidationError(_("The end time must be after the start time."))
            minutes = (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)
            if minutes // every < 1:
                raise forms.ValidationError(_("The time range is shorter than one slot."))
            if minutes // every > self.MAX_SLOTS:
                raise forms.ValidationError(_("That would create too many slots at once."))
        return data

    def slot_times(self):
        """Aware (start, end) pairs in local time for the cleaned data."""
        data = self.cleaned_data
        tz = timezone.get_current_timezone()
        current = datetime.combine(data["day"], data["start"], tzinfo=tz)
        last = datetime.combine(data["day"], data["end"], tzinfo=tz)
        step = timedelta(minutes=data["every"])
        while current + step <= last:
            yield current, current + step
            current += step


class SlotForm(forms.ModelForm):
    """Manager: change the seats of one slot or close it for booking."""

    class Meta:
        model = Slot
        fields = ["capacity", "is_active"]

    def clean_capacity(self):
        capacity = self.cleaned_data["capacity"]
        if capacity < self.instance.booked_count:
            raise forms.ValidationError(
                _("%(n)s seats are already booked; capacity cannot be lower.")
                % {"n": self.instance.booked_count}
            )
        return capacity


class AppointmentFilterForm(forms.Form):
    day = forms.DateField(
        label=_("Day"), required=False, widget=forms.DateInput(attrs={"type": "date"})
    )
    q = forms.CharField(
        label=_("Reference, nickname or last 4 digits"), required=False, max_length=40
    )
