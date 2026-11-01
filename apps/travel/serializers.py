from rest_framework import serializers

from .models import (Airport, BaggageOption, Booking, FareClass, Flight, Passenger, PassengerBaggage, Ticket,
                     TravelPackage)
from .services import pricing


class AirportSerializer(serializers.ModelSerializer):
    class Meta:
        model = Airport
        fields = ["id", "iata", "name", "city", "country", "timezone"]


class FareClassSerializer(serializers.ModelSerializer):
    current_price = serializers.SerializerMethodField()
    seats_left = serializers.IntegerField(read_only=True)
    cabin_display = serializers.CharField(source="get_cabin_display", read_only=True)

    class Meta:
        model = FareClass
        fields = ["id", "cabin", "cabin_display", "code", "name", "base_fare", "current_price", "taxes", "seats_left",
                  "refundable", "change_fee", "checked_baggage_kg", "cabin_baggage_kg", "free_seat_selection"]

    def get_current_price(self, obj) -> str | None:
        return str(pricing.current_fare(obj))


class FlightListSerializer(serializers.ModelSerializer):
    origin = AirportSerializer(read_only=True)
    destination = AirportSerializer(read_only=True)
    duration_minutes = serializers.IntegerField(read_only=True)
    lowest_price = serializers.SerializerMethodField()
    aircraft_name = serializers.CharField(source="aircraft.name", read_only=True)

    class Meta:
        model = Flight
        fields = ["id", "airline_code", "airline_name", "flight_number", "origin", "destination", "departure_time",
                  "arrival_time", "duration_minutes", "status", "is_international", "aircraft_name", "lowest_price"]

    def get_lowest_price(self, obj) -> str | None:
        prices = [pricing.current_fare(f) for f in obj.fares.all() if f.seats_left > 0]
        return str(min(prices)) if prices else None


class FlightDetailSerializer(FlightListSerializer):
    fares = FareClassSerializer(many=True, read_only=True)

    class Meta(FlightListSerializer.Meta):
        fields = FlightListSerializer.Meta.fields + ["terminal", "gate", "fares"]


class SeatLockSerializer(serializers.Serializer):
    seat_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False, max_length=9)


class SeatReleaseSerializer(serializers.Serializer):
    seat_ids = serializers.ListField(child=serializers.IntegerField(), required=False)


class BaggageOptionSerializer(serializers.ModelSerializer):
    class Meta:
        model = BaggageOption
        fields = ["id", "code", "name", "weight_kg", "price", "international_only"]


class TravelPackageSerializer(serializers.ModelSerializer):
    seats_left = serializers.IntegerField(read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)

    class Meta:
        model = TravelPackage
        fields = ["id", "name", "slug", "category", "category_display", "destination_city", "destination_country",
                  "summary", "description", "duration_days", "price", "deposit_amount", "departure_date",
                  "return_date", "capacity", "seats_left", "includes", "hotel_name", "hotel_stars",
                  "outbound_flight", "return_flight", "image_url", "is_featured"]


# --- booking input ---------------------------------------------------------
class BaggageItemSerializer(serializers.Serializer):
    option = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1, max_value=3, default=1)


class PassengerInputSerializer(serializers.Serializer):
    passenger_type = serializers.ChoiceField(choices=Passenger.Type.choices, default=Passenger.Type.ADULT)
    title = serializers.CharField(required=False, allow_blank=True, max_length=6)
    first_name = serializers.CharField(max_length=60)
    last_name = serializers.CharField(max_length=60)
    gender = serializers.ChoiceField(choices=Passenger.Gender.choices)
    date_of_birth = serializers.DateField()
    nationality = serializers.CharField(max_length=2, default="ET")
    passport_number = serializers.CharField(required=False, allow_blank=True, max_length=32)
    passport_expiry = serializers.DateField(required=False, allow_null=True)
    phone = serializers.CharField(required=False, allow_blank=True, max_length=16)
    seat = serializers.IntegerField(required=False, allow_null=True)
    baggage = BaggageItemSerializer(many=True, required=False)
    special_requests = serializers.CharField(required=False, allow_blank=True, max_length=255)


