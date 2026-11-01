"""
Office operations on bookings: payments taken at the desk, refunds, check-in & boarding, notes and reminders.

Everything here writes a :class:`BookingEvent`, so the booking page shows who did what and when.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.payments.models import PaymentTransaction

from ..models import Booking, BookingEvent, Flight, Ticket
from .bookings import cancel_booking, confirm_booking, log_event
from .tickets import verify_payload

CENT = Decimal("0.01")
OFFICE_METHODS = {
    PaymentTransaction.Provider.CASH: "Cash",
    PaymentTransaction.Provider.BANK: "Bank transfer",
    PaymentTransaction.Provider.MOBILE: "Mobile money",
}
REFUND_METHODS = ["Cash", "Bank transfer", "telebirr", "Chapa", "Booking credit"]
# Staff can still check passengers in / board them a while after the scheduled time (delays).
LATE_DESK_WINDOW = timedelta(hours=6)


def _money(v) -> str:
    return f"{Decimal(v):,.2f}"


def booking_payments(booking: Booking):
    ct = ContentType.objects.get_for_model(Booking)
    return PaymentTransaction.objects.filter(content_type=ct, object_id=booking.pk).order_by("-created_at")


def amount_paid(booking: Booking) -> Decimal:
    """Money actually received for this booking (gateway + office), before refunds."""
    return (booking_payments(booking).filter(status__in=[PaymentTransaction.Status.SUCCESS,
                                                         PaymentTransaction.Status.REFUNDED])
            .aggregate(s=Sum("amount"))["s"] or Decimal("0"))


def refundable_amount(booking: Booking) -> Decimal:
    return max(amount_paid(booking) - booking.refunded_amount, Decimal("0"))


# --------------------------------------------------------------------------- payments at the office
@transaction.atomic
def record_office_payment(booking: Booking, staff, *, method: str, reference: str = "", note: str = "") -> Booking:
    """The customer pays the full amount at the office (cash, bank transfer, manual mobile money)."""
    if method not in OFFICE_METHODS:
        raise BusinessRuleError("Choose cash, bank transfer or mobile money.")
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking.status != Booking.Status.PENDING_PAYMENT:
        raise BusinessRuleError("Only a booking that is awaiting payment can be paid.")
    if not booking.is_payable:
        raise BusinessRuleError("The seat hold has expired. Make a new booking for the customer.")
    reference = reference.strip()
    if method != PaymentTransaction.Provider.CASH and not reference:
        raise BusinessRuleError("Enter the transfer reference so the payment can be traced.")
    txn = PaymentTransaction.objects.create(
        user=booking.user, provider=method, purpose=PaymentTransaction.Purpose.BOOKING, amount=booking.total_amount,
        status=PaymentTransaction.Status.SUCCESS, target=booking, provider_reference=reference[:128],
        payer_phone=booking.contact_phone, completed_at=timezone.now(), fulfilled=True, recorded_by=staff,
        raw_request={"recorded_by": getattr(staff, "full_name", ""), "note": note[:255]},
    )
    log_event(booking, BookingEvent.Kind.PAYMENT,
              f"{OFFICE_METHODS[method]} payment of {_money(txn.amount)} ETB recorded at the office"
              + (f" (ref {reference})" if reference else "") + (f" – {note.strip()}" if note.strip() else "") + ".",
              by=staff)
    return confirm_booking(booking, funding_reference=str(txn.reference))


# --------------------------------------------------------------------------- cancellations & refunds
def staff_cancel(booking: Booking, staff, *, reason: str, notify_customer: bool = True) -> Booking:
    reason = (reason or "").strip() or "Cancelled by Rihla Travel"
    return cancel_booking(booking, reason, staff=staff, notify_customer=notify_customer)


@transaction.atomic
def refund_booking(booking: Booking, staff, *, amount, method: str, reference: str = "", note: str = "",
                   notify_customer: bool = True) -> Booking:
    """
    Record money going back to the customer. A confirmed booking is cancelled first (seats released, tickets
    voided). Partial refunds are allowed (e.g. a fee was kept); the booking becomes REFUNDED.
    """
    booking = Booking.objects.select_for_update().get(pk=booking.pk)
    if booking.status == Booking.Status.CONFIRMED:
        booking = cancel_booking(booking, f"Cancelled for refund – {note.strip() or method}", staff=staff,
                                 notify_customer=False)
    if booking.status not in (Booking.Status.CANCELLED, Booking.Status.EXPIRED, Booking.Status.REFUNDED):
        raise BusinessRuleError("Only a cancelled, expired or confirmed booking can be refunded.")
    try:
        amount = Decimal(str(amount)).quantize(CENT)
    except Exception:  # noqa: BLE001 – any parse error is a user input problem
        raise BusinessRuleError("Enter the amount refunded.") from None
    available = refundable_amount(booking)
    if amount <= 0:
        raise BusinessRuleError("Enter the amount refunded.")
    if amount > available:
        raise BusinessRuleError(f"At most {_money(available)} ETB was paid and not yet refunded.")
    if method not in REFUND_METHODS:
        raise BusinessRuleError("Choose how the money was returned.")
    if method not in ("Cash", "Booking credit") and not reference.strip():
        raise BusinessRuleError("Enter the transfer reference so the refund can be traced.")

    # Spread the refund over the successful payments, newest first.
    left = amount
    for txn in booking_payments(booking).filter(status=PaymentTransaction.Status.SUCCESS).select_for_update():
        if left <= 0:
            break
        take = min(left, txn.amount - txn.refunded_amount)
        if take <= 0:
            continue
        txn.refunded_amount += take
        if txn.refunded_amount >= txn.amount:
            txn.status = PaymentTransaction.Status.REFUNDED
        txn.save(update_fields=["refunded_amount", "status", "updated_at"])
        left -= take

    booking.refunded_amount += amount
    booking.refunded_at = timezone.now()
    booking.refund_method = method[:20]
    booking.refund_reference = reference.strip()[:64]
    booking.status = Booking.Status.REFUNDED
    booking.save()
    log_event(booking, BookingEvent.Kind.REFUNDED,
              f"{_money(amount)} ETB refunded by {method}" + (f" (ref {reference.strip()})" if reference.strip() else "")
              + (f" – {note.strip()}" if note.strip() else "") + ".", by=staff)
    if notify_customer:
        b = booking
        transaction.on_commit(lambda: notify(
            b.user, Notification.Kind.PAYMENT, "Refund sent 💸",
            f"{_money(amount)} ETB for booking {b.reference} was refunded by {method}"
            + (f" (ref {b.refund_reference})." if b.refund_reference else "."), {"booking": b.reference}))
    return booking


# --------------------------------------------------------------------------- check-in & boarding
def _desk_checks(ticket: Ticket):
    b = ticket.booking
    if b.status != Booking.Status.CONFIRMED:
        raise BusinessRuleError(f"Booking {b.reference} is {b.get_status_display().lower()} – not valid for travel.")
    if ticket.status == Ticket.Status.VOID:
        raise BusinessRuleError("This ticket has been voided.")
    if b.flight_id:
        if b.flight.status == Flight.Status.CANCELLED:
            raise BusinessRuleError("This flight is cancelled.")
        if timezone.now() > b.flight.departure_time + LATE_DESK_WINDOW:
            raise BusinessRuleError("This flight has already left.")


@transaction.atomic
def check_in(ticket: Ticket, staff) -> Ticket:
    ticket = (Ticket.objects.select_for_update(of=("self",)).select_related("booking__flight", "passenger")
              .get(pk=ticket.pk))
    _desk_checks(ticket)
    if ticket.status in (Ticket.Status.CHECKED_IN, Ticket.Status.BOARDED):
        raise BusinessRuleError(f"{ticket.passenger.full_name} is already {ticket.get_status_display().lower()}.")
    ticket.status = Ticket.Status.CHECKED_IN
    ticket.checked_in_at = timezone.now()
    ticket.checked_in_by = staff
    ticket.save(update_fields=["status", "checked_in_at", "checked_in_by", "updated_at"])
    log_event(ticket.booking, BookingEvent.Kind.CHECKED_IN, f"{ticket.passenger.full_name} checked in.", by=staff)
    return ticket


@transaction.atomic
def board(ticket: Ticket, staff) -> Ticket:
    ticket = (Ticket.objects.select_for_update(of=("self",)).select_related("booking__flight", "passenger")
              .get(pk=ticket.pk))
    _desk_checks(ticket)
    if ticket.status == Ticket.Status.BOARDED:
        raise BusinessRuleError(f"{ticket.passenger.full_name} has already boarded.")
    if ticket.status != Ticket.Status.CHECKED_IN:
        raise BusinessRuleError(f"Check {ticket.passenger.full_name} in before boarding.")
    ticket.status = Ticket.Status.BOARDED
    ticket.boarded_at = timezone.now()
    ticket.boarded_by = staff
    ticket.save(update_fields=["status", "boarded_at", "boarded_by", "updated_at"])
    log_event(ticket.booking, BookingEvent.Kind.BOARDED, f"{ticket.passenger.full_name} boarded.", by=staff)
    return ticket


@transaction.atomic
def undo_desk(ticket: Ticket, staff) -> Ticket:
    """Step back one desk stage (boarded → checked in → issued) after a mistake."""
    ticket = Ticket.objects.select_for_update(of=("self",)).select_related("booking", "passenger").get(pk=ticket.pk)
    if ticket.status == Ticket.Status.BOARDED:
        ticket.status, ticket.boarded_at, ticket.boarded_by = Ticket.Status.CHECKED_IN, None, None
        what = "boarding"
    elif ticket.status == Ticket.Status.CHECKED_IN:
        ticket.status, ticket.checked_in_at, ticket.checked_in_by = Ticket.Status.ISSUED, None, None
        what = "check-in"
    else:
        raise BusinessRuleError("Nothing to undo for this passenger.")
    ticket.save()
    log_event(ticket.booking, BookingEvent.Kind.NOTE, f"Undid {what} for {ticket.passenger.full_name}.", by=staff)
    return ticket


def find_for_desk(query: str) -> tuple[Booking | None, Ticket | None, str]:
    """
    Look up what a desk agent typed or scanned: a booking reference, a 13-digit ticket number or a signed QR payload.
    Returns ``(booking, ticket, message)``.
    """
    q = (query or "").strip()
    if not q:
        return None, None, ""
    if len(q) > 20:  # QR payloads are long signed tokens
        data = verify_payload(q)
        if not data:
            return None, None, "This QR code is not a valid Rihla ticket (signature check failed)."
        ticket = Ticket.objects.select_related("booking").filter(ticket_number=data.get("t")).first()
        if not ticket:
            return None, None, "The QR code is genuine but the ticket no longer exists."
        return ticket.booking, ticket, ""
    if q.isdigit():
        ticket = Ticket.objects.select_related("booking").filter(ticket_number=q).first()
        return (ticket.booking, ticket, "") if ticket else (None, None, f"No ticket {q}.")
    booking = Booking.objects.filter(reference__iexact=q).first()
    return (booking, None, "") if booking else (None, None, f"No booking with reference {q.upper()}.")


# --------------------------------------------------------------------------- customer messages
def resend_confirmation(booking: Booking, staff) -> None:
    if booking.status != Booking.Status.CONFIRMED:
        raise BusinessRuleError("Only confirmed bookings have a confirmation to resend.")
    notify(booking.user, Notification.Kind.BOOKING, "Your booking details ✈️",
           f"Booking {booking.reference}: {booking.destination_label}. Your e-tickets and PDF itinerary are in the app.",
           {"booking": booking.reference})
    log_event(booking, BookingEvent.Kind.MESSAGE, "Confirmation sent again to the customer's app.", by=staff)


def add_note(booking: Booking, staff, text: str) -> BookingEvent:
    text = (text or "").strip()
    if not text:
        raise BusinessRuleError("Write a note first.")
    return log_event(booking, BookingEvent.Kind.NOTE, text, by=staff)


def send_trip_reminders(now=None) -> int:
    """One reminder per confirmed booking, about a day before departure."""
    now = now or timezone.now()
    sent = 0
    flights = (Booking.objects.filter(status=Booking.Status.CONFIRMED, trip_reminder_sent_at__isnull=True,
                                      flight__departure_time__gt=now,
                                      flight__departure_time__lte=now + timedelta(hours=24))
               .select_related("flight__origin", "flight__destination", "user"))
    for b in flights:
        f = b.flight
        dep = timezone.localtime(f.departure_time)
        extra = " ".join(x for x in [f"Terminal {f.terminal}." if f.terminal else "", f"Gate {f.gate}." if f.gate else ""] if x)
        notify(b.user, Notification.Kind.BOOKING, f"Your flight to {f.destination.city} is tomorrow ✈️"
               if dep.date() != timezone.localdate(now) else f"Your flight to {f.destination.city} is today ✈️",
               f"{f.flight_number} departs {f.origin.city} at {dep:%H:%M} on {dep:%a %d %b}. Check-in closes 60 minutes "
               f"before departure – bring your passport and e-ticket. {extra}".strip(), {"booking": b.reference})
        Booking.objects.filter(pk=b.pk).update(trip_reminder_sent_at=now)
        log_event(b, BookingEvent.Kind.MESSAGE, "Departure reminder sent.")
        sent += 1
    tomorrow = timezone.localdate(now) + timedelta(days=1)
    packages = (Booking.objects.filter(status=Booking.Status.CONFIRMED, trip_reminder_sent_at__isnull=True,
                                       package__departure_date__gt=timezone.localdate(now),
                                       package__departure_date__lte=tomorrow)
                .select_related("package", "user"))
    for b in packages:
        notify(b.user, Notification.Kind.BOOKING, f"{b.package.name} starts tomorrow",
               "Please bring your passport and booking reference. Our team will contact you with meeting details.",
               {"booking": b.reference})
        Booking.objects.filter(pk=b.pk).update(trip_reminder_sent_at=now)
        log_event(b, BookingEvent.Kind.MESSAGE, "Departure reminder sent.")
        sent += 1
    return sent


# --------------------------------------------------------------------------- lists for the console
def paid_after_expiry():
    """Bookings that expired or were cancelled while unpaid, but where a payment later succeeded – refund these."""
    ct = ContentType.objects.get_for_model(Booking)
    paid_ids = PaymentTransaction.objects.filter(content_type=ct, status=PaymentTransaction.Status.SUCCESS).values("object_id")
    return Booking.objects.filter(pk__in=paid_ids, status__in=[Booking.Status.EXPIRED, Booking.Status.CANCELLED],
                                  confirmed_at__isnull=True)


def refunds_due():
    return Booking.objects.filter(status=Booking.Status.CANCELLED, confirmed_at__isnull=False, total_amount__gt=0,
                                  refunded_at__isnull=True)


def desk_departures(hours: int = 24):
    """Flights leaving in the next ``hours`` with check-in progress."""
    now = timezone.now()
    return (Flight.objects.filter(departure_time__gt=now - LATE_DESK_WINDOW, departure_time__lte=now + timedelta(hours=hours))
            .exclude(status=Flight.Status.CANCELLED)
            .select_related("origin", "destination")
            .annotate(tickets=Count("bookings__tickets", filter=Q(bookings__tickets__status__in=[
                          Ticket.Status.ISSUED, Ticket.Status.CHECKED_IN, Ticket.Status.BOARDED]), distinct=True),
                      checked=Count("bookings__tickets", filter=Q(bookings__tickets__status__in=[
                          Ticket.Status.CHECKED_IN, Ticket.Status.BOARDED]), distinct=True),
                      boarded=Count("bookings__tickets", filter=Q(bookings__tickets__status=Ticket.Status.BOARDED),
                                    distinct=True))
            .order_by("departure_time"))
