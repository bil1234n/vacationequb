import calendar
from datetime import date, timedelta
from decimal import Decimal

from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import TimeStampedModel
from apps.core.utils import generate_code


class EqubSettings(models.Model):
    """
    The Equb rules in force across every pool (a single row, edited from the console).

    Pool-specific money rules (contribution, late fee, grace days, service fee) live on
    each pool; how strictly arrears are treated, reminders and call-centre thresholds are
    company policy and live here, so changing them takes effect everywhere at once.
    """

    warn_at = models.PositiveSmallIntegerField(
        default=1, validators=[MinValueValidator(1)],
        help_text="Missed rounds before a member is warned.")
    block_at = models.PositiveSmallIntegerField(
        default=2, validators=[MinValueValidator(1)],
        help_text="Missed rounds before a member is left out of draws until they catch up.")
    suspend_at = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1)],
        help_text="Missed rounds before a member is suspended and sent to the termination list.")
    auto_flag_for_termination = models.BooleanField(
        default=True, help_text="Put suspended members on the termination list automatically.")
    min_paid_rounds_to_win = models.PositiveSmallIntegerField(
        default=1, help_text="Contributions a member must have paid before they can win a draw.")
    max_advance_rounds = models.PositiveSmallIntegerField(
        default=6, validators=[MaxValueValidator(60)],
        help_text="How many future rounds a member may pay ahead.")
    reminder_days_before = models.PositiveSmallIntegerField(
        default=2, validators=[MaxValueValidator(30)],
        help_text="Days before the due date to send a payment reminder (0 = on the due date).")
    send_overdue_notices = models.BooleanField(
        default=True, help_text="Tell members when they fall behind and when their standing gets worse.")
    call_after_days = models.PositiveSmallIntegerField(
        default=7, help_text="Days overdue before a member appears in the call centre.")
    terminate_after_days = models.PositiveSmallIntegerField(
        default=31, help_text="Days overdue before a member is listed as a termination candidate.")
    # Money rules for every Equb (they used to be set on each package and group).
    service_fee_percent = models.DecimalField(
        max_digits=4, decimal_places=2, default=Decimal("2.00"),
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("20"))],
        help_text="Kept from each winner's payout.")
    late_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0,
                                   help_text="Added to a contribution once the grace days have passed (0 = no late fee).")
    grace_days = models.PositiveSmallIntegerField(default=3, help_text="Days after the due date before a payment is late.")
    require_paid_to_draw = models.BooleanField(default=True, help_text="Only members paid up for the round enter its draw.")
    payout_basis = models.CharField(max_length=10, default="FULL_POT",
                                    choices=[("FULL_POT", "Full pot – Vacation Equb guarantees the payout"),
                                             ("COLLECTED", "Only what was collected by draw day")],
                                    help_text="What each winner receives.")
    auto_start_when_full = models.BooleanField(default=True, help_text="Start an Equb as soon as all its places are taken.")
    # --- winning chance (weighted draw) ---------------------------------------------
    weighted_draw = models.BooleanField(
        default=True, help_text="Give members who pay well a bigger chance in automatic draws. Off = everyone in "
                                "the draw has exactly the same chance.")
    chance_on_time_percent = models.PositiveSmallIntegerField(
        default=50, validators=[MaxValueValidator(500)], help_text="Extra chance for paying this round on or before "
                                                                     "its due date.")
    chance_prepaid_percent = models.PositiveSmallIntegerField(
        default=10, validators=[MaxValueValidator(500)], help_text="Extra chance for having paid future rounds in "
                                                                     "advance.")
    chance_streak_percent = models.PositiveSmallIntegerField(
        default=5, validators=[MaxValueValidator(100)], help_text="Extra chance for every round paid on time in a row.")
    chance_streak_cap_percent = models.PositiveSmallIntegerField(
        default=25, validators=[MaxValueValidator(500)], help_text="Most the on-time streak can add.")
    chance_late_percent = models.PositiveSmallIntegerField(
        default=50, validators=[MaxValueValidator(100)], help_text="Less chance for paying this round after its due "
                                                                     "date.")
    chance_missed_percent = models.PositiveSmallIntegerField(
        default=25, validators=[MaxValueValidator(100)], help_text="Less chance for every earlier round still unpaid.")
    chance_min_percent = models.PositiveSmallIntegerField(
        default=10, validators=[MinValueValidator(1), MaxValueValidator(100)],
        help_text="Nobody in the draw goes below this share of a normal chance.")
    pending_payment_minutes = models.PositiveSmallIntegerField(
        default=3, validators=[MinValueValidator(1), MaxValueValidator(1440)],
        help_text="An app payment that is still unpaid after this many minutes is cancelled automatically "
                  "(Chapa is checked first, so nothing that was paid is lost). Members can also cancel it themselves.")
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        verbose_name = "Equb rules"
        verbose_name_plural = "Equb rules"

    def __str__(self):
        return "Equb rules"

    def save(self, *args, **kwargs):
        self.pk = 1  # singleton
        # Keep the escalation ladder in order, whatever was typed.
        self.block_at = max(self.block_at, self.warn_at)
        self.suspend_at = max(self.suspend_at, self.block_at)
        super().save(*args, **kwargs)

    @classmethod
    def load(cls) -> "EqubSettings":
        obj = cls.objects.filter(pk=1).first()
        if obj is None:
            obj = cls(pk=1)
            obj.save()
        return obj


