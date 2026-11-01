from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.utils import timezone
from django.utils.text import slugify

from apps.core.models import TimeStampedModel
from apps.core.utils import generate_code


class Cabin(models.TextChoices):
    ECONOMY = "ECONOMY", "Economy"
    PREMIUM = "PREMIUM", "Premium Economy"
    BUSINESS = "BUSINESS", "Business"
    FIRST = "FIRST", "First"


class Airport(models.Model):
    iata = models.CharField(max_length=3, unique=True)
    name = models.CharField(max_length=120)
    city = models.CharField(max_length=80)
    country = models.CharField(max_length=80)
    timezone = models.CharField(max_length=48, default="UTC")

    class Meta:
        ordering = ["city"]

    def __str__(self):
        return f"{self.city} ({self.iata})"

    def save(self, *args, **kwargs):
        self.iata = self.iata.upper()
        super().save(*args, **kwargs)


class AircraftType(models.Model):
    """
    Seat-map template. ``layout`` is a list of cabin blocks, e.g.::

        [{"cabin": "BUSINESS", "rows": [1, 4],  "letters": "AC|DF",  "surcharge": 0},
         {"cabin": "ECONOMY",  "rows": [10, 35], "letters": "ABC|DEF",
          "exit_rows": [20], "extra_legroom_rows": [10, 20], "extra_legroom_surcharge": 1500,
          "window_surcharge": 300, "aisle_surcharge": 200}]

    ``|`` marks an aisle. Window = outermost letters, aisle = letters next to ``|``.
    """

    code = models.CharField(max_length=10, unique=True)  # e.g. B738, B788, A359
    name = models.CharField(max_length=80)
    layout = models.JSONField()

    def __str__(self):
        return f"{self.name} ({self.code})"

    def clean(self):
        if not isinstance(self.layout, list) or not self.layout:
            raise ValidationError({"layout": "Layout must be a non-empty list of cabin blocks."})
        for block in self.layout:
            if block.get("cabin") not in Cabin.values:
                raise ValidationError({"layout": f"Unknown cabin {block.get('cabin')}"})
            start, end = block["rows"]
            if start > end:
                raise ValidationError({"layout": "rows must be [start, end]"})

    @staticmethod
    def positions_for(letters: str) -> dict[str, str]:
        groups = letters.split("|")
        seq = letters.replace("|", "")
        positions = {}
        for gi, group in enumerate(groups):
            for li, letter in enumerate(group):
                if letter in (seq[0], seq[-1]):
                    pos = Seat.Position.WINDOW
                elif (li == len(group) - 1 and gi < len(groups) - 1) or (li == 0 and gi > 0):
                    pos = Seat.Position.AISLE
                else:
                    pos = Seat.Position.MIDDLE
                positions[letter] = pos
        return positions

    def build_seats(self, flight: "Flight") -> list["Seat"]:
        seats = []
        for block in self.layout:
            positions = self.positions_for(block["letters"])
            start, end = block["rows"]
            exit_rows = set(block.get("exit_rows", []))
            legroom_rows = set(block.get("extra_legroom_rows", []))
            for row in range(start, end + 1):
                for letter in block["letters"].replace("|", ""):
                    pos = positions[letter]
                    surcharge = Decimal(block.get("surcharge", 0))
                    if row in legroom_rows:
                        surcharge += Decimal(block.get("extra_legroom_surcharge", 0))
                    if pos == Seat.Position.WINDOW:
                        surcharge += Decimal(block.get("window_surcharge", 0))
                    elif pos == Seat.Position.AISLE:
                        surcharge += Decimal(block.get("aisle_surcharge", 0))
                    seats.append(Seat(
                        flight=flight, row=row, letter=letter, number=f"{row}{letter}", cabin=block["cabin"],
                        position=pos, is_exit_row=row in exit_rows, extra_legroom=row in legroom_rows,
                        surcharge=surcharge,
                    ))
        return seats


