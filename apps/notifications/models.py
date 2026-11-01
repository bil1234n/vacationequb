from django.conf import settings
from django.db import models

from apps.core.models import TimeStampedModel


class Notification(TimeStampedModel):
    class Kind(models.TextChoices):
        BOOKING = "BOOKING", "Booking"
        PAYMENT = "PAYMENT", "Payment"
        EQUB_REMINDER = "EQUB_REMINDER", "Equb reminder"
        EQUB_WIN = "EQUB_WIN", "Equb win"
        LUCKY_WIN = "LUCKY_WIN", "Giveaway win"
        LUCKY_ENTRY = "LUCKY_ENTRY", "Giveaway entry"
        DONATION = "DONATION", "Crowdfunding donation"
        GROUP_INVITE = "GROUP_INVITE", "Group Equb invitation"
        PROMO = "PROMO", "Promotion"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=16, choices=Kind.choices)
    title = models.CharField(max_length=120)
    body = models.CharField(max_length=500)
    data = models.JSONField(default=dict, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    pushed = models.BooleanField(default=False)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.kind}: {self.title}"
