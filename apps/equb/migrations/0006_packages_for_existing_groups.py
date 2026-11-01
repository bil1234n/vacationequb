"""
Put every existing Equb group under an Equb package.

Groups with the same destination, contribution and frequency share one package (named after the destination,
e.g. "Umrah Equb – 5,000 ETB monthly"). Kept separate from 0005 so PostgreSQL never mixes the schema change and the
row updates in one transaction.
"""
from django.db import migrations
from django.utils.text import slugify

DEST = {"HAJJ": "Hajj", "UMRAH": "Umrah", "DUBAI": "Dubai", "CHINA": "China trade tour", "TURKEY": "Turkey",
        "OTHER": "Travel"}
FREQ = {"WEEKLY": "weekly", "BIWEEKLY": "every two weeks", "MONTHLY": "monthly"}


def forwards(apps, schema_editor):
    EqubGroup = apps.get_model("equb", "EqubGroup")
    EqubPackage = apps.get_model("equb", "EqubPackage")
    made = {}
    for g in EqubGroup.objects.filter(equb_package__isnull=True).order_by("id"):
        key = (g.destination, g.contribution_amount, g.frequency)
        pkg = made.get(key)
        if pkg is None:
            amount = f"{g.contribution_amount:,.0f}"
            name = f"{DEST.get(g.destination, 'Travel')} Equb – {amount} ETB {FREQ.get(g.frequency, '')}".strip()
            base, slug, i = slugify(name)[:120] or "equb-package", None, 1
            slug = base
            while EqubPackage.objects.filter(slug=slug).exists():
                i += 1
                slug = f"{base}-{i}"
            pkg = EqubPackage.objects.create(
                name=name, slug=slug, destination=g.destination, travel_package_id=g.package_id,
                description=g.description, image_url=g.image_url, contribution_amount=g.contribution_amount,
                frequency=g.frequency, max_members=g.max_members, winners_per_round=g.winners_per_round,
                payout_basis=g.payout_basis, service_fee_percent=g.service_fee_percent, late_fee=g.late_fee,
                grace_days=g.grace_days, require_paid_to_draw=g.require_paid_to_draw, terms_content=g.rules_text,
                is_active=True)
            made[key] = pkg
        EqubGroup.objects.filter(pk=g.pk).update(equb_package=pkg)


class Migration(migrations.Migration):

    dependencies = [
        ("equb", "0005_equb_packages"),
    ]

    operations = [
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
