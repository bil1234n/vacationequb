"""
Staff console – travel operations: bookings (work queues, filters, CSV), the booking page (office payments,
cancellations, refunds, notes, messages, check-in), the check-in & boarding desk and flight manifests.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlencode

from django.contrib import messages
from django.db.models import Count, F, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.travel.models import Booking, BookingEvent, Flight, Passenger, Ticket
from apps.travel.services import operations as ops
from apps.travel.services.bookings import cancellation_policy

from .equb_views import _csv, _date, _decimal, _do, _next
from .views import page, pct, staff_required

QUEUES = [
    ("all", "All bookings"),
    ("pending", "Awaiting payment"),
    ("departing", "Departing in 48 h"),
    ("refund_due", "Refunds due"),
    ("paid_after_expiry", "Paid after expiry"),
]
EVENT_TONE = {"CONFIRMED": "green", "BOARDED": "green", "CHECKED_IN": "gold", "PAYMENT": "gold", "CREATED": "grey",
              "CANCELLED": "red", "EXPIRED": "red", "REFUNDED": "red", "NOTE": "grey", "MESSAGE": "grey"}


def _queue(qs, key: str):
    now = timezone.now()
    if key == "pending":
        return qs.filter(status=Booking.Status.PENDING_PAYMENT, expires_at__gt=now).order_by("expires_at")
    if key == "departing":
        return (qs.filter(status=Booking.Status.CONFIRMED)
                .filter(Q(flight__departure_time__gt=now, flight__departure_time__lte=now + timedelta(hours=48))
                        | Q(package__departure_date__gte=timezone.localdate(),
                            package__departure_date__lte=timezone.localdate() + timedelta(days=2)))
                .order_by(F("flight__departure_time").asc(nulls_last=True), "package__departure_date"))
    if key == "refund_due":
        return qs.filter(pk__in=ops.refunds_due().values("pk")).order_by("-cancelled_at")
    if key == "paid_after_expiry":
        return qs.filter(pk__in=ops.paid_after_expiry().values("pk")).order_by("-updated_at")
    return qs.order_by("-created_at")


# --------------------------------------------------------------------------- bookings list
@staff_required
def bookings(request):
    base = (Booking.objects.select_related("user", "flight__origin", "flight__destination", "package", "fare_class")
            .annotate(pax=Count("passengers", distinct=True)))
    q, status, btype = request.GET.get("q", "").strip(), request.GET.get("status", ""), request.GET.get("type", "")
    start, end = _date(request.GET.get("from", "")), _date(request.GET.get("to", ""))
    queue = request.GET.get("queue", "all")
    if queue not in dict(QUEUES):
        queue = "all"
    qs = base
    if q:
        qs = qs.filter(Q(reference__icontains=q) | Q(user__phone__icontains=q) | Q(user__full_name__icontains=q)
                       | Q(contact_phone__icontains=q) | Q(passengers__last_name__icontains=q)
                       | Q(passengers__first_name__icontains=q) | Q(passengers__passport_number__icontains=q)
                       | Q(tickets__ticket_number=q)).distinct()
    if status:
        qs = qs.filter(status=status)
    if btype:
        qs = qs.filter(booking_type=btype)
    if start:
        qs = qs.filter(created_at__date__gte=start)
    if end:
        qs = qs.filter(created_at__date__lte=end)
    qs = _queue(qs, queue)

    if request.GET.get("export") == "csv":
        rows = ([b.reference, timezone.localtime(b.created_at).strftime("%Y-%m-%d %H:%M"), b.get_status_display(),
                 b.get_booking_type_display(), b.destination_label,
                 timezone.localtime(b.departure_at).strftime("%Y-%m-%d %H:%M") if b.departure_at else "",
                 b.flight.flight_number if b.flight_id else "", b.fare_class.name if b.fare_class_id else "",
                 b.pax, b.user.full_name, b.user.phone, b.contact_phone, b.get_funding_source_display(),
                 f"{b.total_amount:.2f}", f"{b.refunded_amount:.2f}"] for b in qs.iterator(chunk_size=500))
        return _csv(f"vacation-bookings-{timezone.localdate()}.csv",
                    ["Reference", "Created", "Status", "Type", "Trip", "Departure", "Flight", "Fare", "Travellers",
                     "Customer", "Customer phone", "Contact phone", "Funding", "Total (ETB)", "Refunded (ETB)"], rows)

    totals = qs.aggregate(n=Count("id", distinct=True), value=Sum("total_amount"))
    counts = {
        "pending": Booking.objects.filter(status=Booking.Status.PENDING_PAYMENT, expires_at__gt=timezone.now()).count(),
        "departing": _queue(Booking.objects.all(), "departing").count(),
        "refund_due": ops.refunds_due().count(),
        "paid_after_expiry": ops.paid_after_expiry().count(),
    }
    return render(request, "console/bookings.html", {
        "nav": "bookings", "page": page(request, qs), "q": q, "status": status, "type": btype, "totals": totals,
        "statuses": Booking.Status.choices, "queue": queue, "queues": QUEUES, "counts": counts,
        "from": request.GET.get("from", ""), "to": request.GET.get("to", ""), "now": timezone.now(),
    })


# --------------------------------------------------------------------------- booking page
def _timeline(b: Booking) -> list:
    """Recorded events, plus the milestones of bookings made before the audit trail existed."""
    events = list(b.events.select_related("by")[:60])
    kinds = {e.kind for e in events}
    milestones = [("CREATED", b.created_at, f"Booked in the app by {b.user.full_name}."),
                  ("CONFIRMED", b.confirmed_at, "Paid – e-tickets issued."),
                  ("CANCELLED", b.cancelled_at, b.cancellation_reason or "Cancelled."),
                  ("REFUNDED", b.refunded_at, f"{b.refunded_amount:,.2f} ETB refunded by {b.refund_method}.")]
    for kind, when, message in milestones:
        if when and kind not in kinds:
            events.append(SimpleNamespace(kind=kind, get_kind_display=BookingEvent.Kind(kind).label, message=message,
                                          by=None, created_at=when))
    events.sort(key=lambda e: e.created_at, reverse=True)
    for e in events:
        e.tone = EVENT_TONE.get(e.kind, "grey")
    return events


@staff_required
def booking_detail(request, reference):
    b = get_object_or_404(Booking.objects.select_related("user", "flight__origin", "flight__destination",
                                                         "flight__aircraft", "fare_class", "package", "cancelled_by"),
                          reference=reference)
    here = reverse("console:booking", args=[b.reference])
    if request.method == "POST":
        action, staff, p = request.POST.get("action", ""), request.user, request.POST
        notify_customer = p.get("notify") == "1" or "notify" not in p
        if action == "record_payment":
            _do(request, lambda: ops.record_office_payment(b, staff, method=p.get("method", ""),
                                                           reference=p.get("reference", ""), note=p.get("note", "")),
                f"Payment recorded – booking {b.reference} is confirmed and tickets are issued.")
        elif action == "cancel":
            _do(request, lambda: ops.staff_cancel(b, staff, reason=p.get("reason", "") or
                                                  f"Cancelled by staff ({staff.full_name})",
                                                  notify_customer=notify_customer),
                f"Booking {b.reference} cancelled and inventory released.")
        elif action == "refund":
            amount = _decimal(p.get("amount", ""))
            if amount is None:
                messages.error(request, "Enter the amount refunded.")
            else:
                _do(request, lambda: ops.refund_booking(b, staff, amount=amount, method=p.get("method", ""),
                                                        reference=p.get("reference", ""), note=p.get("note", ""),
                                                        notify_customer=notify_customer),
                    f"Refund of {amount:,.2f} ETB recorded.")
        elif action == "note":
            _do(request, lambda: ops.add_note(b, staff, p.get("note", "")), "Note added.")
        elif action == "resend":
            _do(request, lambda: ops.resend_confirmation(b, staff), "Confirmation sent to the customer's app.")
        elif action in ("check_in", "board", "undo"):
            ticket = Ticket.objects.filter(booking=b, pk=p.get("ticket")).first()
            if ticket:
                fn = {"check_in": ops.check_in, "board": ops.board, "undo": ops.undo_desk}[action]
                _do(request, lambda: fn(ticket, staff),
                    {"check_in": "Checked in.", "board": "Boarded.", "undo": "Step undone."}[action])
        return redirect(_next(request, here))

    passengers = list(b.passengers.select_related("seat", "ticket").prefetch_related("baggage__option"))
    payments = ops.booking_payments(b).select_related("recorded_by")
    paid = ops.amount_paid(b)
    policy = cancellation_policy(b)
    refund_default = b.total_amount - Decimal(policy["fee"]) if policy["refundable"] else paid
    events = _timeline(b)
    tickets = [p.ticket for p in passengers if hasattr(p, "ticket")]
    return render(request, "console/booking_detail.html", {
        "nav": "bookings", "b": b, "payments": payments, "passengers": passengers, "events": events,
        "paid": paid, "refundable": ops.refundable_amount(b), "refund_default": max(min(refund_default, paid), 0),
        "policy": policy, "office_methods": list(ops.OFFICE_METHODS.items()), "refund_methods": ops.REFUND_METHODS,
        "checked_in": sum(1 for t in tickets if t.status in ("CHECKED_IN", "BOARDED")), "tickets": len(tickets),
        "paid_late": b.status in ("EXPIRED", "CANCELLED") and not b.confirmed_at and paid > 0,
    })


# --------------------------------------------------------------------------- check-in & boarding desk
@staff_required
def check_in_desk(request):
    query = (request.POST.get("q") or request.GET.get("q") or "").strip()
    if request.method == "POST" and request.POST.get("action") in ("check_in", "board", "undo", "check_in_all"):
        action, staff = request.POST["action"], request.user
        if action == "check_in_all":
            booking = get_object_or_404(Booking, pk=request.POST.get("booking"))
            done = 0
            for t in booking.tickets.select_related("passenger").filter(status=Ticket.Status.ISSUED):
                if _do(request, lambda t=t: ops.check_in(t, staff)):
                    done += 1
            if done:
                messages.success(request, f"{done} passenger(s) checked in.")
        else:
            ticket = get_object_or_404(Ticket, pk=request.POST.get("ticket"))
            fn = {"check_in": ops.check_in, "board": ops.board, "undo": ops.undo_desk}[action]
            t = _do(request, lambda: fn(ticket, staff))
            if t:
                messages.success(request, f"{t.passenger.full_name}: {t.get_status_display().lower()}.")
        return redirect(f"{reverse('console:check_in')}?{urlencode({'q': query})}" if query else reverse("console:check_in"))

    booking, ticket, message = ops.find_for_desk(query) if query else (None, None, "")
    passengers = []
    if booking:
        booking = Booking.objects.select_related("user", "flight__origin", "flight__destination", "fare_class",
                                                 "package").get(pk=booking.pk)
        passengers = list(booking.passengers.select_related("seat", "ticket").prefetch_related("baggage__option"))
        if booking.status != Booking.Status.CONFIRMED:
            message = f"Booking {booking.reference} is {booking.get_status_display().lower()} – not valid for travel."
    departures = list(ops.desk_departures(36))
    for f in departures:
        f.progress = pct(f.checked, f.tickets)
    return render(request, "console/check_in.html", {
        "nav": "check_in", "q": query, "booking": booking, "ticket": ticket, "passengers": passengers,
        "message": message, "departures": departures,
        "issued": sum(1 for p in passengers if getattr(getattr(p, "ticket", None), "status", "") == "ISSUED"),
    })


# --------------------------------------------------------------------------- flight manifest
@staff_required
def flight_manifest(request, pk):
    f = get_object_or_404(Flight.objects.select_related("origin", "destination", "aircraft"), pk=pk)
    pax = list(Passenger.objects.filter(booking__flight=f, booking__status=Booking.Status.CONFIRMED)
               .select_related("booking__user", "booking__fare_class", "seat", "ticket")
               .prefetch_related("baggage__option")
               .order_by(F("seat__row").asc(nulls_last=True), "seat__letter", "last_name"))
    if request.GET.get("export") == "csv":
        rows = ([p.seat.number if p.seat_id else "", p.last_name.upper(), p.first_name, p.get_passenger_type_display(),
                 p.get_gender_display(), p.date_of_birth.isoformat(), p.nationality, p.passport_number,
                 p.passport_expiry.isoformat() if p.passport_expiry else "", p.booking.reference,
                 p.booking.fare_class.name if p.booking.fare_class_id else "",
                 getattr(getattr(p, "ticket", None), "ticket_number", ""),
                 getattr(getattr(p, "ticket", None), "get_status_display", lambda: "")(),
                 "; ".join(f"{bg.quantity}x {bg.option.name}" for bg in p.baggage.all()),
                 p.booking.contact_phone, p.special_requests] for p in pax)
        return _csv(f"manifest-{f.flight_number}-{timezone.localtime(f.departure_time):%Y%m%d}.csv",
                    ["Seat", "Surname", "Given names", "Type", "Gender", "Date of birth", "Nationality", "Passport",
                     "Passport expiry", "Booking", "Fare", "Ticket", "Status", "Extra baggage", "Contact phone",
                     "Special requests"], rows)
    statuses = {"ISSUED": 0, "CHECKED_IN": 0, "BOARDED": 0}
    for p in pax:
        t = getattr(p, "ticket", None)
        if t and t.status in statuses:
            statuses[t.status] += 1
    return render(request, "console/flight_manifest.html", {
        "nav": "flights", "f": f, "pax": pax, "statuses": statuses, "total": len(pax),
        "infants": sum(1 for p in pax if p.passenger_type == Passenger.Type.INFANT),
        "printed": timezone.localtime(),
    })
