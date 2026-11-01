"""
Dynamic pricing engine.

fare = base_fare × demand_multiplier(load factor) × time_multiplier(days to departure)
rounded up to the nearest 10 Birr, plus fixed taxes, seat surcharges and baggage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, Decimal

from django.utils import timezone

from ..models import Cabin, FareClass, Passenger

# (days-to-departure lower bound, multiplier) – evaluated top-down
TIME_BANDS = [(60, Decimal("0.90")), (30, Decimal("1.00")), (14, Decimal("1.10")), (7, Decimal("1.22")), (0, Decimal("1.38"))]
CHILD_DISCOUNT = Decimal("0.75")
INFANT_DISCOUNT = Decimal("0.10")


def demand_multiplier(fare: FareClass) -> Decimal:
    if not fare.seats_allocated:
        return Decimal("1")
    load = Decimal(fare.seats_sold) / Decimal(fare.seats_allocated)
    if load <= Decimal("0.5"):
        return Decimal("1.00")
    # linear 1.00 → 1.60 between 50 % and 100 % load factor
    return (Decimal("1.00") + (load - Decimal("0.5")) * Decimal("1.2")).quantize(Decimal("0.001"))


def time_multiplier(fare: FareClass, now=None) -> Decimal:
    now = now or timezone.now()
    days = (fare.flight.departure_time - now).days
    for lower, mult in TIME_BANDS:
        if days >= lower:
            return mult
    return TIME_BANDS[-1][1]


def _round10(value: Decimal) -> Decimal:
    return (value / 10).quantize(Decimal("1"), rounding=ROUND_CEILING) * 10


def current_fare(fare: FareClass, passenger_type: str = Passenger.Type.ADULT, now=None) -> Decimal:
    price = _round10(fare.base_fare * demand_multiplier(fare) * time_multiplier(fare, now))
    if passenger_type == Passenger.Type.CHILD:
        price = _round10(price * CHILD_DISCOUNT)
    elif passenger_type == Passenger.Type.INFANT:
        price = _round10(price * INFANT_DISCOUNT)
    return price


def seat_price(fare: FareClass, seat) -> Decimal:
    if seat is None:
        return Decimal("0")
    if fare.free_seat_selection or fare.cabin in (Cabin.BUSINESS, Cabin.FIRST):
        # Premium cabins include seat choice; only extra-legroom/exit surcharges apply in economy.
        return Decimal("0")
    return Decimal(seat.surcharge)


@dataclass
class PassengerQuote:
    passenger_type: str
    fare: Decimal
    taxes: Decimal
    seat: Decimal
    baggage: Decimal

    @property
    def total(self):
        return self.fare + self.taxes + self.seat + self.baggage


@dataclass
class Quote:
    currency: str = "ETB"
    passengers: list[PassengerQuote] = field(default_factory=list)
    discount: Decimal = Decimal("0")

    def _sum(self, attr):
        return sum((getattr(p, attr) for p in self.passengers), Decimal("0"))

    @property
    def fare(self):
        return self._sum("fare")

    @property
    def taxes(self):
        return self._sum("taxes")

    @property
    def seat(self):
        return self._sum("seat")

    @property
    def baggage(self):
        return self._sum("baggage")

    @property
    def total(self):
        return max(self.fare + self.taxes + self.seat + self.baggage - self.discount, Decimal("0"))

    def as_dict(self):
        return {
            "currency": self.currency,
            "fare": str(self.fare), "taxes": str(self.taxes), "seat": str(self.seat),
            "baggage": str(self.baggage), "discount": str(self.discount), "total": str(self.total),
            "passengers": [
                {"type": p.passenger_type, "fare": str(p.fare), "taxes": str(p.taxes), "seat": str(p.seat),
                 "baggage": str(p.baggage), "total": str(p.total)}
                for p in self.passengers
            ],
        }


def quote_flight(fare: FareClass, passengers: list[dict], now=None) -> Quote:
    """``passengers``: [{"passenger_type": "ADT", "seat": Seat|None, "baggage": [(BaggageOption, qty)]}]"""
    q = Quote()
    for pax in passengers:
        ptype = pax.get("passenger_type", Passenger.Type.ADULT)
        bag_total = sum((opt.price * qty for opt, qty in pax.get("baggage", [])), Decimal("0"))
        q.passengers.append(PassengerQuote(
            passenger_type=ptype,
            fare=current_fare(fare, ptype, now),
            taxes=Decimal(fare.taxes) if ptype != Passenger.Type.INFANT else Decimal("0"),
            seat=seat_price(fare, pax.get("seat")),
            baggage=bag_total,
        ))
    return q


def quote_package(package, passengers: list[dict]) -> Quote:
    q = Quote()
    for pax in passengers:
        ptype = pax.get("passenger_type", Passenger.Type.ADULT)
        price = Decimal(package.price)
        if ptype == Passenger.Type.CHILD:
            price = _round10(price * Decimal("0.85"))
        elif ptype == Passenger.Type.INFANT:
            price = _round10(price * Decimal("0.20"))
        bag_total = sum((opt.price * qty for opt, qty in pax.get("baggage", [])), Decimal("0"))
        q.passengers.append(PassengerQuote(ptype, price, Decimal("0"), Decimal("0"), bag_total))
    return q
