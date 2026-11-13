from django.db.models import DateTimeField, F, Prefetch, Q
from django.db.models.functions import Cast, Coalesce
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import BusinessRuleError
from apps.payments.models import PaymentTransaction
from apps.payments.serializers import PayRequestSerializer, PaymentTransactionSerializer
from apps.payments.services import initiate_payment

from .filters import FlightFilter, PackageFilter
from .models import Airport, BaggageOption, Booking, FareClass, Flight, Seat, Ticket, TravelPackage
from .serializers import (AirportSerializer, BaggageOptionSerializer, BookingCreateSerializer,
                          BookingDetailSerializer, BookingListSerializer, FlightDetailSerializer,
                          FlightListSerializer, QuoteSerializer, SeatLockSerializer, SeatReleaseSerializer,
                          TicketSerializer, TicketVerifySerializer, TravelPackageSerializer)
from .services import bookings as booking_service
from .services import operations, pricing
from .services.seats import build_seat_map, lock_seats, release_seats
from .services.tickets import qr_png, render_itinerary_pdf, verify_payload


class AirportViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Airport.objects.all()
    serializer_class = AirportSerializer
    permission_classes = [permissions.AllowAny]
    search_fields = ["iata", "city", "name", "country"]
    pagination_class = None


class FlightViewSet(viewsets.ReadOnlyModelViewSet):
    """Search flights: ``?origin=ADD&destination=DXB&date=2026-11-20&cabin=ECONOMY&passengers=2``"""

    permission_classes = [permissions.AllowAny]
    filterset_class = FlightFilter
    ordering_fields = ["departure_time", "arrival_time"]

    def get_queryset(self):
        return (Flight.objects.filter(departure_time__gt=timezone.now())
                .select_related("origin", "destination", "aircraft")
                .prefetch_related(Prefetch("fares", queryset=FareClass.objects.order_by("base_fare"))))

    def get_serializer_class(self):
        return FlightDetailSerializer if self.action != "list" else FlightListSerializer

    @action(detail=True, methods=["get"], url_path="seat-map")
    def seat_map(self, request, pk=None):
        return Response(build_seat_map(self.get_object(), request.user))

    @extend_schema(request=SeatLockSerializer)
    @action(detail=True, methods=["post"], url_path="lock-seats", permission_classes=[permissions.IsAuthenticated])
    def lock_seats(self, request, pk=None):
        s = SeatLockSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        seats = lock_seats(request.user, self.get_object(), s.validated_data["seat_ids"])
        return Response({
            "locked": [{"id": x.id, "number": x.number, "cabin": x.cabin, "surcharge": str(x.surcharge)} for x in seats],
            "locked_until": seats[0].locked_until,
        })

    @extend_schema(request=SeatReleaseSerializer)
    @action(detail=True, methods=["post"], url_path="release-seats", permission_classes=[permissions.IsAuthenticated])
    def release_seats(self, request, pk=None):
        s = SeatReleaseSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        n = release_seats(request.user, self.get_object(), s.validated_data.get("seat_ids"))
        return Response({"released": n})


class BaggageOptionViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = BaggageOption.objects.filter(is_active=True)
    serializer_class = BaggageOptionSerializer
    permission_classes = [permissions.AllowAny]
    pagination_class = None


class TravelPackageViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = TravelPackage.objects.filter(is_active=True)
    serializer_class = TravelPackageSerializer
    permission_classes = [permissions.AllowAny]
    filterset_class = PackageFilter
    search_fields = ["name", "destination_city", "summary"]
    lookup_field = "slug"


