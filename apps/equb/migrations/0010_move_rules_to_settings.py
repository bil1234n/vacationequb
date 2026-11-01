"""
Before the per-group settings are removed:
* the Equb rules (service fee, late fee, grace days, who enters the draw, payout basis, auto start) take the values
  most groups were using;
* customer-created private Equbs (the earlier "circles") become Group Equbs owned by their organizer, so their
  members stay together.
"""
from collections import Counter

from django.db import migrations


def most_common(values, default):
    values = [v for v in values if v is not None]
    return Counter(values).most_common(1)[0][0] if values else default


def forwards(apps, schema_editor):
    EqubGroup = apps.get_model("equb", "EqubGroup")
    EqubSettings = apps.get_model("equb", "EqubSettings")
    GroupEqub = apps.get_model("equb", "GroupEqub")
    EqubMembership = apps.get_model("equb", "EqubMembership")

    rules = EqubSettings.objects.filter(pk=1).first() or EqubSettings(pk=1)
    groups = list(EqubGroup.objects.exclude(status="CANCELLED"))
    if groups:
        rules.service_fee_percent = most_common([g.service_fee_percent for g in groups], rules.service_fee_percent)
        rules.late_fee = most_common([g.late_fee for g in groups], rules.late_fee)
        rules.grace_days = most_common([g.grace_days for g in groups], rules.grace_days)
        rules.require_paid_to_draw = most_common([g.require_paid_to_draw for g in groups], True)
        rules.payout_basis = most_common([g.payout_basis for g in groups], "FULL_POT")
        rules.auto_start_when_full = most_common([g.auto_start_when_full for g in groups], True)
    rules.save()

    import random
    import string

    def code():
        while True:
            c = "G" + "".join(random.choices(string.ascii_uppercase.replace("O", "") + "23456789", k=5))
            if not GroupEqub.objects.filter(invite_code=c).exists():
                return c

    for g in EqubGroup.objects.exclude(kind="STANDARD").exclude(organizer__isnull=True):
        team = GroupEqub.objects.create(name=g.name, kind=g.kind, equb=g, owner_id=g.organizer_id,
                                        description=g.description, invite_code=code(), created_by_id=g.organizer_id)
        EqubMembership.objects.filter(group=g).update(team=team)


class Migration(migrations.Migration):
    dependencies = [("equb", "0009_group_equbs_and_company_rules")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
