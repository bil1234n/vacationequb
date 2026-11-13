"""
Travel agencies (partners) on EthioVacation.

An agency signs up its own customers (and their family members), enrols them in Equbs and books flights and
packages for them through the platform. Every ticket sale is split: the agency keeps the sale, EthioVacation keeps
``commission_percent`` (5 % by default). With a Chapa subaccount, Chapa settles the agency's share directly to the
agency's bank account at payment time; otherwise the share is recorded as owed and paid out from the console.
"""
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import TimeStampedModel

CENT = Decimal("0.01")


def default_commission():
    return Decimal(str(settings.ETHIOVACATION.get("AGENCY_COMMISSION_PERCENT", 5)))


class Agency(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending approval"
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"

    name = models.CharField(max_length=150)
    slug = models.SlugField(max_length=160, unique=True, blank=True)
    license_number = models.CharField(max_length=60, blank=True, help_text="Trade / tour-operator licence number")
    tin = models.CharField("TIN", max_length=20, blank=True)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    city = models.CharField(max_length=80, default="Addis Ababa")
    address = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True)
    commission_percent = models.DecimalField(
        max_digits=5, decimal_places=2, default=default_commission,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("50"))],
        help_text="EthioVacation's cut of every sale, taken from the agency's share.")

    # Payout account → Chapa subaccount (Chapa sends the agency's share here automatically).
    bank_code = models.PositiveIntegerField(null=True, blank=True, help_text="Chapa bank id")
    bank_name = models.CharField(max_length=80, blank=True)
    account_name = models.CharField(max_length=120, blank=True)
    account_number = models.CharField(max_length=40, blank=True)
    chapa_subaccount_id = models.CharField(max_length=80, blank=True,
                                           help_text="Set automatically when the Chapa subaccount is created")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    notes = models.TextField(blank=True, help_text="Internal notes (only EthioVacation staff see these)")

    class Meta:
        ordering = ["name"]
        verbose_name_plural = "agencies"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:140] or "agency"
            slug, i = base, 2
            while Agency.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def has_split(self) -> bool:
        return bool(self.chapa_subaccount_id)

    def commission_on(self, amount) -> Decimal:
        return (Decimal(amount) * self.commission_percent / 100).quantize(CENT, rounding=ROUND_HALF_UP)


class AgencyStaff(TimeStampedModel):
    """A person who works for an agency and signs in to the agency portal."""

    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        MANAGER = "MANAGER", "Manager"
        AGENT = "AGENT", "Sales agent"

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="staff")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agency_staff")
    role = models.CharField(max_length=8, choices=Role.choices, default=Role.AGENT)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["agency", "role", "user__full_name"]
        verbose_name_plural = "agency staff"

    def __str__(self):
        return f"{self.user.full_name} – {self.agency} ({self.get_role_display()})"

    @property
    def can_manage(self) -> bool:
        return self.role in (self.Role.OWNER, self.Role.MANAGER)


class AgencyCustomer(TimeStampedModel):
    """A customer the agency signed up. A customer belongs to one agency; all their ticket sales count for it."""

    agency = models.ForeignKey(Agency, on_delete=models.CASCADE, related_name="customers")
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="agency_link")
    added_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user.full_name} @ {self.agency}"


class FamilyMember(TimeStampedModel):
    """
    A relative of an agency customer (often without a phone). Used to pre-fill passengers on bookings and
    to give them an Equb place as a responsibility person that the customer pays for.
    """

    class Relation(models.TextChoices):
        SPOUSE = "SPOUSE", "Spouse"
        CHILD = "CHILD", "Child"
        PARENT = "PARENT", "Parent"
        SIBLING = "SIBLING", "Brother / sister"
        OTHER = "OTHER", "Other relative"

    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    customer = models.ForeignKey(AgencyCustomer, on_delete=models.CASCADE, related_name="family")
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    relation = models.CharField(max_length=8, choices=Relation.choices, default=Relation.OTHER)
    gender = models.CharField(max_length=1, choices=Gender.choices)
    date_of_birth = models.DateField()
    phone = models.CharField(max_length=20, blank=True)
    passport_number = models.CharField(max_length=32, blank=True)
    passport_expiry = models.DateField(null=True, blank=True)

    class Meta:
        ordering = ["customer", "first_name"]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class AgencyLedgerEntry(TimeStampedModel):
    """
    One agency sale: what the customer paid, EthioVacation's commission and the agency's share.

    * ``CHAPA_SPLIT`` – paid through a gateway. With a Chapa subaccount the agency's share was settled by Chapa;
      otherwise it is owed and paid out by EthioVacation.
    * ``CREDIT_SALE`` – a booking paid with an Equb payout or a prize (money EthioVacation already holds) – owed.
    """

    class Kind(models.TextChoices):
        GATEWAY = "GATEWAY", "Paid online"
        CREDIT = "CREDIT", "Paid with Equb payout / prize credit"

    class Payout(models.TextChoices):
        SETTLED_BY_CHAPA = "SETTLED_BY_CHAPA", "Sent to agency by Chapa"
        OWED = "OWED", "Owed to agency"
        PAID = "PAID", "Paid to agency"
        REVERSED = "REVERSED", "Reversed (refund)"

    agency = models.ForeignKey(Agency, on_delete=models.PROTECT, related_name="ledger")
    kind = models.CharField(max_length=8, choices=Kind.choices)
    booking = models.ForeignKey("travel.Booking", null=True, blank=True, on_delete=models.PROTECT,
                                related_name="agency_entries")
    payment = models.OneToOneField("payments.PaymentTransaction", null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="agency_entry")
    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    gross_amount = models.DecimalField(max_digits=12, decimal_places=2)
    commission_percent = models.DecimalField(max_digits=5, decimal_places=2)
    commission = models.DecimalField(max_digits=12, decimal_places=2)
    agency_amount = models.DecimalField(max_digits=12, decimal_places=2)
    payout_status = models.CharField(max_length=16, choices=Payout.choices, default=Payout.OWED, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    paid_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    paid_reference = models.CharField(max_length=64, blank=True)
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name_plural = "agency ledger entries"
        constraints = [
            models.UniqueConstraint(fields=["booking", "kind"], condition=models.Q(kind="CREDIT"),
                                    name="uniq_agency_credit_sale"),
        ]

    def __str__(self):
        return f"{self.agency} {self.gross_amount} ({self.get_payout_status_display()})"
