"""Booking lifecycle: create (hold) → pay → confirm & ticket | expire | cancel."""
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify

from ..models import BaggageOption, Booking, BookingEvent, FareClass, Passenger, PassengerBaggage, Seat, TravelPackage
from . import pricing
from .seats import extend_hold
from .tickets import issue_tickets

log = logging.getLogger(__name__)


def _payment_window():
    return timedelta(minutes=settings.VACATION["BOOKING_PAYMENT_WINDOW_MINUTES"])


def log_event(booking: Booking, kind: str, message: str, by=None) -> BookingEvent:
    return BookingEvent.objects.create(booking=booking, kind=kind, message=message[:255], by=by)


# Online self-service cancellation closes this long before departure; after that the office handles changes.
SELF_CANCEL_CUTOFF = timedelta(hours=3)


def cancellation_policy(booking: Booking, now=None) -> dict:
    """
    What the customer may do with this booking, in plain words.

    * Unpaid holds can always be cancelled (nothing to refund).
    * Paid flight bookings on a refundable fare can be cancelled online until 3 hours before departure;
      the refund is the amount paid minus the fare's change fee per traveller (seat holders, not infants).
    * Non-refundable fares and packages are changed through the office.
    """
    now = now or timezone.now()
    out = {"can_cancel": False, "refundable": False, "refund_estimate": "0.00", "fee": "0.00", "message": "", "code": ""}

    def deny(code, message):
        out.update(code=code, message=message)
        return out

    if booking.status == Booking.Status.PENDING_PAYMENT:
        out.update(can_cancel=True)
        return deny("unpaid", "Nothing has been paid yet. Cancelling releases the held seats.")
    if booking.status != Booking.Status.CONFIRMED:
        return deny("closed", "This booking can no longer be changed.")
    departs = booking.departure_at
    if departs and departs <= now:
        return deny("started", "This trip has already started.")
    if booking.booking_type == Booking.Type.PACKAGE:
        return deny("package", "To change or cancel a package, please contact Vacation Equb – deposits follow the "
                               "package terms.")
    fare = booking.fare_class
    if not fare or not fare.refundable:
        return deny("non_refundable", "This fare is non-refundable. Contact Vacation Equb if you need to change it.")
    if departs and departs - now < SELF_CANCEL_CUTOFF:
        return deny("cutoff", "Online cancellation closes 3 hours before departure. Please contact Vacation Equb.")
    travellers = booking.passengers.exclude(passenger_type=Passenger.Type.INFANT).count()
    fee = min(fare.change_fee * travellers, booking.total_amount)
    out.update(can_cancel=True, refundable=True, fee=str(fee), refund_estimate=str(booking.total_amount - fee),
               code="refundable",
               message=f"Refundable fare: {booking.total_amount - fee:,.2f} ETB comes back to you"
                       + (f" after a {fee:,.2f} ETB fee." if fee else "."))
    return out


def _resolve_baggage(items, international: bool):
    resolved = []
    if not items:
        return resolved
    options = {o.id: o for o in BaggageOption.objects.filter(id__in=[i["option"] for i in items], is_active=True)}
    for item in items:
        opt = options.get(item["option"])
        if not opt:
            raise BusinessRuleError("Unknown baggage option.")
        if opt.international_only and not international:
            raise BusinessRuleError(f"{opt.name} is only available on international flights.")
        resolved.append((opt, int(item.get("quantity", 1))))
    return resolved


def _validate_passengers(passengers: list[dict]):
    if not passengers:
        raise BusinessRuleError("At least one passenger is required.")
    adults = sum(1 for p in passengers if p.get("passenger_type", "ADT") == "ADT")
    infants = sum(1 for p in passengers if p.get("passenger_type") == "INF")
    if adults == 0:
        raise BusinessRuleError("Each booking needs at least one adult.")
    if infants > adults:
        raise BusinessRuleError("Each infant must travel with an adult.")