class BookingCreateSerializer(serializers.Serializer):
    fare_class = serializers.IntegerField(required=False)
    package = serializers.IntegerField(required=False)
    contact_phone = serializers.CharField(max_length=16)
    contact_email = serializers.EmailField(required=False, allow_blank=True)
    passengers = PassengerInputSerializer(many=True, min_length=1, max_length=9)

    def validate(self, attrs):
        if bool(attrs.get("fare_class")) == bool(attrs.get("package")):
            raise serializers.ValidationError("Provide exactly one of fare_class or package.")
        return attrs


class QuoteSerializer(serializers.Serializer):
    fare_class = serializers.IntegerField(required=False)
    package = serializers.IntegerField(required=False)
    passengers = serializers.ListField(child=serializers.DictField(), min_length=1, max_length=9)


# --- booking output --------------------------------------------------------
class PassengerBaggageSerializer(serializers.ModelSerializer):
    name = serializers.CharField(source="option.name", read_only=True)
    weight_kg = serializers.IntegerField(source="option.weight_kg", read_only=True)

    class Meta:
        model = PassengerBaggage
        fields = ["option", "name", "weight_kg", "quantity", "unit_price"]


class TicketSerializer(serializers.ModelSerializer):
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model = Ticket
        fields = ["ticket_number", "qr_payload", "status", "status_display", "checked_in_at", "boarded_at", "created_at"]


class PassengerSerializer(serializers.ModelSerializer):
    seat_number = serializers.CharField(source="seat.number", read_only=True, default=None)
    baggage = PassengerBaggageSerializer(many=True, read_only=True)
    ticket = TicketSerializer(read_only=True)

    class Meta:
        model = Passenger
        fields = ["id", "passenger_type", "title", "first_name", "last_name", "gender", "date_of_birth",
                  "nationality", "passport_number", "seat_number", "seat_price", "baggage", "ticket"]


class BookingListSerializer(serializers.ModelSerializer):
    flight = FlightListSerializer(read_only=True)
    package_name = serializers.CharField(source="package.name", read_only=True, default=None)
    package_slug = serializers.CharField(source="package.slug", read_only=True, default=None)
    passenger_count = serializers.IntegerField(source="passengers.count", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    departure_at = serializers.DateTimeField(read_only=True)
    destination_label = serializers.CharField(read_only=True)
    is_payable = serializers.BooleanField(read_only=True)

    class Meta:
        model = Booking
        fields = ["reference", "booking_type", "status", "status_display", "flight", "package_name", "package_slug",
                  "destination_label", "departure_at", "total_amount", "refunded_amount", "currency", "passenger_count",
                  "is_payable", "expires_at", "created_at"]


class BookingDetailSerializer(BookingListSerializer):
    fare_class = FareClassSerializer(read_only=True)
    package = TravelPackageSerializer(read_only=True)
    passengers = PassengerSerializer(many=True, read_only=True)
    funding_source_display = serializers.CharField(source="get_funding_source_display", read_only=True)
    cancel_policy = serializers.SerializerMethodField()

    class Meta(BookingListSerializer.Meta):
        fields = BookingListSerializer.Meta.fields + [
            "fare_class", "package", "passengers", "fare_amount", "seat_amount", "baggage_amount", "tax_amount",
            "discount_amount", "price_breakdown", "funding_source", "funding_source_display", "contact_phone",
            "contact_email", "confirmed_at", "cancelled_at", "cancellation_reason", "refunded_at", "refund_method",
            "refund_reference", "cancel_policy",
        ]

    def get_cancel_policy(self, obj) -> dict:
        from .services.bookings import cancellation_policy

        return cancellation_policy(obj)


class TicketVerifySerializer(serializers.Serializer):
    qr_payload = serializers.CharField()
    action = serializers.ChoiceField(choices=["check_in", "board"], required=False)
