"""
Community Finance & Impact – two separate, Shariah-compliant systems:

1. **Crowdfunding Hub (donations)** – people give voluntarily to help a community member reach Umrah, Hajj
   or another travel goal. The gift goes 100% to the cause; EthioVacation's platform fee is a separate, explicit amount
   added on top at checkout (100 ETB gift + 5% fee = 105 ETB paid). Giving never earns a ticket or a prize.
2. **Community Giveaways** – promotional trips paid for entirely by EthioVacation's marketing budget or a sponsor.
   Entry is free: every N friends you invite who register (10 by default) earns one entry. Nobody pays to enter,
   so there is no stake and no Maysir. Winners are picked with the provably-fair draw in ``apps.core.fairness``.

(The app label stays ``luckydraw`` so existing data and payment records migrate in place.)
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import TimeStampedModel


def _unique_slug(model, obj, text: str, fallback: str) -> str:
    base = slugify(text)[:120] or fallback
    slug, i = base, 2
    while model.objects.filter(slug=slug).exclude(pk=obj.pk).exists():
        slug, i = f"{base}-{i}", i + 1
    return slug


def _counter_safe_save(obj, counters, args, kwargs):
    """Counters are kept with atomic F() updates – never overwrite them from a stale instance."""
    if obj.pk and not kwargs.get("update_fields") and not kwargs.get("force_insert"):
        kwargs["update_fields"] = [f.name for f in obj._meta.concrete_fields
                                   if not f.primary_key and f.name not in counters]
    return args, kwargs


# =========================================================================== 1. Crowdfunding (donations)
class CampaignQuerySet(models.QuerySet):
    def live(self):
        now = timezone.now()
        return self.filter(status=CrowdfundingCampaign.Status.LIVE, starts_at__lte=now).filter(
            models.Q(ends_at__isnull=True) | models.Q(ends_at__gt=now))


class CrowdfundingCampaign(TimeStampedModel):
    """A cause people give to – e.g. helping an elderly mother of the community reach Umrah."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        LIVE = "LIVE", "Live – accepting donations"
        FUNDED = "FUNDED", "Goal reached"
        CLOSED = "CLOSED", "Closed"
        CANCELLED = "CANCELLED", "Cancelled"

    class Category(models.TextChoices):
        UMRAH = "UMRAH", "Umrah"
        HAJJ = "HAJJ", "Hajj"
        TRAVEL = "TRAVEL", "Travel"
        COMMUNITY = "COMMUNITY", "Community need"

    title = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    category = models.CharField(max_length=10, choices=Category.choices, default=Category.UMRAH)
    story = models.TextField(blank=True, help_text="Who is being helped and why")
    image_url = models.URLField(blank=True)
    beneficiary_name = models.CharField(max_length=150, help_text="Who receives the money (shown to donors)")
    beneficiary = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="crowdfunding_campaigns", help_text="Their EthioVacation account, if any")
    goal_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(1)])
    platform_fee_percent = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("5"), validators=[MinValueValidator(0), MaxValueValidator(20)],
        help_text="Added on top of each gift at checkout – the gift itself goes 100% to the cause")
    min_donation = models.PositiveIntegerField(default=10, validators=[MinValueValidator(1)])
    starts_at = models.DateTimeField(default=timezone.now)
    ends_at = models.DateTimeField(null=True, blank=True, help_text="Leave empty to keep it open until the goal is reached")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.LIVE, db_index=True)
    is_featured = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    # counters (kept with F() updates)
    raised_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    fees_collected = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    disbursed_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    donations_count = models.PositiveIntegerField(default=0)
    donors_count = models.PositiveIntegerField(default=0)

    objects = CampaignQuerySet.as_manager()
    COUNTER_FIELDS = {"raised_amount", "fees_collected", "disbursed_amount", "donations_count", "donors_count"}

    class Meta:
        ordering = ["-is_featured", "-starts_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(CrowdfundingCampaign, self, self.title, "campaign")
        args, kwargs = _counter_safe_save(self, self.COUNTER_FIELDS, args, kwargs)
        super().save(*args, **kwargs)

    @property
    def is_live(self) -> bool:
        now = timezone.now()
        return self.status == self.Status.LIVE and self.starts_at <= now and (not self.ends_at or now < self.ends_at)

    @property
    def progress_percent(self) -> float:
        return round(min(float(self.raised_amount) / float(self.goal_amount) * 100, 100), 1) if self.goal_amount else 0.0

    @property
    def remaining(self) -> Decimal:
        return max(self.goal_amount - self.raised_amount, Decimal("0"))

    @property
    def available_to_disburse(self) -> Decimal:
        return max(self.raised_amount - self.disbursed_amount, Decimal("0"))

    def fee_for(self, amount) -> Decimal:
        return (Decimal(amount) * self.platform_fee_percent / 100).quantize(Decimal("0.01"))


class Donation(TimeStampedModel):
    class Kind(models.TextChoices):
        DONATION = "DONATION", "Donation"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending payment"
        PAID = "PAID", "Paid"
        FAILED = "FAILED", "Failed"

    campaign = models.ForeignKey(CrowdfundingCampaign, on_delete=models.PROTECT, related_name="donations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="donations")
    kind = models.CharField(max_length=8, choices=Kind.choices, default=Kind.DONATION)
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(1)],
                                 help_text="The gift – goes 100% to the campaign")
    fee_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0, help_text="Platform fee, added on top")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, help_text="What the donor paid: gift + fee")
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    is_anonymous = models.BooleanField(default=False)
    message = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user} {self.amount} ETB → {self.campaign}"