def trip_tabs():
    """Q objects splitting a customer's bookings into the three 'My trips' tabs."""
    now = timezone.now()
    pending = Q(status=Booking.Status.PENDING_PAYMENT) & (Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    upcoming = Q(status=Booking.Status.CONFIRMED) & (Q(flight__departure_time__gt=now)
                                                     | Q(package__departure_date__gte=timezone.localdate()))
    return {"pending": pending, "upcoming": upcoming}


class BookingViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                     viewsets.GenericViewSet):
    """My bookings. ``?tab=upcoming|pending|past`` gives the 'My trips' tabs (upcoming sorted by departure)."""

    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "reference"
    filterset_fields = ["status", "booking_type"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Booking.objects.none()
        qs = Booking.objects.select_related("flight__origin", "flight__destination", "flight__aircraft",
                                            "fare_class", "package").prefetch_related(
            "passengers__seat", "passengers__baggage__option", "passengers__ticket")
        if not (self.request.user.is_staff and self.request.query_params.get("all")):
            qs = qs.filter(user=self.request.user)
        tab = self.request.query_params.get("tab") if self.action == "list" else None
        if tab in ("pending", "upcoming", "past"):
            q = trip_tabs()
            if tab == "pending":
                qs = qs.filter(q["pending"]).order_by("expires_at")
            elif tab == "upcoming":
                qs = qs.filter(q["upcoming"]).annotate(
                    dep=Coalesce(F("flight__departure_time"), Cast("package__departure_date", DateTimeField()))
                ).order_by("dep")
            else:
                qs = qs.exclude(q["pending"]).exclude(q["upcoming"]).order_by("-created_at")
        return qs

    @action(detail=False, methods=["get"])
    def counts(self, request):
        """How many bookings sit in each 'My trips' tab."""
        qs = Booking.objects.filter(user=request.user)
        q = trip_tabs()
        pending, upcoming = qs.filter(q["pending"]).count(), qs.filter(q["upcoming"]).count()
        return Response({"pending": pending, "upcoming": upcoming, "past": qs.count() - pending - upcoming})

    def get_serializer_class(self):
        if self.action == "create":
            return BookingCreateSerializer
        return BookingListSerializer if self.action == "list" else BookingDetailSerializer

    def create(self, request, *args, **kwargs):
        s = BookingCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        common = dict(user=request.user, passengers=d["passengers"], contact_phone=d["contact_phone"],
                      contact_email=d.get("contact_email", ""))
        if d.get("fare_class"):
            booking = booking_service.create_flight_booking(fare_class_id=d["fare_class"], **common)
        else:
            booking = booking_service.create_package_booking(package_id=d["package"], **common)
        return Response(BookingDetailSerializer(self.get_queryset().get(pk=booking.pk)).data,
                        status=status.HTTP_201_CREATED)

    @extend_schema(request=QuoteSerializer)
    @action(detail=False, methods=["post"])
    def quote(self, request):
        """Price preview before creating a booking (no inventory is held)."""
        s = QuoteSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        seat_ids = [p.get("seat") for p in d["passengers"] if p.get("seat")]
        seats = {x.id: x for x in Seat.objects.filter(id__in=seat_ids)}
        bag_ids = {b["option"] for p in d["passengers"] for b in p.get("baggage", [])}
        bags = {b.id: b for b in BaggageOption.objects.filter(id__in=bag_ids)}
        pax = [{"passenger_type": p.get("passenger_type", "ADT"), "seat": seats.get(p.get("seat")),
                "baggage": [(bags[b["option"]], int(b.get("quantity", 1))) for b in p.get("baggage", []) if b["option"] in bags]}
               for p in d["passengers"]]
        if d.get("fare_class"):
            quote = pricing.quote_flight(FareClass.objects.select_related("flight").get(pk=d["fare_class"]), pax)
        elif d.get("package"):
            quote = pricing.quote_package(TravelPackage.objects.get(pk=d["package"]), pax)
        else:
            raise BusinessRuleError("Provide fare_class or package.")
        return Response(quote.as_dict())

    @extend_schema(request=PayRequestSerializer, responses=PaymentTransactionSerializer)
    @action(detail=True, methods=["post"])
    def pay(self, request, reference=None):
        booking = self.get_object()
        if not booking.is_payable:
            raise BusinessRuleError("This booking is not awaiting payment (it may have expired).")
        s = PayRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        txn = initiate_payment(user=request.user, target=booking, amount=booking.amount_due,
                               purpose=PaymentTransaction.Purpose.BOOKING, provider=s.validated_data.get("provider"),
                               payer_phone=s.validated_data.get("payer_phone", ""))
        return Response(PaymentTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, reference=None):
        """Cancel within the booking's ``cancel_policy`` (unpaid holds always; refundable fares until 3 h before)."""
        reason = str(request.data.get("reason") or "Cancelled by customer")[:200]
        booking_service.cancel_booking(self.get_object(), reason)
        return Response(BookingDetailSerializer(self.get_queryset().get(reference=reference)).data)

    @action(detail=True, methods=["get"])
    def tickets(self, request, reference=None):
        booking = self.get_object()
        return Response(TicketSerializer(booking.tickets.all(), many=True).data)

    @action(detail=True, methods=["get"])
    def itinerary(self, request, reference=None):
        booking = self.get_object()
        if booking.status != Booking.Status.CONFIRMED:
            raise BusinessRuleError("The itinerary is available once the booking is confirmed.")
        resp = HttpResponse(render_itinerary_pdf(booking), content_type="application/pdf")
        resp["Content-Disposition"] = f'attachment; filename="EthioVacation-{booking.reference}.pdf"'
        return resp

    @action(detail=True, methods=["get"], url_path=r"qr/(?P<ticket_number>\d+)")
    def qr(self, request, reference=None, ticket_number=None):
        ticket = self.get_object().tickets.filter(ticket_number=ticket_number).first()
        if not ticket:
            raise NotFound("Ticket not found")
        return HttpResponse(qr_png(ticket.qr_payload), content_type="image/png")


class TicketVerifyView(APIView):
    """
    Gate / check-in agents scan a QR code and validate its signature.
    Send ``action: "check_in"`` or ``"board"`` to record that step in the same call.
    """

    permission_classes = [permissions.IsAdminUser]

    @extend_schema(request=TicketVerifySerializer)
    def post(self, request):
        s = TicketVerifySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        data = verify_payload(s.validated_data["qr_payload"])
        if not data:
            return Response({"valid": False, "reason": "Signature mismatch"}, status=400)
        ticket = (Ticket.objects.select_related("booking__flight", "passenger__seat")
                  .filter(ticket_number=data["t"]).first())
        valid = bool(ticket and ticket.status != Ticket.Status.VOID and ticket.booking.status == Booking.Status.CONFIRMED)
        message = ""
        act = s.validated_data.get("action")
        if valid and act:
            try:
                ticket = (operations.check_in if act == "check_in" else operations.board)(ticket, request.user)
                message = f"{ticket.passenger.full_name}: {ticket.get_status_display().lower()}."
            except BusinessRuleError as exc:
                message = str(exc.detail)
        return Response({
            "valid": valid, "data": data, "status": ticket.status if ticket else None, "message": message,
            "passenger": ticket.passenger.full_name if ticket else None,
            "seat": ticket.passenger.seat.number if ticket and ticket.passenger.seat_id else None,
            "booking": ticket.booking.reference if ticket else None,
        })
