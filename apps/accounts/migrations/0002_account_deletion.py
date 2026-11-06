"""Account deletion requests: the customer asks, the account is suspended, staff approve or restore it."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="deletion_status",
            field=models.CharField(
                blank=True,
                choices=[("", "Active"), ("PENDING", "Deletion requested (suspended)"), ("DELETED", "Deleted")],
                db_index=True,
                default="",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="deletion_reason",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="user",
            name="deletion_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="deletion_reviewed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="user",
            name="deletion_reviewed_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="deletion_note",
            field=models.CharField(blank=True, help_text="Staff note on the decision", max_length=255),
        ),
    ]