class Disbursement(TimeStampedModel):
    """Money paid out of a campaign to its beneficiary (or straight to a travel booking for them)."""

    campaign = models.ForeignKey(CrowdfundingCampaign, on_delete=models.PROTECT, related_name="disbursements")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(1)])
    paid_to = models.CharField(max_length=150)
    method = models.CharField(max_length=40, default="Bank transfer")
    reference = models.CharField(max_length=80, blank=True)
    note = models.CharField(max_length=255, blank=True)
    paid_on = models.DateField(default=timezone.localdate)
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-paid_on", "-created_at"]

    def __str__(self):
        return f"{self.amount} ETB → {self.paid_to}"


# =========================================================================== 2. Community Giveaways (free, sponsored)
class GiveawayQuerySet(models.QuerySet):
    def live(self):
        now = timezone.now()
        return self.filter(status=Giveaway.Status.LIVE, starts_at__lte=now, ends_at__gt=now)


class Giveaway(TimeStampedModel):
    """A promotional trip paid by EthioVacation or a sponsor. Entries are free – earned only by inviting friends."""

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        LIVE = "LIVE", "Live – collecting referrals"
        CLOSED = "CLOSED", "Closed – awaiting draw"
        DRAWN = "DRAWN", "Winners drawn"
        CANCELLED = "CANCELLED", "Cancelled"

    class PrizeType(models.TextChoices):
        TRAVEL_PACKAGE = "TRAVEL_PACKAGE", "Travel package (Umrah, Hajj, tour)"
        TRAVEL_CREDIT = "TRAVEL_CREDIT", "Travel credit (flight booking)"
        EQUB_CONTRIBUTION = "EQUB_CONTRIBUTION", "Paid Equb contributions"

    class SponsorType(models.TextChoices):
        COMPANY = "COMPANY", "EthioVacation marketing budget"
        CORPORATE = "CORPORATE", "Corporate sponsor"

    title = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    description = models.TextField(blank=True)
    image_url = models.URLField(blank=True)

    sponsor_type = models.CharField(max_length=10, choices=SponsorType.choices, default=SponsorType.COMPANY)
    sponsor_name = models.CharField(max_length=120, default="EthioVacation")
    sponsor_note = models.CharField(max_length=255, blank=True, help_text="e.g. 'Fully paid by Awash Bank'")
    sponsor_logo_url = models.URLField(blank=True)

    prize_type = models.CharField(max_length=18, choices=PrizeType.choices)
    prize_title = models.CharField(max_length=120, help_text="e.g. 'Umrah package for one'")
    prize_value = models.DecimalField(max_digits=12, decimal_places=2, help_text="Value per winner (ETB), paid by the sponsor")
    prize_package = models.ForeignKey("travel.TravelPackage", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    prize_equb_group = models.ForeignKey("equb.EqubGroup", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    number_of_winners = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1)])

    referrals_per_entry = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Friends who must register with your code for one free entry")
    max_entries_per_user = models.PositiveIntegerField(null=True, blank=True)

    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    draw_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.LIVE, db_index=True)
    is_featured = models.BooleanField(default=False)

    entries_issued = models.PositiveIntegerField(default=0)
    participants_count = models.PositiveIntegerField(default=0)
    referrals_count = models.PositiveIntegerField(default=0)

    seed_commitment = models.CharField(max_length=64, blank=True)
    server_seed = models.CharField(max_length=64, blank=True)
    public_input = models.TextField(blank=True)
    drawn_at = models.DateTimeField(null=True, blank=True)

    objects = GiveawayQuerySet.as_manager()
    COUNTER_FIELDS = {"entries_issued", "participants_count", "referrals_count"}

    class Meta:
        ordering = ["-starts_at"]

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        from apps.core import fairness

        if not self.slug:
            self.slug = _unique_slug(Giveaway, self, self.title, "giveaway")
        if not self.server_seed:
            self.server_seed = fairness.new_seed()
            self.seed_commitment = fairness.commitment_for(self.server_seed)
        args, kwargs = _counter_safe_save(self, self.COUNTER_FIELDS, args, kwargs)
        super().save(*args, **kwargs)

    @property
    def is_live(self) -> bool:
        now = timezone.now()
        return self.status == self.Status.LIVE and self.starts_at <= now < self.ends_at


