"""
Seat inventory with pessimistic locking.

Seats are held with ``SELECT … FOR UPDATE`` (rows locked in a stable order to
avoid deadlocks), so two users can never hold or book the same seat. A hold
expires after ``SEAT_LOCK_MINUTES``; expired holds are treated as available and
are swept by the ``expire_holds`` management command.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError

from ..models import Seat


def lock_minutes():
    return settings.RIHLA["SEAT_LOCK_MINUTES"]


@transaction.atomic
def lock_seats(user, flight, seat_ids: list[int], minutes: int | None = None) -> list[Seat]:
    if not seat_ids:
        raise BusinessRuleError("Select at least one seat.")
    if len(seat_ids) > settings.RIHLA["MAX_SEATS_PER_LOCK"]:
        raise BusinessRuleError(f"You can hold at most {settings.RIHLA['MAX_SEATS_PER_LOCK']} seats.")
    if not flight.is_bookable:
        raise BusinessRuleError("This flight is no longer open for booking.")

    seats = list(Seat.objects.select_for_update().filter(flight=flight, id__in=seat_ids).order_by("id"))
    if len(seats) != len(set(seat_ids)):
        raise BusinessRuleError("One or more seats do not belong to this flight.")
    unavailable = [s.number for s in seats if not s.is_free_for(user)]
    if unavailable:
        raise BusinessRuleError(f"Seat(s) {', '.join(unavailable)} were just taken. Please choose another seat.")

    until = timezone.now() + timedelta(minutes=minutes or lock_minutes())
    # Release this user's other un-booked holds on the same flight (one active selection per user).
    Seat.objects.filter(flight=flight, locked_by=user, status=Seat.Status.LOCKED, passenger__isnull=True).exclude(
        id__in=seat_ids).update(status=Seat.Status.AVAILABLE, locked_by=None, locked_until=None)
    Seat.objects.filter(id__in=[s.id for s in seats]).update(status=Seat.Status.LOCKED, locked_by=user, locked_until=until)
    for s in seats:
        s.status, s.locked_by, s.locked_until = Seat.Status.LOCKED, user, until
    return seats


@transaction.atomic
def release_seats(user, flight, seat_ids: list[int] | None = None) -> int:
    qs = Seat.objects.select_for_update().filter(flight=flight, locked_by=user, status=Seat.Status.LOCKED,
                                                 passenger__isnull=True)
    if seat_ids:
        qs = qs.filter(id__in=seat_ids)
    return qs.update(status=Seat.Status.AVAILABLE, locked_by=None, locked_until=None)


def extend_hold(seats: list[Seat], until):
    Seat.objects.filter(id__in=[s.id for s in seats]).update(locked_until=until)


def expire_stale_holds() -> int:
    return Seat.objects.filter(status=Seat.Status.LOCKED, locked_until__lt=timezone.now()).filter(
        Q(passenger__isnull=True) | Q(passenger__booking__status__in=["EXPIRED", "CANCELLED"])
    ).update(status=Seat.Status.AVAILABLE, locked_by=None, locked_until=None)


def viewer_status(seat: Seat, user) -> str:
    """Status as seen by the requesting user (hides other users' identities)."""
    now = timezone.now()
    if seat.status == Seat.Status.LOCKED and seat.locked_until and seat.locked_until <= now:
        return "AVAILABLE"
    if seat.status == Seat.Status.LOCKED and user and user.is_authenticated and seat.locked_by_id == user.id:
        return "HELD_BY_YOU"
    if seat.status in (Seat.Status.LOCKED, Seat.Status.BOOKED, Seat.Status.BLOCKED):
        return "OCCUPIED"
    return "AVAILABLE"


def build_seat_map(flight, user) -> dict:
    seats = list(flight.seats.all().order_by("row", "letter"))
    by_cabin: dict[str, dict] = {}
    for block in flight.aircraft.layout:
        by_cabin.setdefault(block["cabin"], {"cabin": block["cabin"], "layout": block["letters"], "rows": {}})
    for seat in seats:
        cabin = by_cabin.setdefault(seat.cabin, {"cabin": seat.cabin, "layout": "", "rows": {}})
        row = cabin["rows"].setdefault(seat.row, {"row": seat.row, "is_exit_row": seat.is_exit_row, "seats": []})
        row["seats"].append({
            "id": seat.id, "number": seat.number, "letter": seat.letter, "position": seat.position,
            "extra_legroom": seat.extra_legroom, "is_exit_row": seat.is_exit_row,
            "surcharge": str(seat.surcharge), "status": viewer_status(seat, user),
        })
    cabins = []
    for c in by_cabin.values():
        c["rows"] = [c["rows"][r] for r in sorted(c["rows"])]
        if c["rows"]:
            cabins.append(c)
    return {
        "flight_id": flight.id,
        "aircraft": flight.aircraft.name,
        "lock_minutes": lock_minutes(),
        "legend": ["AVAILABLE", "OCCUPIED", "HELD_BY_YOU"],
        "cabins": cabins,
    }
