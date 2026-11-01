"""
Existing packages and groups get the new fields from what they already had:
contribution frequency in days, the duration that matches their number of rounds, the end date, and
"Automatic" draws (that is how they were drawn until now). Nothing else is changed.
"""
import calendar
from datetime import date, timedelta

from django.db import migrations

DAYS = {"WEEKLY": 7, "BIWEEKLY": 14, "MONTHLY": 30}


def _add_months(d, months):
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def forwards(apps, schema_editor):
    EqubPackage = apps.get_model("equb", "EqubPackage")
    EqubGroup = apps.get_model("equb", "EqubGroup")
    for p in EqubPackage.objects.all():
        p.frequency_days = DAYS.get(p.frequency, 30)
        p.save(update_fields=["frequency_days"])
    for g in EqubGroup.objects.all():
        freq = g.frequency if g.frequency in DAYS else "MONTHLY"
        rounds = g.rounds.count() if g.status not in ("DRAFT", "OPEN") else 0
        if not rounds:
            rounds = -(-g.max_members // max(g.winners_per_round, 1))
        g.frequency_days = DAYS[freq]
        if freq == "MONTHLY":
            g.duration_value, g.duration_unit = rounds, "MONTHS"
            end = _add_months(g.start_date, rounds)
        else:
            weeks = rounds * (2 if freq == "BIWEEKLY" else 1)
            g.duration_value, g.duration_unit = weeks, "WEEKS"
            end = g.start_date + timedelta(weeks=weeks)
        last = g.rounds.order_by("-number").first()
        g.end_date = last.draw_date if last else end
        g.draw_type = "AUTOMATIC"
        g.save(update_fields=["frequency_days", "duration_value", "duration_unit", "end_date", "draw_type"])


class Migration(migrations.Migration):
    dependencies = [("equb", "0007_draws_duration_group_equb")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