RESERVED_GROUP_SLUGS = {"new", "group-equb", "packages", "memberships", "collections", "call-center", "terminations", "reports",
                        "payouts", "member", "draws", "group-equbs"}

FREQUENCY_CHOICES = [("DAILY", "Daily"), ("WEEKLY", "Weekly"), ("BIWEEKLY", "Every two weeks"), ("MONTHLY", "Monthly"),
                     ("CUSTOM", "Every few days")]
FREQUENCY_DAYS = {"DAILY": 1, "WEEKLY": 7, "BIWEEKLY": 14, "MONTHLY": 30}


def is_monthly(days: int) -> bool:
    """28–31 days is treated as a calendar month (due on the same date every month)."""
    return 28 <= (days or 0) <= 31


def frequency_code(days: int) -> str:
    if is_monthly(days):
        return "MONTHLY"
    return {1: "DAILY", 7: "WEEKLY", 14: "BIWEEKLY"}.get(days or 0, "CUSTOM")


def frequency_label(days: int) -> str:
    """Daily / Weekly / Every two weeks / Monthly / Every 10 days."""
    code = frequency_code(days)
    return f"Every {days} days" if code == "CUSTOM" else dict(FREQUENCY_CHOICES)[code]


def every_phrase(days: int) -> str:
    code = frequency_code(days)
    return {"DAILY": "every day", "WEEKLY": "every week", "BIWEEKLY": "every two weeks",
            "MONTHLY": "every month"}.get(code, f"every {days} days")


def add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


class DurationUnit(models.TextChoices):
    DAYS = "DAYS", "Days"
    WEEKS = "WEEKS", "Weeks"
    MONTHS = "MONTHS", "Months"


# One round lasts one Duration Unit: Days → members pay every day, Weeks → every week, Months → every month.
ROUND_DAYS = {DurationUnit.DAYS: 1, DurationUnit.WEEKS: 7, DurationUnit.MONTHS: 30}


def duration_end(start: date, value: int, unit: str) -> date:
    if unit == DurationUnit.MONTHS:
        return add_months(start, value)
    return start + timedelta(days=value * (7 if unit == DurationUnit.WEEKS else 1))