class Flight(TimeStampedModel):
    class Status(models.TextChoices):
        SCHEDULED = "SCHEDULED", "Scheduled"
        BOARDING = "BOARDING", "Boarding"
        DEPARTED = "DEPARTED", "Departed"
        ARRIVED = "ARRIVED", "Arrived"
        DELAYED = "DELAYED", "Delayed"
        CANCELLED = "CANCELLED", "Cancelled"

    airline_code = models.CharField(max_length=3, default="ET")
    airline_name = models.CharField(max_length=80, default="Ethiopian Airlines")
    flight_number = models.CharField(max_length=8)
    origin = models.ForeignKey(Airport, on_delete=models.PROTECT, related_name="departures")
    destination = models.ForeignKey(Airport, on_delete=models.PROTECT, related_name="arrivals")
    departure_time = models.DateTimeField(db_index=True)
    arrival_time = models.DateTimeField()
    aircraft = models.ForeignKey(AircraftType, on_delete=models.PROTECT)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.SCHEDULED, db_index=True)
    is_international = models.BooleanField(default=True)
    terminal = models.CharField(max_length=8, blank=True)
    gate = models.CharField(max_length=8, blank=True)

    class Meta:
        ordering = ["departure_time"]
        constraints = [models.UniqueConstraint(fields=["flight_number", "departure_time"], name="uniq_flight_departure")]
        indexes = [models.Index(fields=["origin", "destination", "departure_time"])]

    def __str__(self):
        return f"{self.flight_number} {self.origin.iata}->{self.destination.iata} {self.departure_time:%Y-%m-%d %H:%M}"

    def clean(self):
        if self.origin_id and self.origin_id == self.destination_id:
            raise ValidationError("Origin and destination must differ.")
        if self.arrival_time and self.departure_time and self.arrival_time <= self.departure_time:
            raise ValidationError("Arrival must be after departure.")

    @property
    def duration_minutes(self) -> int:
        return int((self.arrival_time - self.departure_time).total_seconds() // 60)

    @property
    def is_bookable(self) -> bool:
        return self.status in (self.Status.SCHEDULED, self.Status.DELAYED) and self.departure_time > timezone.now() + timedelta(hours=2)

    def generate_seats(self) -> int:
        if self.seats.exists():
            return 0
        return len(Seat.objects.bulk_create(self.aircraft.build_seats(self)))


class FareClass(models.Model):
    """A bookable fare bucket on a flight (e.g. Economy Saver, Economy Flex, Business)."""

    flight = models.ForeignKey(Flight, on_delete=models.CASCADE, related_name="fares")
    cabin = models.CharField(max_length=10, choices=Cabin.choices)
    code = models.CharField(max_length=2, help_text="RBD, e.g. Y, M, J")
    name = models.CharField(max_length=40)
    base_fare = models.DecimalField(max_digits=12, decimal_places=2, validators=[MinValueValidator(0)])
    taxes = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    seats_allocated = models.PositiveIntegerField()
    seats_sold = models.PositiveIntegerField(default=0)
    refundable = models.BooleanField(default=False)
    change_fee = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    checked_baggage_kg = models.PositiveSmallIntegerField(default=23)
    cabin_baggage_kg = models.PositiveSmallIntegerField(default=7)
    free_seat_selection = models.BooleanField(default=False)

    class Meta:
        ordering = ["flight", "base_fare"]
        constraints = [
            models.UniqueConstraint(fields=["flight", "code"], name="uniq_fare_code_per_flight"),
            models.CheckConstraint(condition=models.Q(seats_sold__lte=models.F("seats_allocated")), name="fare_not_oversold"),
        ]

    def __str__(self):
        return f"{self.flight.flight_number} {self.name} ({self.code})"

    @property
    def seats_left(self) -> int:
        return max(self.seats_allocated - self.seats_sold, 0)


class Seat(models.Model):
    class Position(models.TextChoices):
        WINDOW = "WINDOW", "Window"
        MIDDLE = "MIDDLE", "Middle"
        AISLE = "AISLE", "Aisle"

    class Status(models.TextChoices):
        AVAILABLE = "AVAILABLE", "Available"
        LOCKED = "LOCKED", "Temporarily held"
        BOOKED = "BOOKED", "Booked"
        BLOCKED = "BLOCKED", "Blocked by airline"

    flight = models.ForeignKey(Flight, on_delete=models.CASCADE, related_name="seats")
    number = models.CharField(max_length=4)
    row = models.PositiveSmallIntegerField()
    letter = models.CharField(max_length=1)
    cabin = models.CharField(max_length=10, choices=Cabin.choices)
    position = models.CharField(max_length=6, choices=Position.choices)
    is_exit_row = models.BooleanField(default=False)
    extra_legroom = models.BooleanField(default=False)
    surcharge = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.AVAILABLE)
    locked_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="seat_locks")
    locked_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["flight", "row", "letter"]
        constraints = [models.UniqueConstraint(fields=["flight", "number"], name="uniq_seat_per_flight")]
        indexes = [models.Index(fields=["flight", "status"])]

    def __str__(self):
        return f"{self.flight.flight_number} {self.number}"

    def is_free_for(self, user) -> bool:
        if self.status == self.Status.AVAILABLE:
            return True
        if self.status == self.Status.LOCKED:
            return self.locked_by_id == user.id or (self.locked_until and self.locked_until <= timezone.now())
        return False