class GiveawayParticipant(TimeStampedModel):
    """Someone's referral progress in one giveaway."""

    giveaway = models.ForeignKey(Giveaway, on_delete=models.CASCADE, related_name="participants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="giveaway_progress")
    referrals = models.PositiveIntegerField(default=0)
    entries = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-entries", "-referrals"]
        constraints = [models.UniqueConstraint(fields=["giveaway", "user"], name="uniq_giveaway_participant")]


class GiveawayEntry(TimeStampedModel):
    """A contiguous range of free entry numbers ``[first_ticket, last_ticket]`` earned by referrals."""

    giveaway = models.ForeignKey(Giveaway, on_delete=models.CASCADE, related_name="entries")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="giveaway_entries")
    first_ticket = models.PositiveIntegerField()
    last_ticket = models.PositiveIntegerField()
    count = models.PositiveIntegerField()

    class Meta:
        ordering = ["giveaway", "first_ticket"]
        constraints = [models.UniqueConstraint(fields=["giveaway", "first_ticket"], name="uniq_giveaway_entry_start")]
        indexes = [models.Index(fields=["giveaway", "user"])]

    def __str__(self):
        return f"#{self.first_ticket}-{self.last_ticket} ({self.user})"


class GiveawayWinner(TimeStampedModel):
    giveaway = models.ForeignKey(Giveaway, on_delete=models.CASCADE, related_name="winners")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="giveaway_wins")
    entry = models.ForeignKey(GiveawayEntry, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    ticket_number = models.PositiveIntegerField()
    rank = models.PositiveSmallIntegerField()
    prize_value = models.DecimalField(max_digits=12, decimal_places=2)
    voucher_code = models.CharField(max_length=12, unique=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_booking = models.ForeignKey("travel.Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    claimed_equb_membership = models.ForeignKey("equb.EqubMembership", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["giveaway", "rank"]
        constraints = [models.UniqueConstraint(fields=["giveaway", "rank"], name="uniq_giveaway_winner_rank")]

    def __str__(self):
        return f"{self.giveaway} #{self.rank}: {self.user}"