def rounds_for(start: date, frequency_days: int, value: int, unit: str) -> int:
    """How many contributions (= rounds = draws) fit in the duration."""
    if not (value and frequency_days):
        return 0
    if unit == DurationUnit.MONTHS and is_monthly(frequency_days):
        return value
    days = (duration_end(start, value, unit) - start).days
    return max(days // frequency_days, 1)


class EqubPackage(TimeStampedModel):
    """
    The template Equb groups are created from (like NiyaEqubBank's Equb Package): a name, its terms and whether
    it is active. Amount, frequency, members and rounds are set on each group.
    """

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    terms_content = models.TextField(blank=True, help_text="Terms and conditions shown before joining (one per line).")
    is_active = models.BooleanField(default=True, help_text="Inactive packages can't get new groups.")

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(self.name)[:120] or "equb-package"
            slug, i = base, 2
            while EqubPackage.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    def group_defaults(self) -> dict:
        """What a new group copies from this package."""
        return {"rules_text": self.terms_content}


class EqubGroup(TimeStampedModel):
    """A rotating savings circle (ዕቁብ) created from an Equb package."""

    class Frequency(models.TextChoices):
        DAILY = "DAILY", "Daily"
        WEEKLY = "WEEKLY", "Weekly"
        BIWEEKLY = "BIWEEKLY", "Every two weeks"
        MONTHLY = "MONTHLY", "Monthly"
        CUSTOM = "CUSTOM", "Every few days"

    class DrawType(models.TextChoices):
        MANUAL = "MANUAL", "Manual – staff run or pick the winner"
        AUTOMATIC = "AUTOMATIC", "Automatic – fair random draw on the draw date"

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        OPEN = "OPEN", "Open for members"
        ACTIVE = "ACTIVE", "Running"
        COMPLETED = "COMPLETED", "Completed"
        CANCELLED = "CANCELLED", "Cancelled"

    class PayoutBasis(models.TextChoices):
        FULL_POT = "FULL_POT", "Full pot – Vacation Equb guarantees the payout"
        COLLECTED = "COLLECTED", "Only what was collected by draw day"

    equb_package = models.ForeignKey(EqubPackage, null=True, blank=True, on_delete=models.PROTECT,
                                     related_name="groups", help_text="The package this group was created from.")
    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    rules_text = models.TextField(blank=True, help_text="Terms shown to members before they join.")

    contribution_amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("1"))])
    frequency_days = models.PositiveSmallIntegerField(
        "Contribution frequency (days)", default=30, validators=[MinValueValidator(1), MaxValueValidator(365)])
    frequency = models.CharField(max_length=8, choices=Frequency.choices, default=Frequency.MONTHLY, editable=False)
    duration_value = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1)])
    duration_unit = models.CharField(max_length=6, choices=DurationUnit.choices, default=DurationUnit.MONTHS)
    draw_type = models.CharField(max_length=10, choices=DrawType.choices, default=DrawType.MANUAL)
    max_members = models.PositiveSmallIntegerField(validators=[MinValueValidator(2), MaxValueValidator(10000)],
                                                   help_text="Members in this Equb.")
    rounds_drawn = models.PositiveSmallIntegerField(
        "Rounds drawn", null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(10000)],
        help_text="How many draws the Equb has. Members ÷ rounds = winners in each draw. Empty = one draw per member.")

    service_fee_override = models.DecimalField(
        "Service fee (%)", max_digits=4, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("20"))],
        help_text="Kept from each winner's payout. Leave empty to use the Equb rules default.")
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN, db_index=True)
    current_round = models.PositiveSmallIntegerField(default=0)
    organizer = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="organized_equbs")
    join_code = models.CharField(max_length=8, unique=True, null=True, blank=True,
                                 help_text="Members can join with this code (needed for private pools).")

    class Meta:
        ordering = ["start_date"]

    def __str__(self):
        return self.name

    def get_frequency_display(self) -> str:
        return frequency_label(self.frequency_days)

    @property
    def every(self) -> str:
        return every_phrase(self.frequency_days)

    # --- company-wide rules (Equb rules page), read through the group so every Equb follows them -------------
    @property
    def rules(self) -> "EqubSettings":
        if not hasattr(self, "_rules"):
            self._rules = EqubSettings.load()
        return self._rules

    @property
    def service_fee_percent(self) -> Decimal:
        """This Equb's own service fee when staff set one, otherwise the Equb rules default."""
        if self.service_fee_override is not None:
            return self.service_fee_override
        return self.rules.service_fee_percent

    @property
    def late_fee(self) -> Decimal:
        return self.rules.late_fee

    @property
    def grace_days(self) -> int:
        return self.rules.grace_days

    @property
    def require_paid_to_draw(self) -> bool:
        return self.rules.require_paid_to_draw

    @property
    def payout_basis(self) -> str:
        return self.rules.payout_basis

    def get_payout_basis_display(self) -> str:
        return dict(self.PayoutBasis.choices).get(self.payout_basis, self.payout_basis)

    @property
    def auto_start_when_full(self) -> bool:
        return self.rules.auto_start_when_full

    @property
    def winners_per_round(self) -> int:
        """Members ÷ rounds drawn (rounded up): 12 members over 3 rounds → 4 winners each draw."""
        if not self.rounds_drawn or self.rounds_drawn >= self.max_members:
            return 1
        return -(-self.max_members // self.rounds_drawn)

    @property
    def payment_rounds(self) -> int:
        """How many times each member pays (winners too): the Duration Value – 70,000 × 3 = 210,000 per person."""
        return self.duration_value or 0

    @property
    def price_per_person(self) -> Decimal:
        """What each member pays in total: contribution amount × duration value (70,000 × 3 = 210,000)."""
        return (self.contribution_amount or 0) * self.payment_rounds
    min_members_to_start = 0     # staff can start an Equb at any time, even before anyone has joined
    is_public = True             # every Equb is listed in the app; Group Equbs are joined with their code

    @property
    def planned_rounds(self) -> int:
        """Rounds that fit in the chosen duration (0 if no duration was set)."""
        return rounds_for(self.start_date, self.frequency_days, self.duration_value, self.duration_unit) \
            if self.start_date else 0

    @property
    def duration_label(self) -> str:
        if not self.duration_value:
            return ""
        unit = self.get_duration_unit_display().lower()
        return f"{self.duration_value} {unit[:-1] if self.duration_value == 1 else unit}"

    def save(self, *args, **kwargs):
        if self._state.adding and self.frequency in ("DAILY", "WEEKLY", "BIWEEKLY") and self.frequency_days == 30:
            self.frequency_days = FREQUENCY_DAYS[self.frequency]  # created with an old-style code ("WEEKLY")
        if self.duration_value and self.duration_unit in ROUND_DAYS:
            self.frequency_days = ROUND_DAYS[self.duration_unit]  # the round length follows the Duration Unit
        self.frequency = frequency_code(self.frequency_days)
        if self.duration_value and self.start_date and not self.end_date:
            self.end_date = duration_end(self.start_date, self.duration_value, self.duration_unit)
        if not self.slug:
            base = slugify(self.name)[:120] or "equb"
            if base in RESERVED_GROUP_SLUGS:  # these are console pages under /console/equb/
                base = f"{base}-equb"
            slug, i = base, 2
            while EqubGroup.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        if not self.join_code:
            self.join_code = self.new_join_code()
        super().save(*args, **kwargs)

    @staticmethod
    def new_join_code() -> str:
        code = generate_code(6)
        while EqubGroup.objects.filter(join_code=code).exists():
            code = generate_code(6)
        return code

    @property
    def members_count(self) -> int:
        return self.memberships.exclude(status=EqubMembership.Status.LEFT).count()

    @property
    def total_rounds(self) -> int:
        if self.status not in (self.Status.DRAFT, self.Status.OPEN):
            scheduled = self.rounds.count()
            if scheduled:
                return scheduled
        # Each member wins exactly once (several winners per round shortens the draws), and everyone –
        # winners included – pays every round up to the duration (price per person = contribution × duration).
        draws = self.rounds_drawn or -(-self.planned_members // self.winners_per_round)
        return max(draws, self.payment_rounds)

    @property
    def planned_members(self) -> int:
        """Members the pot is built from: the target size until the Equb starts, the real count after."""
        return self.max_members if self.status in (self.Status.DRAFT, self.Status.OPEN) else self.members_count

    @property
    def pot_per_round(self) -> Decimal:
        return self.contribution_amount * self.planned_members

    @property
    def payout_per_winner(self) -> Decimal:
        # Each winner receives the full price per person (contribution × duration) minus the service fee.
        gross = Decimal(self.price_per_person) if self.payment_rounds else self.pot_per_round / self.winners_per_round
        return (gross * (1 - self.service_fee_percent / 100)).quantize(Decimal("0.01"))

    @property
    def is_running(self) -> bool:
        return self.status == self.Status.ACTIVE


class EqubMembership(TimeStampedModel):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        WON = "WON", "Won – still contributing"
        DEFAULTED = "DEFAULTED", "Terminated"
        LEFT = "LEFT", "Left"

    LIVE_STATUSES = (Status.ACTIVE, Status.WON)

    group = models.ForeignKey(EqubGroup, on_delete=models.CASCADE, related_name="memberships")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="equb_memberships",
                             help_text="The customer – or, for a responsibility person, the owner who pays for them.")
    team = models.ForeignKey("GroupEqub", null=True, blank=True, on_delete=models.SET_NULL, related_name="memberships",
                             help_text="The Group Equb (family, friends, company…) this place belongs to.")
    dependent_name = models.CharField(max_length=120, blank=True,
                                      help_text="Responsibility person: someone not on the app; the owner pays for them.")
    dependent_phone = models.CharField(max_length=20, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ACTIVE)
    position = models.PositiveSmallIntegerField(help_text="Join order – used for canonical draw ordering")
    has_won = models.BooleanField(default=False)
    won_round = models.ForeignKey("EqubRound", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    total_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    missed_count = models.PositiveSmallIntegerField(default=0)
    prepaid_credit = models.DecimalField(max_digits=12, decimal_places=2, default=0,
                                         help_text="Credit (advance payments, lucky-draw prizes) auto-applied to future rounds")

    # Draw control by staff (investigations, disputes) – independent of payment history.
    draw_blocked_until = models.DateField(null=True, blank=True)
    draw_block_reason = models.CharField(max_length=255, blank=True)
    # Call centre & collections
    called_at = models.DateTimeField(null=True, blank=True)
    called_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    termination_requested_at = models.DateTimeField(null=True, blank=True)
    termination_note = models.CharField(max_length=255, blank=True)
    # Member asked to leave a running Equb
    exit_requested_at = models.DateTimeField(null=True, blank=True)
    exit_request_reason = models.CharField(max_length=500, blank=True)
    # Last standing we told the member about (so escalation notices go out once).
    standing_notified = models.CharField(max_length=16, blank=True)
    notes = models.TextField(blank=True, help_text="Internal staff notes")
    ended_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["group", "position"]
        constraints = [
            models.UniqueConstraint(fields=["group", "user"], condition=models.Q(dependent_name=""),
                                    name="uniq_equb_member"),
            models.UniqueConstraint(fields=["group", "position"], name="uniq_equb_position"),
        ]

    def __str__(self):
        return f"{self.user} in {self.group}"

    @property
    def is_live(self) -> bool:
        return self.status in self.LIVE_STATUSES

    @property
    def is_dependent(self) -> bool:
        return bool(self.dependent_name)

    @property
    def display_name(self) -> str:
        """The member's name – for a responsibility person, their own name."""
        return self.dependent_name or self.user.full_name

    @property
    def is_draw_blocked(self) -> bool:
        return bool(self.draw_blocked_until and self.draw_blocked_until >= timezone.localdate())


class GroupEqub(TimeStampedModel):
    """
    A Group Equb (NiyaEqubBank style): a family, friends, a company team or a community that joins an Equb together.
    The owner is the first member; members join with the group's invite code (or are added by staff); responsibility
    people are relatives without the app – each takes a place in the Equb and the owner pays for them.
    """

    class Kind(models.TextChoices):
        FAMILY = "FAMILY", "Family"
        FRIENDS = "FRIENDS", "Friends"
        WORKPLACE = "WORKPLACE", "Company / workplace"
        COMMUNITY = "COMMUNITY", "Community / association"

    name = models.CharField(max_length=120)
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.FAMILY)
    equb = models.ForeignKey(EqubGroup, on_delete=models.PROTECT, related_name="teams",
                             help_text="The Equb this group is joining.")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="owned_group_equbs")
    description = models.TextField(blank=True)
    invite_code = models.CharField(max_length=8, unique=True, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Group Equb"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.invite_code:
            self.invite_code = self.new_code()
        super().save(*args, **kwargs)

    @staticmethod
    def new_code() -> str:
        code = "G" + generate_code(5)
        while GroupEqub.objects.filter(invite_code=code).exists() or EqubGroup.objects.filter(join_code=code).exists():
            code = "G" + generate_code(5)
        return code

    @property
    def live_memberships(self):
        return self.memberships.exclude(status=EqubMembership.Status.LEFT)


class GroupEqubInvite(TimeStampedModel):
    """
    The owner asks a vacation customer (found by phone number) to join the group. Nothing happens until the
    person accepts; then they get their own place in the Equb and pay their own contributions.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for an answer"
        ACCEPTED = "ACCEPTED", "Accepted"
        DECLINED = "DECLINED", "Declined"
        CANCELLED = "CANCELLED", "Cancelled"

    team = models.ForeignKey(GroupEqub, on_delete=models.CASCADE, related_name="invites")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="group_invites")
    invited_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    membership = models.ForeignKey("EqubMembership", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["team", "user"], condition=models.Q(status="PENDING"),
                                               name="uniq_pending_group_invite")]

    def __str__(self):
        return f"{self.user} → {self.team} ({self.status})"


class EqubRound(TimeStampedModel):
    class Status(models.TextChoices):
        UPCOMING = "UPCOMING", "Upcoming"
        COLLECTING = "COLLECTING", "Collecting contributions"
        DRAWN = "DRAWN", "Winner drawn"

    group = models.ForeignKey(EqubGroup, on_delete=models.CASCADE, related_name="rounds")
    number = models.PositiveSmallIntegerField()
    due_date = models.DateField()
    draw_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UPCOMING)
    expected_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    collected_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    draw_note = models.CharField(max_length=255, blank=True, help_text="e.g. why the draw was postponed")
    dates_fixed = models.BooleanField(default=False, help_text="Staff set this round's dates by hand – the automatic "
                                                               "schedule leaves them alone.")

    class Meta:
        ordering = ["group", "number"]
        constraints = [models.UniqueConstraint(fields=["group", "number"], name="uniq_round_number")]

    def __str__(self):
        return f"{self.group} – round {self.number}"


class Contribution(TimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PAID = "PAID", "Paid"
        LATE = "LATE", "Late"
        WAIVED = "WAIVED", "Paid from credit"

    SETTLED = (Status.PAID, Status.WAIVED)
    UNSETTLED = (Status.PENDING, Status.LATE)

    round = models.ForeignKey(EqubRound, on_delete=models.CASCADE, related_name="contributions")
    membership = models.ForeignKey(EqubMembership, on_delete=models.CASCADE, related_name="contributions")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    late_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    late_fee_waived = models.BooleanField(default=False)
    status = models.CharField(max_length=8, choices=Status.choices, default=Status.PENDING, db_index=True)
    paid_at = models.DateTimeField(null=True, blank=True)
    payment = models.ForeignKey("payments.PaymentTransaction", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+")
    payment_record = models.ForeignKey("EqubPayment", null=True, blank=True, on_delete=models.SET_NULL,
                                       related_name="settled_contributions")
    reminded_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=160, blank=True)

    class Meta:
        ordering = ["round", "membership__position"]
        constraints = [models.UniqueConstraint(fields=["round", "membership"], name="uniq_contribution")]

    def __str__(self):
        return f"{self.membership.user} R{self.round.number} {self.status}"

    @property
    def amount_due(self):
        return self.amount + self.late_fee

    @property
    def is_settled(self) -> bool:
        return self.status in self.SETTLED


def _receipt_no():
    return f"EQ{timezone.localtime():%y%m}-{generate_code(6)}"


class EqubPayment(TimeStampedModel):
    """
    Money paid towards one membership – through a gateway in the app, or recorded by staff
    at the office. It settles the member's oldest unpaid rounds first (late fees included)
    and anything left over becomes credit for the next rounds.
    """

    class Method(models.TextChoices):
        TELEBIRR = "telebirr", "telebirr"
        CHAPA = "chapa", "Chapa"
        MOCK = "mock", "Sandbox"
        CASH = "cash", "Cash (office)"
        BANK = "bank", "Bank transfer"
        MOBILE = "mobile", "Mobile money (manual)"

    OFFICE_METHODS = (Method.CASH, Method.BANK, Method.MOBILE)

    class Status(models.TextChoices):
        PENDING = "PENDING", "Awaiting payment"
        PAID = "PAID", "Paid"
        FAILED = "FAILED", "Failed"
        CANCELLED = "CANCELLED", "Cancelled"

    reference = models.CharField(max_length=16, unique=True, default=_receipt_no, editable=False)
    membership = models.ForeignKey(EqubMembership, on_delete=models.PROTECT, related_name="payments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="equb_payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal("0.01"))])
    rounds_requested = models.PositiveSmallIntegerField(default=1)
    method = models.CharField(max_length=10, choices=Method.choices)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    transaction = models.ForeignKey("payments.PaymentTransaction", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    external_reference = models.CharField(max_length=64, blank=True, help_text="Bank / mobile-money reference")
    recorded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    paid_at = models.DateTimeField(null=True, blank=True)
    allocation = models.JSONField(default=dict, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.reference} – {self.amount}"

    @property
    def is_office(self) -> bool:
        return self.method in self.OFFICE_METHODS


class EqubDraw(TimeStampedModel):
    """Commit/reveal audit record for a round's draw."""

    round = models.OneToOneField(EqubRound, on_delete=models.CASCADE, related_name="draw")
    seed_commitment = models.CharField(max_length=64, help_text="sha256(server_seed) published before the draw")
    server_seed = models.CharField(max_length=64, help_text="Revealed after the draw")
    public_input = models.TextField(blank=True)
    eligible_memberships = models.JSONField(default=list, blank=True)
    winner_indices = models.JSONField(default=list, blank=True)
    pool_snapshot = models.JSONField(default=dict, blank=True,
                                     help_text="Who was in the draw, who was left out and why")
    executed_at = models.DateTimeField(null=True, blank=True)
    executed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    weights = models.JSONField(default=list, blank=True,
                               help_text="Chance weight of each eligible member (weighted draw), in pool order.")
    mode = models.CharField(max_length=8, choices=[("RANDOM", "Random draw"), ("WEIGHTED", "Weighted random draw"),
                                                   ("SELECTED", "Selected by staff")],
                            default="RANDOM")
    note = models.CharField(max_length=255, blank=True, help_text="Why staff chose these winners")

    @property
    def is_selected(self):
        return self.mode == "SELECTED"

    @property
    def is_executed(self):
        return self.executed_at is not None


class EqubPayout(TimeStampedModel):
    class Status(models.TextChoices):
        ISSUED = "ISSUED", "Issued – ready to redeem"
        REDEEMED = "REDEEMED", "Redeemed on a booking"
        PAID_OUT = "PAID_OUT", "Paid out in cash"

    round = models.ForeignKey(EqubRound, on_delete=models.CASCADE, related_name="payouts")
    membership = models.OneToOneField(EqubMembership, on_delete=models.PROTECT, related_name="payout")
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    gross_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    fee_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)
    voucher_code = models.CharField(max_length=12, unique=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ISSUED)
    booking = models.ForeignKey("travel.Booking", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    redeemed_at = models.DateTimeField(null=True, blank=True)
    paid_out_at = models.DateTimeField(null=True, blank=True)
    paid_out_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    paid_out_method = models.CharField(max_length=20, blank=True)
    paid_out_reference = models.CharField(max_length=64, blank=True)

    def __str__(self):
        return f"{self.voucher_code} – {self.amount}"


class CallLog(TimeStampedModel):
    """One collections call to a member who is behind."""

    class Outcome(models.TextChoices):
        PROMISED = "PROMISED", "Promised to pay"
        PAID = "PAID", "Says already paid"
        CALL_BACK = "CALL_BACK", "Asked to call back"
        NO_ANSWER = "NO_ANSWER", "No answer"
        UNREACHABLE = "UNREACHABLE", "Phone off / unreachable"
        WRONG_NUMBER = "WRONG_NUMBER", "Wrong number"
        HARDSHIP = "HARDSHIP", "Financial difficulty"
        WANTS_TO_LEAVE = "WANTS_TO_LEAVE", "Wants to leave the Equb"
        REFUSED = "REFUSED", "Refuses to pay"
        OTHER = "OTHER", "Other"

    TONES = {"PROMISED": "gold", "PAID": "green", "CALL_BACK": "gold", "NO_ANSWER": "grey", "UNREACHABLE": "red",
             "WRONG_NUMBER": "red", "HARDSHIP": "gold", "WANTS_TO_LEAVE": "red", "REFUSED": "red", "OTHER": "grey"}

    membership = models.ForeignKey(EqubMembership, on_delete=models.CASCADE, related_name="call_logs")
    called_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                  related_name="+")
    outcome = models.CharField(max_length=16, choices=Outcome.choices)
    promised_date = models.DateField(null=True, blank=True)
    feedback = models.TextField(blank=True)
    arrears_at_call = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    days_overdue_at_call = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.membership} – {self.get_outcome_display()}"

    @property
    def tone(self):
        return self.TONES.get(self.outcome, "grey")


