from django import template
from django.template.defaultfilters import floatformat

register = template.Library()


@register.filter
def num1(value):
    """One decimal place, or an em dash when there is no value (nothing to average)."""
    if value is None:
        return "—"
    return floatformat(value, 1)
