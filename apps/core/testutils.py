"""Shared fixtures for the test-suite."""
from datetime import datetime, time, timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.travel.models import AircraftType, Airport, BaggageOption, FareClass, Flight, TravelPackage

LAYOUT = [
    {"cabin": "BUSINESS", "rows": [1, 2], "letters": "AC|DF"},
    {"cabin": "ECONOMY", "rows": [10, 14], "letters": "ABC|DEF", "extra_legroom_rows": [10],
     "extra_legroom_surcharge": 1500, "window_surcharge": 300, "aisle_surcharge": 200},
]

_counter = {"n": 0}


def make_user(name="Test User", staff=False, **extra):
    _counter["n"] += 1
    return User.objects.create_user(phone=f"+2519{_counter['n']:08d}", password="pass12345", full_name=name,
                                    is_staff=staff, **extra)


def client_for(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def make_flight(days_ahead=40, economy_seats=20):
    add, _ = Airport.objects.get_or_create(iata="ADD", defaults=dict(name="Bole", city="Addis Ababa", country="Ethiopia"))
    dxb, _ = Airport.objects.get_or_create(iata="DXB", defaults=dict(name="DXB", city="Dubai", country="UAE"))
    ac, _ = AircraftType.objects.get_or_create(code="T1", defaults=dict(name="Test 737", layout=LAYOUT))
    dep = datetime.combine(timezone.localdate() + timedelta(days=days_ahead), time(9), tzinfo=timezone.get_current_timezone())
    f = Flight.objects.create(flight_number=f"ET{600 + Flight.objects.count()}", origin=add, destination=dxb,
                              departure_time=dep, arrival_time=dep + timedelta(hours=4), aircraft=ac)
    f.generate_seats()
    eco = FareClass.objects.create(flight=f, cabin="ECONOMY", code="K", name="Economy Saver", base_fare=18000,
                                   taxes=3200, seats_allocated=economy_seats)
    bus = FareClass.objects.create(flight=f, cabin="BUSINESS", code="J", name="Business", base_fare=52000,
                                   taxes=3200, seats_allocated=8, refundable=True)
    return f, eco, bus


def make_package(price=185000, capacity=10):
    today = timezone.localdate()
    return TravelPackage.objects.create(
        name="Umrah Test", category="UMRAH", destination_city="Makkah", destination_country="Saudi Arabia",
        summary="Test", duration_days=15, price=price, departure_date=today + timedelta(days=60),
        return_date=today + timedelta(days=75), capacity=capacity)


def make_bag():
    return BaggageOption.objects.get_or_create(code="BAG23", defaults=dict(name="Extra 23kg", weight_kg=23, price=3900))[0]


def passenger(seat=None, **kw):
    data = {"first_name": "Abebe", "last_name": "Kebede", "gender": "M", "date_of_birth": "1990-01-01",
            "passport_number": "EP1234567", "passport_expiry": "2030-01-01"}
    if seat:
        data["seat"] = seat
    data.update(kw)
    return data


RULE_FIELDS = ("service_fee_percent", "late_fee", "grace_days", "require_paid_to_draw", "payout_basis",
               "auto_start_when_full")
GONE_FIELDS = ("destination", "package", "description", "payout_type", "is_public", "kind", "image_url",
               "min_members_to_start", "winners_per_round")


def make_equb(**kw):
    """Create an Equb group. Money rules are company-wide now, so they go to the Equb rules (EqubSettings)."""
    from apps.equb.models import EqubGroup, EqubSettings

    rules = {k: kw.pop(k) for k in RULE_FIELDS if k in kw}
    for k in GONE_FIELDS:
        kw.pop(k, None)
    if rules:
        s = EqubSettings.load()
        for k, v in rules.items():
            setattr(s, k, v)
        s.save()
    return EqubGroup.objects.create(**kw)


def plenty_of_money():
    """
    Patch for tests about who can win (pools, selection, groups) rather than about money: pretend the Equb holds
    enough for 99 winners. The money rule itself is tested separately.
    """
    from decimal import Decimal
    from unittest import mock

    def fake(group, rnd=None):
        from apps.equb.services import prize_for

        prize = prize_for(group, rnd)
        fee = (prize * Decimal(group.service_fee_percent) / 100).quantize(Decimal("0.01"))
        return {"collected": prize * 99, "paid_out": Decimal("0"), "available": prize * 99, "prize": prize, "fee": fee,
                "net": prize - fee, "max_winners": 99, "short_for_one": Decimal("0")}

    return mock.patch("apps.equb.services.equb_funds", new=fake)