class BaggageOption(models.Model):
    code = models.CharField(max_length=12, unique=True)
    name = models.CharField(max_length=60)
    weight_kg = models.PositiveSmallIntegerField()
    price = models.DecimalField(max_digits=10, decimal_places=2)
    international_only = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["weight_kg"]

    def __str__(self):
        return f"{self.name} (+{self.weight_kg}kg)"


class TravelPackage(TimeStampedModel):
    class Category(models.TextChoices):
        HAJJ = "HAJJ", "Hajj"
        UMRAH = "UMRAH", "Umrah"
        DUBAI = "DUBAI", "Dubai"
        CHINA = "CHINA", "China trade tour"
        TURKEY = "TURKEY", "Turkey"
        OTHER = "OTHER", "Other"

    name = models.CharField(max_length=120)
    slug = models.SlugField(max_length=140, unique=True, blank=True)
    category = models.CharField(max_length=8, choices=Category.choices, db_index=True)
    destination_city = models.CharField(max_length=80)
    destination_country = models.CharField(max_length=80)
    summary = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    duration_days = models.PositiveSmallIntegerField()
    price = models.DecimalField(max_digits=12, decimal_places=2)
    deposit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    departure_date = models.DateField()
    return_date = models.DateField()
    capacity = models.PositiveIntegerField()
    seats_sold = models.PositiveIntegerField(default=0)
    includes = models.JSONField(default=list, blank=True, help_text='e.g. ["Visa", "Return flight", "4★ hotel"]')
    hotel_name = models.CharField(max_length=120, blank=True)
    hotel_stars = models.PositiveSmallIntegerField(null=True, blank=True)
    outbound_flight = models.ForeignKey(Flight, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    return_flight = models.ForeignKey(Flight, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    image_url = models.URLField(blank=True)
    is_featured = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["departure_date"]
        constraints = [models.CheckConstraint(condition=models.Q(seats_sold__lte=models.F("capacity")), name="package_not_oversold")]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            base = slugify(f"{self.name}-{self.departure_date}")[:120]
            slug, i = base, 2
            while TravelPackage.objects.filter(slug=slug).exclude(pk=self.pk).exists():
                slug, i = f"{base}-{i}", i + 1
            self.slug = slug
        super().save(*args, **kwargs)

    @property
    def seats_left(self):
        return max(self.capacity - self.seats_sold, 0)


def _pnr():
    return generate_code(6)


class Booking(TimeStampedModel):
    class Type(models.TextChoices):
        FLIGHT = "FLIGHT", "Flight"
        PACKAGE = "PACKAGE", "Package"

    class Status(models.TextChoices):
        PENDING_PAYMENT = "PENDING_PAYMENT", "Awaiting payment"
        CONFIRMED = "CONFIRMED", "Confirmed & ticketed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired (unpaid)"
        REFUNDED = "REFUNDED", "Refunded"

    class Funding(models.TextChoices):
        DIRECT = "DIRECT", "Direct payment"
        EQUB_PAYOUT = "EQUB_PAYOUT", "Equb payout"
        LUCKY_DRAW = "LUCKY_DRAW", "Lucky draw prize"

    reference = models.CharField(max_length=6, unique=True, default=_pnr, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="bookings")
    booking_type = models.CharField(max_length=8, choices=Type.choices)
    flight = models.ForeignKey(Flight, null=True, blank=True, on_delete=models.PROTECT, related_name="bookings")
    fare_class = models.ForeignKey(FareClass, null=True, blank=True, on_delete=models.PROTECT, related_name="bookings")
    package = models.ForeignKey(TravelPackage, null=True, blank=True, on_delete=models.PROTECT, related_name="bookings")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING_PAYMENT, db_index=True)

    fare_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    seat_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    baggage_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="ETB")
    price_breakdown = models.JSONField(default=dict, blank=True)

    funding_source = models.CharField(max_length=12, choices=Funding.choices, default=Funding.DIRECT)
    funding_reference = models.CharField(max_length=64, blank=True)
    contact_phone = models.CharField(max_length=16)
    contact_email = models.EmailField(blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancellation_reason = models.CharField(max_length=255, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="+", help_text="Staff member who cancelled (empty = the customer)")
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_method = models.CharField(max_length=20, blank=True)
    refund_reference = models.CharField(max_length=64, blank=True)
    trip_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(booking_type="FLIGHT", flight__isnull=False, fare_class__isnull=False)
                           | models.Q(booking_type="PACKAGE", package__isnull=False)),
                name="booking_has_product",
            )
        ]

    def __str__(self):
        return f"{self.reference} ({self.get_status_display()})"

    @property
    def amount_due(self) -> Decimal:
        return self.total_amount  # total_amount is already net of discounts

    @property
    def is_payable(self) -> bool:
        return self.status == self.Status.PENDING_PAYMENT and (not self.expires_at or self.expires_at > timezone.now())

    @property
    def departure_at(self):
        """When the trip starts: the flight's departure, or 08:00 local time on a package's departure day."""
        if self.flight_id:
            return self.flight.departure_time
        if self.package_id:
            from datetime import datetime, time
            return timezone.make_aware(datetime.combine(self.package.departure_date, time(8, 0)))
        return None

    @property
    def destination_label(self) -> str:
        if self.flight_id:
            return f"{self.flight.origin.city} → {self.flight.destination.city}"
        return self.package.name if self.package_id else ""

    @property
    def refund_due(self) -> bool:
        """Paid, then cancelled, and the money has not gone back yet."""
        return (self.status == self.Status.CANCELLED and self.confirmed_at is not None
                and self.total_amount > 0 and not self.refunded_at)


