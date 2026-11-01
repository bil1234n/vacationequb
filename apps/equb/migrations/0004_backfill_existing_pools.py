"""
Data for pools that existed before the professional Equb upgrade.

Kept separate from 0003 on purpose: PostgreSQL refuses to build 0003's deferred indexes in the same transaction
as row updates ("cannot CREATE INDEX … because it has pending trigger events").
"""
from django.db import migrations


def backfill(apps, schema_editor):
    """
    Give existing pools a join code, keep their payout rule and split existing payouts into gross / fee.

    Pools created before this upgrade paid winners what had been collected by draw day, so they keep
    ``payout_basis = COLLECTED``; pools created from now on default to the full pot.
    """
    import secrets
    from decimal import Decimal

    EqubGroup = apps.get_model("equb", "EqubGroup")
    EqubPayout = apps.get_model("equb", "EqubPayout")
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    used = set(EqubGroup.objects.exclude(join_code=None).values_list("join_code", flat=True))
    for g in EqubGroup.objects.filter(join_code=None):
        code = "".join(secrets.choice(alphabet) for _ in range(6))
        while code in used:
            code = "".join(secrets.choice(alphabet) for _ in range(6))
        used.add(code)
        EqubGroup.objects.filter(pk=g.pk).update(join_code=code)
    EqubGroup.objects.update(payout_basis="COLLECTED")
    for p in EqubPayout.objects.filter(gross_amount=0).select_related("round__group"):
        rate = Decimal(p.round.group.service_fee_percent) / 100
        gross = (Decimal(p.amount) / (1 - rate)).quantize(Decimal("0.01")) if rate < 1 else Decimal(p.amount)
        EqubPayout.objects.filter(pk=p.pk).update(gross_amount=gross, fee_amount=gross - Decimal(p.amount))


class Migration(migrations.Migration):

    dependencies = [
        ("equb", "0003_professional_equb"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
