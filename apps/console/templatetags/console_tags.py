from decimal import Decimal, InvalidOperation

from django import template
from django.db.models import Q
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()

TONES = {
    "green": {"CONFIRMED", "SUCCESS", "PAID", "ACTIVE", "DRAWN", "LIVE", "ISSUED", "WON", "SCHEDULED", "AVAILABLE", "REDEEMED",
              "OPEN", "PAID_OUT", "COMPLETED", "BOARDED"},
    "gold": {"PENDING_PAYMENT", "PENDING", "COLLECTING", "INITIATED", "CLOSED", "LOCKED", "WAIVED", "CHECKED_IN", "UPCOMING"},
    "red": {"FAILED", "CANCELLED", "LATE", "DEFAULTED", "EXPIRED", "VOID", "REFUNDED"},
}
LABELS = {"DEFAULTED": "Terminated", "WAIVED": "From credit", "PAID_OUT": "Paid out", "WON": "Won"}


@register.filter
def money(value, currency="ETB"):
    try:
        v = Decimal(value or 0)
    except (InvalidOperation, TypeError):
        return value
    text = f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"
    return f"{text} {currency}".strip()


@register.filter
def compact(value):
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return value
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 10_000:
        return f"{v / 1000:.0f}K"
    if v >= 1000:
        return f"{v / 1000:.1f}K"
    return f"{v:,.0f}"


@register.filter
def status_badge(value, label=None):
    v = str(value or "")
    tone = next((t for t, vals in TONES.items() if v in vals), "grey")
    text = label or LABELS.get(v) or v.replace("_", " ").title()
    return mark_safe(f'<span class="badge b-{tone}">{text}</span>')


@register.filter
def initials(name):
    parts = (name or "?").split()
    return "".join(p[0] for p in parts[:2]).upper()


@register.filter
def mask_phone(phone):
    p = str(phone or "")
    return p[:4] + "•••••" + p[-3:] if len(p) > 7 else p


@register.simple_tag(takes_context=True)
def qs(context, **kwargs):
    """Rebuild the current query string with overrides (for filters & pagination)."""
    params = context["request"].GET.copy()
    for k, v in kwargs.items():
        if v in (None, ""):
            params.pop(k, None)
        else:
            params[k] = v
    return "?" + params.urlencode() if params else "?"


PURPOSE_SHORT = {"BOOKING": "Booking", "EQUB_CONTRIBUTION": "Equb contribution", "MICRO_DONATION": "Old lucky-draw entry", "DONATION": "Crowdfunding gift"}


@register.filter
def purpose_short(value):
    return PURPOSE_SHORT.get(value, value)


@register.filter
def every(frequency):
    """Accepts frequency days (7) or an old code ("WEEKLY")."""
    from apps.equb.models import FREQUENCY_DAYS, every_phrase

    if isinstance(frequency, int) or str(frequency).isdigit():
        return every_phrase(int(frequency))
    return every_phrase(FREQUENCY_DAYS.get(frequency, 30))


@register.filter
def more_has_errors(form):
    return any(form[n].errors for n in getattr(form, "more_sections", []) if n in form.fields)


@register.filter
def field(form, name):
    """{{ form|field:"name" }} → bound field (lets templates lay out fields by section)."""
    try:
        return form[name]
    except KeyError:
        return None


@register.filter
def is_checkbox(bound_field):
    return bound_field.field.widget.__class__.__name__ == "CheckboxInput"


@register.filter
def is_wide(bound_field):
    return bound_field.field.widget.__class__.__name__ == "Textarea"


@register.filter
def split(value, sep=" "):
    return value.split(sep)


# ----------------------------------------------------------------- Equb helpers
TONE_CLASS = {"green": "b-green", "gold": "b-gold", "red": "b-red", "grey": "b-grey", "amber": "b-amber"}


@register.filter
def standing_badge(status):
    from apps.equb.standing import STATUS_META

    label, tone = STATUS_META.get(str(status or ""), (str(status or "—").replace("_", " ").title(), "grey"))
    return mark_safe(f'<span class="badge {TONE_CLASS[tone]}">{label}</span>')


SCHEDULE = {
    "paid": ("Paid", "green"), "paid_late": ("Paid late", "gold"), "credit": ("Paid from credit", "green"),
    "overdue": ("Overdue", "red"), "due": ("Due", "gold"), "prepaid": ("Prepaid", "green"),
    "upcoming": ("Upcoming", "grey"), "not_billed": ("Not billed", "grey"),
}


@register.filter
def schedule_badge(state):
    label, tone = SCHEDULE.get(str(state), (str(state).title(), "grey"))
    return mark_safe(f'<span class="badge {TONE_CLASS[tone]}">{label}</span>')


@register.filter
def days_badge(days):
    try:
        d = int(days or 0)
    except (TypeError, ValueError):
        return days
    if d <= 0:
        return mark_safe('<span class="badge b-green">On time</span>')
    tone = "b-gold" if d <= 14 else "b-amber" if d <= 30 else "b-red"
    return mark_safe(f'<span class="badge {tone}">{d} day{"s" if d != 1 else ""}</span>')


@register.filter
def outcome_badge(log):
    if not log:
        return mark_safe('<span class="muted small">Never called</span>')
    tone = TONE_CLASS.get(getattr(log, "tone", "grey"), "b-grey")
    return mark_safe(f'<span class="badge {tone}">{log.get_outcome_display()}</span>')


@register.filter
def tel(phone):
    p = str(phone or "")
    return format_html('<a class="tel" href="tel:{}">{}</a>', p, p) if p else ""


@register.filter
def get_item(d, key):
    try:
        return d.get(key)
    except AttributeError:
        return None


@register.filter
def neg(value):
    try:
        return -Decimal(value or 0)
    except (InvalidOperation, TypeError):
        return value


@register.simple_tag
def equb_counts():
    """Cheap counts for the sidebar badges (no standings maths)."""
    from django.db.models import F
    from django.utils import timezone

    from apps.equb.models import Contribution, EqubMembership, EqubRound

    live = EqubMembership.objects.filter(status__in=EqubMembership.LIVE_STATUSES)
    return {
        "draws_due": EqubRound.objects.filter(status="COLLECTING", group__status="ACTIVE", group__current_round=F("number"),
                                              draw_date__lte=timezone.localdate()).count(),
        "terminations": live.filter(Q(termination_requested_at__isnull=False) | Q(exit_requested_at__isnull=False)).count(),
        "uncalled": live.filter(called_at__isnull=True, contributions__status=Contribution.Status.LATE).distinct().count(),
    }


@register.filter
def modulo(value, arg):
    try:
        return int(value) % int(arg)
    except (TypeError, ValueError, ZeroDivisionError):
        return 0
