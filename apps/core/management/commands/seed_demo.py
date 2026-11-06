"""Load realistic demo data: airports, aircraft, flights with seat maps, packages, Equbs, crowdfunding and giveaways."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

B738 = [
    {"cabin": "BUSINESS", "rows": [1, 4], "letters": "AC|DF"},
    {"cabin": "ECONOMY", "rows": [10, 32], "letters": "ABC|DEF", "exit_rows": [16, 17],
     "extra_legroom_rows": [10, 16, 17], "extra_legroom_surcharge": 1500, "window_surcharge": 350, "aisle_surcharge": 250},
]
B788 = [
    {"cabin": "BUSINESS", "rows": [1, 6], "letters": "AC|DG|HK"},
    {"cabin": "ECONOMY", "rows": [10, 40], "letters": "ABC|DEFG|HJK", "exit_rows": [20],
     "extra_legroom_rows": [10, 20], "extra_legroom_surcharge": 2500, "window_surcharge": 500, "aisle_surcharge": 400},
]

ROUTES = [
    # number, origin, dest, dep hour, minutes, aircraft, economy, business, taxes
    ("ET600", "ADD", "DXB", 9, 245, "B738", 18500, 52000, 3200),
    ("ET602", "ADD", "DXB", 22, 245, "B788", 17800, 49500, 3200),
    ("ET412", "ADD", "JED", 8, 150, "B738", 16900, 45000, 2900),
    ("ET424", "ADD", "MED", 14, 170, "B788", 17500, 46500, 2900),
    ("ET684", "ADD", "CAN", 23, 600, "B788", 48500, 128000, 5400),
    ("ET604", "ADD", "PEK", 1, 660, "B788", 51000, 135000, 5400),
    ("ET690", "ADD", "IST", 7, 330, "B738", 31000, 82000, 4100),
    ("ET106", "ADD", "DIR", 6, 55, "B738", 4200, 9800, 450),
]


class Command(BaseCommand):
    help = "Seed demo data for local development and app demos"

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=21, help="How many days of flights to create")

    @transaction.atomic
    def handle(self, *args, **opts):
        from apps.accounts.models import User
        from apps.equb.models import EqubGroup, EqubPackage
        from apps.equb.services import join_group
        from apps.luckydraw.models import CrowdfundingCampaign, Giveaway
        from apps.travel.models import AircraftType, Airport, BaggageOption, FareClass, Flight, TravelPackage

        airports = {
            "ADD": ("Addis Ababa Bole International", "Addis Ababa", "Ethiopia", "Africa/Addis_Ababa"),
            "DIR": ("Aba Tenna Dejazmach Yilma", "Dire Dawa", "Ethiopia", "Africa/Addis_Ababa"),
            "DXB": ("Dubai International", "Dubai", "United Arab Emirates", "Asia/Dubai"),
            "JED": ("King Abdulaziz International", "Jeddah", "Saudi Arabia", "Asia/Riyadh"),
            "MED": ("Prince Mohammad bin Abdulaziz", "Madinah", "Saudi Arabia", "Asia/Riyadh"),
            "CAN": ("Guangzhou Baiyun International", "Guangzhou", "China", "Asia/Shanghai"),
            "PEK": ("Beijing Capital International", "Beijing", "China", "Asia/Shanghai"),
            "IST": ("Istanbul Airport", "Istanbul", "Türkiye", "Europe/Istanbul"),
        }
        ap = {}
        for iata, (name, city, country, tz) in airports.items():
            ap[iata], _ = Airport.objects.update_or_create(iata=iata, defaults=dict(name=name, city=city, country=country, timezone=tz))
        ac = {
            "B738": AircraftType.objects.update_or_create(code="B738", defaults={"name": "Boeing 737-800", "layout": B738})[0],
            "B788": AircraftType.objects.update_or_create(code="B788", defaults={"name": "Boeing 787-8 Dreamliner", "layout": B788})[0],
        }
        for code, name, kg, price, intl in [("BAG10", "Extra bag 10 kg", 10, 1800, False), ("BAG23", "Extra bag 23 kg", 23, 3900, True),
                                            ("BAG32", "Heavy bag 32 kg", 32, 5600, True), ("ZAMZAM", "Zamzam water 10 L", 10, 900, True)]:
            BaggageOption.objects.update_or_create(code=code, defaults=dict(name=name, weight_kg=kg, price=price, international_only=intl))

        tz = timezone.get_current_timezone()
        today = timezone.localdate()
        flights = 0
        for d in range(1, opts["days"] + 1):
            day = today + timedelta(days=d)
            for number, o, dst, hour, mins, craft, eco, bus, tax in ROUTES:
                dep = datetime.combine(day, time(hour, 0), tzinfo=tz)
                f, created = Flight.objects.get_or_create(
                    flight_number=number, departure_time=dep,
                    defaults=dict(origin=ap[o], destination=ap[dst], arrival_time=dep + timedelta(minutes=mins),
                                  aircraft=ac[craft], is_international=dst not in ("DIR",), terminal="2"))
                if created:
                    f.generate_seats()
                    eco_seats = f.seats.filter(cabin="ECONOMY").count()
                    bus_seats = f.seats.filter(cabin="BUSINESS").count()
                    FareClass.objects.create(flight=f, cabin="ECONOMY", code="K", name="Economy Saver", base_fare=eco,
                                             taxes=tax, seats_allocated=eco_seats // 2, checked_baggage_kg=23, change_fee=2500)
                    FareClass.objects.create(flight=f, cabin="ECONOMY", code="Y", name="Economy Flex", base_fare=int(eco * 1.35),
                                             taxes=tax, seats_allocated=eco_seats - eco_seats // 2, refundable=True,
                                             change_fee=1500, checked_baggage_kg=46, free_seat_selection=True)
                    FareClass.objects.create(flight=f, cabin="BUSINESS", code="J", name="Cloud Nine Business", base_fare=bus,
                                             taxes=tax, seats_allocated=bus_seats, refundable=True, checked_baggage_kg=64,
                                             cabin_baggage_kg=14, free_seat_selection=True)
                    flights += 1

        packages = [
            ("Umrah Economy – 15 Days", "UMRAH", "Makkah & Madinah", "Saudi Arabia", 15, 185000, 30, ["Umrah visa", "Return flight ADD–JED", "4★ hotel near Haram", "Ziyarah tours", "Airport transfers"], "Swissôtel Al Maqam", 4, 25, True),
            ("Hajj Premium 1448", "HAJJ", "Makkah", "Saudi Arabia", 21, 640000, 120, ["Hajj visa", "Return flights", "5★ Makkah hotel", "Mina & Arafat tents", "Scholar guidance"], "Fairmont Makkah", 5, 40, True),
            ("Dubai Business Explorer", "DUBAI", "Dubai", "United Arab Emirates", 6, 78000, 12, ["Visa", "Return flight", "Deira hotel", "Dragon Mart & Gold Souk tours"], "Hyatt Regency Deira", 4, 20, True),
            ("Guangzhou Canton Fair Trade Tour", "CHINA", "Guangzhou", "China", 10, 215000, 60, ["Business visa", "Return flight ADD–CAN", "Canton Fair badge", "Interpreter", "Factory visits"], "Shangri-La Guangzhou", 5, 25, True),
            ("Istanbul Shopping & Heritage", "TURKEY", "Istanbul", "Türkiye", 7, 96000, 30, ["E-visa", "Return flight", "Old city hotel", "Grand Bazaar tour"], "Sultanahmet Palace", 4, 20, False),
        ]
        pk_objs = {}
        for name, cat, city, country, days, price, start, includes, hotel, stars, cap, feat in packages:
            pk_objs[cat], _ = TravelPackage.objects.update_or_create(name=name, defaults=dict(
                category=cat, destination_city=city, destination_country=country, duration_days=days, price=price,
                deposit_amount=Decimal(price) * Decimal("0.2"), departure_date=today + timedelta(days=start),
                return_date=today + timedelta(days=start + days), capacity=cap, includes=includes, hotel_name=hotel,
                hotel_stars=stars, is_featured=feat,
                summary=f"{days} days in {city} – {', '.join(includes[:3])}."))

        admin, _ = User.objects.get_or_create(phone="+251900000000", defaults={"full_name": "Vacation Equb Admin", "is_staff": True, "is_superuser": True})
        admin.set_password("admin12345")
        admin.save()
        demo, _ = User.objects.get_or_create(phone="+251911223344", defaults={"full_name": "Abebe Kebede", "email": "abebe@example.com"})
        demo.set_password("demo12345")
        demo.save()
        members = [demo]
        for i, name in enumerate(["Hana Tesfaye", "Mohammed Ali", "Selam Girma", "Yusuf Ahmed", "Meron Alemu",
                                  "Kedir Hassen", "Tigist Bekele", "Ibrahim Omar", "Liya Haile"], start=1):
            u, _ = User.objects.get_or_create(phone=f"+25191100{i:04d}", defaults={"full_name": name})
            u.set_password("demo12345")
            u.save()
            members.append(u)

        equbs = [
            ("Umrah 1448 Family Equb", "UMRAH", pk_objs["UMRAH"], 15500, "MONTHLY", 12, "Save monthly together; each month one member is drawn and travels on a fully-funded Umrah."),
            ("Hajj Savers Circle", "HAJJ", pk_objs["HAJJ"], 32000, "MONTHLY", 20, "A 20-month circle for Hajj. Winners get their full Hajj package funded."),
            ("Dubai Traders Weekly", "DUBAI", pk_objs["DUBAI"], 6500, "WEEKLY", 12, "Weekly Equb for Dubai business trips – winner gets flights, visa and hotel."),
            ("Guangzhou Import Equb", "CHINA", pk_objs["CHINA"], 18000, "BIWEEKLY", 12, "For importers heading to the Canton Fair."),
        ]
        dest_names = {"HAJJ": "Hajj", "UMRAH": "Umrah", "DUBAI": "Dubai", "CHINA": "China trade"}
        for name, dest, pkg, amount, freq, size, desc in equbs:
            # Equb package (template) → group created from it → members
            equb_pkg, _ = EqubPackage.objects.get_or_create(name=f"{dest_names[dest]} Equb Package", defaults=dict(
                terms_content="Winners must travel within 12 months of their draw.\nPayouts can't be transferred."))
            g, created = EqubGroup.objects.get_or_create(name=name, defaults=dict(
                **equb_pkg.group_defaults(), equb_package=equb_pkg, contribution_amount=amount,
                frequency_days={"WEEKLY": 7, "BIWEEKLY": 14}.get(freq, 30),
                start_date=today + timedelta(days=7), max_members=size,
                duration_value=size * (2 if freq == "BIWEEKLY" else 1), duration_unit="MONTHS" if freq == "MONTHLY" else "WEEKS",
                draw_type="AUTOMATIC", organizer=admin))
            if created:
                for u in members[: min(len(members), size - 2)]:
                    join_group(u, g)

        # A Group Equb: the demo customer's family saving together in an open Equb, with a child who has no phone.
        from apps.equb.models import GroupEqub
        from apps.equb.teams import create_group_equb

        if not GroupEqub.objects.filter(owner=demo).exists():
            fam_equb, made = EqubGroup.objects.get_or_create(name="Umrah Family Savers", defaults=dict(
                equb_package=EqubPackage.objects.get(name="Umrah Equb Package"), contribution_amount=Decimal("5000"),
                frequency_days=30, duration_value=10, duration_unit="MONTHS", max_members=10,
                start_date=today + timedelta(days=14), draw_type="MANUAL", organizer=admin))
            if fam_equb.status == "OPEN":
                create_group_equb(demo, fam_equb, name="Kebede Family", kind="FAMILY",
                                  description="Our family saves together so everyone can go to Umrah.",
                                  members=[m for m in members[1:3] if not fam_equb.memberships.filter(user=m).exists()],
                                  dependents=[{"name": "Liya Abebe"}], created_by=admin)

        now = timezone.now()
        # Community Finance & Impact: Sadaqah crowdfunding (fee added on top) and free, sponsored giveaways.
        CrowdfundingCampaign.objects.get_or_create(title="Help Ummi Fatuma reach Umrah", defaults=dict(
            category="UMRAH", beneficiary_name="Fatuma Ali (72), Addis Ababa", goal_amount=Decimal("185000"),
            story="Ummi Fatuma has served tea at our neighbourhood masjid for 30 years and has always dreamed of "
                  "Umrah. Your Sadaqah goes 100% to her Umrah package – Vacation Equb's 5% platform fee is shown separately.",
            starts_at=now - timedelta(days=3), is_featured=True, created_by=admin))
        CrowdfundingCampaign.objects.get_or_create(title="Hajj fund for our Imam", defaults=dict(
            category="HAJJ", beneficiary_name="Imam Abdulkadir, Dessie", goal_amount=Decimal("650000"),
            story="The community of Dessie is gathering Hibah so our Imam can perform Hajj this year.",
            starts_at=now - timedelta(days=1), created_by=admin))
        Giveaway.objects.get_or_create(title="Umrah Giveaway", defaults=dict(
            description="One Umrah Economy package, fully paid by Vacation Equb. Entry is free – invite 10 friends who "
                        "register on Vacation Equb and you get an entry.",
            sponsor_type="COMPANY", sponsor_name="Vacation Equb", sponsor_note="Paid from Vacation Equb's marketing budget.",
            prize_type="TRAVEL_PACKAGE", prize_title="Umrah Economy package for one", prize_value=Decimal("185000"),
            prize_package=pk_objs["UMRAH"], number_of_winners=1, starts_at=now - timedelta(days=1),
            ends_at=now + timedelta(days=21), draw_at=now + timedelta(days=21, hours=1), is_featured=True))
        Giveaway.objects.get_or_create(title="Dubai Weekend Giveaway", defaults=dict(
            description="Three winners get 25,000 ETB flight credit to Dubai, sponsored by our partner.",
            sponsor_type="CORPORATE", sponsor_name="Habesha Trade PLC", sponsor_note="100% sponsored by Habesha Trade PLC.",
            prize_type="TRAVEL_CREDIT", prize_title="25,000 ETB flight credit", prize_value=Decimal("25000"),
            number_of_winners=3, starts_at=now - timedelta(hours=2), ends_at=now + timedelta(days=10),
            draw_at=now + timedelta(days=10, hours=1)))

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {flights} new flights, {len(packages)} packages, {len(equbs)} Equbs, 2 crowdfunding campaigns, 2 giveaways.\n"
            "Admin login: +251900000000 / admin12345   Demo user: +251911223344 / demo12345"))