class BookingEvent(models.Model):
    """Audit trail for a booking: what happened, when and who did it."""

    class Kind(models.TextChoices):
        CREATED = "CREATED", "Created"
        PAYMENT = "PAYMENT", "Payment"
        CONFIRMED = "CONFIRMED", "Confirmed"
        CANCELLED = "CANCELLED", "Cancelled"
        EXPIRED = "EXPIRED", "Expired"
        REFUNDED = "REFUNDED", "Refunded"
        CHECKED_IN = "CHECKED_IN", "Checked in"
        BOARDED = "BOARDED", "Boarded"
        NOTE = "NOTE", "Note"
        MESSAGE = "MESSAGE", "Message sent"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="events")
    kind = models.CharField(max_length=12, choices=Kind.choices)
    message = models.CharField(max_length=255)
    by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.booking.reference} {self.kind}: {self.message}"


class Passenger(models.Model):
    class Type(models.TextChoices):
        ADULT = "ADT", "Adult"
        CHILD = "CHD", "Child (2-11)"
        INFANT = "INF", "Infant (<2)"

    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="passengers")
    passenger_type = models.CharField(max_length=3, choices=Type.choices, default=Type.ADULT)
    title = models.CharField(max_length=6, blank=True)
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60)
    gender = models.CharField(max_length=1, choices=Gender.choices)
    date_of_birth = models.DateField()
    nationality = models.CharField(max_length=2, default="ET", help_text="ISO-3166 alpha-2")
    passport_number = models.CharField(max_length=32, blank=True)
    passport_expiry = models.DateField(null=True, blank=True)
    phone = models.CharField(max_length=16, blank=True)
    seat = models.OneToOneField(Seat, null=True, blank=True, on_delete=models.SET_NULL, related_name="passenger")
    seat_price = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    special_requests = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ["id"]

    def __str__(self):
        return self.full_name

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class PassengerBaggage(models.Model):
    passenger = models.ForeignKey(Passenger, on_delete=models.CASCADE, related_name="baggage")
    option = models.ForeignKey(BaggageOption, on_delete=models.PROTECT)
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)

    @property
    def total(self):
        return self.unit_price * self.quantity


class Ticket(TimeStampedModel):
    class Status(models.TextChoices):
        ISSUED = "ISSUED", "Issued"
        CHECKED_IN = "CHECKED_IN", "Checked in"
        BOARDED = "BOARDED", "Boarded"
        VOID = "VOID", "Void"

    booking = models.ForeignKey(Booking, on_delete=models.CASCADE, related_name="tickets")
    passenger = models.OneToOneField(Passenger, on_delete=models.CASCADE, related_name="ticket")
    ticket_number = models.CharField(max_length=14, unique=True)
    qr_payload = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.ISSUED)
    checked_in_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    boarded_at = models.DateTimeField(null=True, blank=True)
    boarded_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    def __str__(self):
        return self.ticket_number