class Termination(TimeStampedModel):
    """A member removed from a running Equb (usually for non-payment), with the position at that moment."""

    class Reason(models.TextChoices):
        NON_PAYMENT = "NON_PAYMENT", "Did not pay"
        MEMBER_REQUEST = "MEMBER_REQUEST", "Member asked to leave"
        UNREACHABLE = "UNREACHABLE", "Could not be reached"
        FRAUD = "FRAUD", "Fraud or duplicate account"
        OTHER = "OTHER", "Other"

    membership = models.ForeignKey(EqubMembership, on_delete=models.CASCADE, related_name="terminations")
    terminated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    reason = models.CharField(max_length=16, choices=Reason.choices)
    note = models.TextField(blank=True)
    arrears_at_termination = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    paid_at_termination = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    missed_rounds = models.PositiveSmallIntegerField(default=0)
    days_overdue = models.PositiveIntegerField(default=0)
    had_won = models.BooleanField(default=False)
    member_notified = models.BooleanField(default=False)
    reinstated_at = models.DateTimeField(null=True, blank=True)
    reinstated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                      related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.membership} – {self.get_reason_display()}"

    @property
    def is_active(self) -> bool:
        return self.reinstated_at is None


class ScheduledPayment(TimeStampedModel):
    """
    "Calendar pay": a member picks the day and time they want to pay. At that moment we create the
    Chapa checkout for the rounds they chose and send it to their phone; they confirm it in Chapa
    (telebirr / CBE Birr / bank OTP). With ``repeat`` it is set up again for the next due date.
    """

    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        SENT = "SENT", "Bill sent – waiting for payment"
        PAID = "PAID", "Paid"
        SKIPPED = "SKIPPED", "Nothing was due"
        FAILED = "FAILED", "Could not be sent"
        CANCELLED = "CANCELLED", "Cancelled"

    class Repeat(models.TextChoices):
        ONCE = "ONCE", "Only once"
        EVERY_DUE = "EVERY_DUE", "Every due date"

    OPEN_STATUSES = (Status.SCHEDULED, Status.SENT)

    membership = models.ForeignKey(EqubMembership, on_delete=models.CASCADE, related_name="scheduled_payments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="scheduled_equb_payments")
    scheduled_for = models.DateTimeField(db_index=True)
    rounds = models.PositiveSmallIntegerField(default=1, validators=[MinValueValidator(1), MaxValueValidator(60)])
    repeat = models.CharField(max_length=10, choices=Repeat.choices, default=Repeat.ONCE)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                 help_text="Filled in when the bill is sent.")
    transaction = models.ForeignKey("payments.PaymentTransaction", null=True, blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    sent_at = models.DateTimeField(null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["scheduled_for"]

    def __str__(self):
        return f"{self.membership} · {self.scheduled_for:%d %b %Y %H:%M}"


class JoinRequest(TimeStampedModel):
    """A member who was removed from (or left) a started Equb asks to come back; staff approve or decline."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Waiting for approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Declined"
        CANCELLED = "CANCELLED", "Withdrawn"

    group = models.ForeignKey(EqubGroup, on_delete=models.CASCADE, related_name="join_requests")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="equb_join_requests")
    membership = models.ForeignKey(EqubMembership, null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
                                   help_text="The earlier place this person had in the Equb.")
    message = models.CharField(max_length=500, blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING, db_index=True)
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    staff_note = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["group", "user"], condition=models.Q(status="PENDING"),
                                               name="uniq_pending_join_request")]

    def __str__(self):
        return f"{self.user} → {self.group} ({self.status})"