@transaction.atomic
def create_flight_booking(*, user, fare_class_id: int, passengers: list[dict], contact_phone: str,
                          contact_email: str = "", funding_source=Booking.Funding.DIRECT) -> Booking:
    _validate_passengers(passengers)
    fare = FareClass.objects.select_for_update().select_related("flight__aircraft").get(pk=fare_class_id)
    flight = fare.flight
    if not flight.is_bookable:
        raise BusinessRuleError("This flight is no longer open for booking.")

    seated = [p for p in passengers if p.get("passenger_type") != Passenger.Type.INFANT]
    if fare.seats_left < len(seated):
        raise BusinessRuleError(f"Only {fare.seats_left} seat(s) left in {fare.name}.")

    # Seats must be currently held by this user (via /lock-seats) and match the cabin.
    seat_ids = [p["seat"] for p in passengers if p.get("seat")]
    if len(seat_ids) != len(set(seat_ids)):
        raise BusinessRuleError("Each passenger needs a different seat.")
    seats = {s.id: s for s in Seat.objects.select_for_update().filter(id__in=seat_ids, flight=flight).order_by("id")}
    now = timezone.now()
    for sid in seat_ids:
        seat = seats.get(sid)
        if not seat:
            raise BusinessRuleError("Selected seat is not on this flight.")
        if seat.cabin != fare.cabin:
            raise BusinessRuleError(f"Seat {seat.number} is not in the {fare.get_cabin_display()} cabin.")
        held_by_me = seat.status == Seat.Status.LOCKED and seat.locked_by_id == user.id and seat.locked_until > now
        if not held_by_me:
            raise BusinessRuleError(f"Your hold on seat {seat.number} has expired. Please reselect it.")
        if hasattr(seat, "passenger"):
            raise BusinessRuleError(f"Seat {seat.number} is already assigned.")

    pax_inputs = []
    for p in passengers:
        pax_inputs.append({
            "passenger_type": p.get("passenger_type", Passenger.Type.ADULT),
            "seat": seats.get(p.get("seat")),
            "baggage": _resolve_baggage(p.get("baggage", []), flight.is_international),
        })
    quote = pricing.quote_flight(fare, pax_inputs, now)

    booking = Booking.objects.create(
        user=user, booking_type=Booking.Type.FLIGHT, flight=flight, fare_class=fare,
        contact_phone=contact_phone, contact_email=contact_email, funding_source=funding_source,
        fare_amount=quote.fare, seat_amount=quote.seat, baggage_amount=quote.baggage, tax_amount=quote.taxes,
        discount_amount=quote.discount, total_amount=quote.total, price_breakdown=quote.as_dict(),
        expires_at=now + _payment_window(),
    )
    _create_passengers(booking, passengers, pax_inputs, quote)
    FareClass.objects.filter(pk=fare.pk).update(seats_sold=F("seats_sold") + len(seated))
    extend_hold(list(seats.values()), booking.expires_at)
    log_event(booking, BookingEvent.Kind.CREATED,
              f"{flight.flight_number} {fare.name} for {len(passengers)} traveller(s); seats held until "
              f"{timezone.localtime(booking.expires_at):%H:%M}.", by=user)
    return booking


@transaction.atomic
def create_package_booking(*, user, package_id: int, passengers: list[dict], contact_phone: str,
                           contact_email: str = "", funding_source=Booking.Funding.DIRECT) -> Booking:
    _validate_passengers(passengers)
    package = TravelPackage.objects.select_for_update().get(pk=package_id, is_active=True)
    travellers = [p for p in passengers if p.get("passenger_type") != Passenger.Type.INFANT]
    if package.seats_left < len(travellers):
        raise BusinessRuleError(f"Only {package.seats_left} place(s) left on this package.")
    if package.departure_date <= timezone.localdate():
        raise BusinessRuleError("This package has already departed.")
    pax_inputs = [{"passenger_type": p.get("passenger_type", "ADT"), "seat": None,
                   "baggage": _resolve_baggage(p.get("baggage", []), True)} for p in passengers]
    quote = pricing.quote_package(package, pax_inputs)
    booking = Booking.objects.create(
        user=user, booking_type=Booking.Type.PACKAGE, package=package, contact_phone=contact_phone,
        contact_email=contact_email, funding_source=funding_source, fare_amount=quote.fare,
        baggage_amount=quote.baggage, total_amount=quote.total, price_breakdown=quote.as_dict(),
        expires_at=timezone.now() + _payment_window(),
    )
    _create_passengers(booking, passengers, pax_inputs, quote)
    TravelPackage.objects.filter(pk=package.pk).update(seats_sold=F("seats_sold") + len(travellers))
    log_event(booking, BookingEvent.Kind.CREATED, f"{package.name} for {len(passengers)} traveller(s).", by=user)
    return booking


def _create_passengers(booking, raw, pax_inputs, quote):
    fields = ["title", "first_name", "last_name", "gender", "date_of_birth", "nationality",
              "passport_number", "passport_expiry", "phone", "special_requests"]
    for data, inp, pq in zip(raw, pax_inputs, quote.passengers):
        pax = Passenger.objects.create(
            booking=booking, passenger_type=inp["passenger_type"], seat=inp["seat"], seat_price=pq.seat,
            **{f: data[f] for f in fields if data.get(f) not in (None, "")},
        )
        PassengerBaggage.objects.bulk_create(
            [PassengerBaggage(passenger=pax, option=o, quantity=q, unit_price=o.price) for o, q in inp["baggage"]]
        )


def confirm_booking(booking: Booking, *, funding_reference: str = "") -> Booking:
    """Mark paid, convert seat holds to BOOKED and issue e-tickets (idempotent)."""
    with transaction.atomic():
        booking = Booking.objects.select_for_update().get(pk=booking.pk)
        if booking.status == Booking.Status.CONFIRMED:
            return booking
        if booking.status != Booking.Status.PENDING_PAYMENT:
            # Paid after expiry/cancel: keep money traceable, flag for refund/manual re-accommodation.
            log.error("Payment received for %s in status %s", booking.reference, booking.status)
            raise BusinessRuleError("Booking is no longer pending; payment flagged for refund.")
        Seat.objects.filter(passenger__booking=booking).update(status=Seat.Status.BOOKED, locked_until=None)
        booking.status = Booking.Status.CONFIRMED
        booking.confirmed_at = timezone.now()
        booking.expires_at = None
        if funding_reference:
            booking.funding_reference = funding_reference
        booking.save()
        issue_tickets(booking)
        log_event(booking, BookingEvent.Kind.CONFIRMED, "Paid in full – e-tickets issued.")
    notify(booking.user, Notification.Kind.BOOKING, "Booking confirmed ✈️",
           f"Your booking {booking.reference} is confirmed. Your e-ticket is ready.",
           {"booking": booking.reference})
    return booking


