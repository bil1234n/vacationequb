"""E-ticket issuance, signed QR codes and PDF itinerary rendering."""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json

import qrcode
from django.conf import settings
from django.db import IntegrityError, transaction
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from apps.core.utils import generate_numeric

from ..models import Booking, Ticket

GREEN = colors.HexColor("#0F382B")
GREEN_2 = colors.HexColor("#1B4D3E")
MUTED = colors.HexColor("#5B6F66")
LINE = colors.HexColor("#D9E5DF")
TICKET_PREFIX = "966"  # placeholder plating carrier code – use your IATA/BSP code in production


def _key() -> bytes:
    return settings.VACATION["TICKET_SIGNING_KEY"].encode()


def sign_payload(data: dict) -> str:
    body = json.dumps(data, separators=(",", ":"), sort_keys=True)
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:20]
    return base64.urlsafe_b64encode(f"{body}.{sig}".encode()).decode().rstrip("=")


def verify_payload(token: str) -> dict | None:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        body, sig = raw.rsplit(".", 1)
    except (ValueError, UnicodeDecodeError):
        return None
    expected = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:20]
    return json.loads(body) if hmac.compare_digest(sig, expected) else None


def _qr_data(booking: Booking, pax, ticket_number: str) -> dict:
    data = {"t": ticket_number, "p": booking.reference, "n": pax.full_name.upper()}
    if booking.flight_id:
        data |= {"f": booking.flight.flight_number, "d": booking.flight.departure_time.strftime("%Y%m%d%H%M"),
                 "o": booking.flight.origin.iata, "a": booking.flight.destination.iata,
                 "c": booking.fare_class.cabin[:1], "s": pax.seat.number if pax.seat_id else ""}
    else:
        data |= {"k": booking.package.slug}
    return data


def issue_tickets(booking: Booking) -> list[Ticket]:
    tickets = []
    for pax in booking.passengers.select_related("seat").all():
        if hasattr(pax, "ticket"):
            tickets.append(pax.ticket)
            continue
        for _ in range(5):
            number = TICKET_PREFIX + generate_numeric(10)
            try:
                with transaction.atomic():
                    tickets.append(Ticket.objects.create(
                        booking=booking, passenger=pax, ticket_number=number,
                        qr_payload=sign_payload(_qr_data(booking, pax, number)),
                    ))
                break
            except IntegrityError:
                continue
    return tickets


