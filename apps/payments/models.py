import uuid

from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db import models

from apps.core.models import TimeStampedModel


class PaymentTransaction(TimeStampedModel):
    """A single attempt to collect Birr from a user through an aggregator."""

    class Provider(models.TextChoices):
        TELEBIRR = "telebirr", "telebirr"
        CHAPA = "chapa", "Chapa"
        MOCK = "mock", "Sandbox (mock)"
        # Recorded by staff at the office (no gateway involved)
        CASH = "cash", "Cash (office)"
        BANK = "bank", "Bank transfer"
        MOBILE = "mobile", "Mobile money (manual)"

    class Purpose(models.TextChoices):
        BOOKING = "BOOKING", "Flight / package booking"
        EQUB_CONTRIBUTION = "EQUB_CONTRIBUTION", "Equb contribution"
        DONATION = "DONATION", "Crowdfunding donation"
        MICRO_DONATION = "MICRO_DONATION", "Old lucky-draw entry (retired)"

    class Status(models.TextChoices):
        INITIATED = "INITIATED", "Initiated"
        PENDING = "PENDING", "Awaiting customer"
        SUCCESS = "SUCCESS", "Successful"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"
        REFUNDED = "REFUNDED", "Refunded"

    FINAL_STATUSES = {Status.SUCCESS, Status.FAILED, Status.CANCELLED, Status.EXPIRED, Status.REFUNDED}
    OFFICE_PROVIDERS = {Provider.CASH, Provider.BANK, Provider.MOBILE}

    reference = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="payments")
    provider = models.CharField(max_length=16, choices=Provider.choices)
    purpose = models.CharField(max_length=24, choices=Purpose.choices, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=3, default="ETB")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.INITIATED, db_index=True)

    # What is being paid for (Booking, Contribution, MicroDonation…)
    content_type = models.ForeignKey(ContentType, on_delete=models.PROTECT)
    object_id = models.PositiveBigIntegerField()
    target = GenericForeignKey("content_type", "object_id")

    provider_reference = models.CharField(max_length=128, blank=True, db_index=True)
    checkout_url = models.URLField(max_length=1024, blank=True)
    payer_phone = models.CharField(max_length=16, blank=True)
    failure_reason = models.CharField(max_length=255, blank=True)
    raw_request = models.JSONField(default=dict, blank=True)
    raw_response = models.JSONField(default=dict, blank=True)
    callback_payload = models.JSONField(default=dict, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    fulfilled = models.BooleanField(default=False, help_text="Business side-effects have been applied.")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+", help_text="Staff member who recorded an office payment")
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Travel agency sales: the agency keeps the rest, EthioVacation keeps ``platform_fee``.
    agency = models.ForeignKey("agencies.Agency", null=True, blank=True, on_delete=models.PROTECT,
                               related_name="payments", help_text="Travel agency this sale belongs to")
    platform_fee = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                       help_text="EthioVacation's commission on an agency sale")
    split_settled = models.BooleanField(default=False,
                                        help_text="Chapa paid the agency's share directly (split payment)")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["content_type", "object_id"])]

    def __str__(self):
        return f"{self.get_provider_display()} {self.amount} {self.currency} [{self.status}]"

    @property
    def is_final(self):
        return self.status in self.FINAL_STATUSES

    @property
    def is_office(self):
        return self.provider in self.OFFICE_PROVIDERS


class WebhookEvent(TimeStampedModel):
    """Raw inbound callbacks — kept for audit, replay and dispute handling."""

    provider = models.CharField(max_length=16)
    headers = models.JSONField(default=dict)
    body = models.TextField()
    signature_valid = models.BooleanField(default=False)
    transaction = models.ForeignKey(PaymentTransaction, null=True, blank=True, on_delete=models.SET_NULL, related_name="webhooks")
    processing_error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