def _release_inventory(booking: Booking):
    seated = booking.passengers.exclude(passenger_type=Passenger.Type.INFANT).count()
    seat_ids = list(booking.passengers.exclude(seat__isnull=True).values_list("seat_id", flat=True))
    Passenger.objects.filter(booking=booking).update(seat=None)
    Seat.objects.filter(id__in=seat_ids).update(status=Seat.Status.AVAILABLE, locked_by=None, locked_until=None)
    if booking.fare_class_id:
        FareClass.objects.filter(pk=booking.fare_class_id).update(seats_sold=F("seats_sold") - seated)
    if booking.package_id:
        TravelPackage.objects.filter(pk=booking.package_id).update(seats_sold=F("seats_sold") - seated)


@transaction.atomic
def cancel_booking(booking: Booking, reason: str = "Cancelled by customer", *, staff=None,
                   notify_customer: bool = True) -> Booking:
    """
    Cancel and release the seats. Customers follow :func:`cancellation_policy`; staff (``staff=``) may cancel any
    unpaid or confirmed booking – the refund is then recorded separately with ``operations.refund_booking``.
    """
    booking = (Booking.objects.select_for_update(of=("self",)).select_related("fare_class", "flight", "package", "user")
               .get(pk=booking.pk))
    if booking.status not in (Booking.Status.PENDING_PAYMENT, Booking.Status.CONFIRMED):
        raise BusinessRuleError("This booking cannot be cancelled.")
    if staff is None:
        policy = cancellation_policy(booking)
        if not policy["can_cancel"]:
            raise BusinessRuleError(policy["message"])
    was_paid = booking.status == Booking.Status.CONFIRMED
    if was_paid:
        booking.tickets.update(status="VOID")
    _release_inventory(booking)
    booking.status = Booking.Status.CANCELLED
    booking.cancelled_at = timezone.now()
    booking.cancellation_reason = reason[:255]
    booking.cancelled_by = staff
    booking.save()
    log_event(booking, BookingEvent.Kind.CANCELLED, reason or "Cancelled", by=staff or booking.user)
    if notify_customer and (staff is not None or was_paid):
        body = f"Booking {booking.reference} ({booking.destination_label}) has been cancelled."
        if was_paid and booking.total_amount > 0:
            body += " Our team will process any refund due to you and confirm it in the app."
        transaction.on_commit(lambda: notify(booking.user, Notification.Kind.BOOKING, "Booking cancelled", body,
                                             {"booking": booking.reference}))
    return booking


def expire_unpaid_bookings() -> int:
    count = 0
    for booking in Booking.objects.filter(status=Booking.Status.PENDING_PAYMENT, expires_at__lt=timezone.now()):
        with transaction.atomic():
            b = Booking.objects.select_for_update().get(pk=booking.pk)
            if b.status != Booking.Status.PENDING_PAYMENT:
                continue
            _release_inventory(b)
            b.status = Booking.Status.EXPIRED
            b.save(update_fields=["status", "updated_at"])
            log_event(b, BookingEvent.Kind.EXPIRED, "Not paid in time – seats released.")
            count += 1
    return count


# --- payments integration --------------------------------------------------
def on_booking_paid(txn):
    booking = txn.target
    if booking.status == Booking.Status.PENDING_PAYMENT:
        confirm_booking(booking, funding_reference=str(txn.reference))


def on_booking_payment_failed(txn):
    booking = txn.target
    if isinstance(booking, Booking):
        log_event(booking, BookingEvent.Kind.PAYMENT,
                  f"{txn.get_provider_display()} payment of {txn.amount:,.2f} ETB failed"
                  + (f": {txn.failure_reason}" if txn.failure_reason else "."))
    notify(txn.user, Notification.Kind.PAYMENT, "Payment not completed",
           "Your payment didn't go through. Your seats are held until the booking expires — try again.",
           {"payment": str(txn.reference), **({"booking": booking.reference} if isinstance(booking, Booking) else {})})


def book_with_credit(booking: Booking, credit: Decimal, source: str, reference: str) -> Booking:
    """Apply an Equb payout / lucky-draw prize as credit; confirm if fully covered."""
    with transaction.atomic():
        b = Booking.objects.select_for_update().get(pk=booking.pk)
        applied = min(Decimal(credit), b.total_amount)
        b.discount_amount += applied
        b.total_amount -= applied
        b.funding_source = source
        b.funding_reference = reference
        b.price_breakdown = {**b.price_breakdown, "credit_applied": str(applied), "total": str(b.total_amount)}
        b.save()
    if b.total_amount == 0:
        return confirm_booking(b, funding_reference=reference)
    return b