def qr_png(payload: str, box_size: int = 8) -> bytes:
    img = qrcode.make(payload, box_size=box_size, border=2,
                      image_factory=None, error_correction=qrcode.constants.ERROR_CORRECT_M)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def render_itinerary_pdf(booking: Booking) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    margin = 18 * mm

    def header():
        c.setFillColor(GREEN)
        c.rect(0, height - 42 * mm, width, 42 * mm, fill=1, stroke=0)
        c.setFillColor(colors.white)
        c.setFont("Helvetica-Bold", 22)
        c.drawString(margin, height - 20 * mm, "VACATION EQUB")
        c.setFont("Helvetica", 10)
        c.drawString(margin, height - 27 * mm, "Electronic ticket itinerary & receipt")
        c.setFont("Helvetica-Bold", 12)
        c.drawRightString(width - margin, height - 20 * mm, f"Booking ref  {booking.reference}")
        c.setFont("Helvetica", 10)
        c.drawRightString(width - margin, height - 27 * mm, f"Status: {booking.get_status_display()}")

    header()
    y = height - 56 * mm
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 13)
    if booking.flight_id:
        f = booking.flight
        c.drawString(margin, y, f"{f.origin.city} ({f.origin.iata})  →  {f.destination.city} ({f.destination.iata})")
        y -= 7 * mm
        c.setFont("Helvetica", 10)
        c.setFillColor(MUTED)
        dep = f.departure_time.astimezone().strftime("%a %d %b %Y, %H:%M")
        arr = f.arrival_time.astimezone().strftime("%a %d %b %Y, %H:%M")
        c.drawString(margin, y, f"{f.airline_name} {f.flight_number} · {f.aircraft.name} · {booking.fare_class.name} ({booking.fare_class.code})")
        y -= 5.5 * mm
        c.drawString(margin, y, f"Departs {dep}   ·   Arrives {arr}   ·   Duration {f.duration_minutes // 60}h {f.duration_minutes % 60:02d}m")
        y -= 5.5 * mm
        c.drawString(margin, y, f"Baggage: {booking.fare_class.checked_baggage_kg} kg checked + {booking.fare_class.cabin_baggage_kg} kg cabin per passenger")
    else:
        p = booking.package
        c.drawString(margin, y, f"{p.name}")
        y -= 7 * mm
        c.setFont("Helvetica", 10)
        c.setFillColor(MUTED)
        c.drawString(margin, y, f"{p.destination_city}, {p.destination_country} · {p.duration_days} days · {p.departure_date:%d %b %Y} – {p.return_date:%d %b %Y}")
        if p.includes:
            y -= 5.5 * mm
            c.drawString(margin, y, "Includes: " + ", ".join(p.includes)[:110])
    y -= 10 * mm

    for ticket in booking.tickets.select_related("passenger__seat"):
        pax = ticket.passenger
        if y < 60 * mm:
            c.showPage()
            header()
            y = height - 56 * mm
        box_h = 40 * mm
        c.setStrokeColor(LINE)
        c.setFillColor(colors.white)
        c.roundRect(margin, y - box_h, width - 2 * margin, box_h, 4 * mm, fill=1, stroke=1)
        c.setFillColor(GREEN)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(margin + 6 * mm, y - 9 * mm, pax.full_name.upper())
        c.setFont("Helvetica", 9.5)
        c.setFillColor(MUTED)
        c.drawString(margin + 6 * mm, y - 15 * mm, f"{pax.get_passenger_type_display()}  ·  Ticket {ticket.ticket_number}")
        if pax.seat_id:
            c.drawString(margin + 6 * mm, y - 21 * mm, f"Seat {pax.seat.number} ({pax.seat.get_position_display()})")
        bags = ", ".join(f"{b.quantity}× {b.option.name}" for b in pax.baggage.select_related("option"))
        if bags:
            c.drawString(margin + 6 * mm, y - 27 * mm, f"Extra baggage: {bags}")
        qr = ImageReader(io.BytesIO(qr_png(ticket.qr_payload, 4)))
        c.drawImage(qr, width - margin - 36 * mm, y - box_h + 3 * mm, 34 * mm, 34 * mm)
        y -= box_h + 6 * mm

    # Fare summary
    if y < 55 * mm:
        c.showPage()
        header()
        y = height - 56 * mm
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(margin, y, "Payment summary")
    y -= 7 * mm
    c.setFont("Helvetica", 10)
    rows = [("Fare", booking.fare_amount), ("Taxes & fees", booking.tax_amount), ("Seats", booking.seat_amount),
            ("Extra baggage", booking.baggage_amount)]
    if booking.discount_amount:
        rows.append((f"Credit ({booking.get_funding_source_display()})", -booking.discount_amount))
    for label, value in rows:
        c.setFillColor(MUTED)
        c.drawString(margin, y, label)
        c.drawRightString(width - margin, y, f"{value:,.2f} {booking.currency}")
        y -= 5.5 * mm
    c.setStrokeColor(LINE)
    c.line(margin, y + 2 * mm, width - margin, y + 2 * mm)
    y -= 3 * mm
    c.setFillColor(GREEN)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(margin, y, "Total paid")
    c.drawRightString(width - margin, y, f"{booking.total_amount:,.2f} {booking.currency}")

    c.setFont("Helvetica", 8)
    c.setFillColor(MUTED)
    c.drawString(margin, 12 * mm, "Present this itinerary and a valid passport at check-in. Check-in closes 60 minutes before departure.")
    c.showPage()
    c.save()
    return buf.getvalue()
