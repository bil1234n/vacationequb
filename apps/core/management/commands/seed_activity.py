"""
Generate ~30 days of realistic DEMO activity so the console charts have data:
customers, paid/pending/cancelled bookings, Equb contributions & a draw,
crowdfunding donations (donations) and referral sign-ups for giveaways. Uses the real services (sandbox
payments) and then back-dates timestamps. Run after ``seed_demo``.

    python manage.py seed_activity            # default volumes
    python manage.py seed_activity --customers 80 --bookings 120
"""
import random
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db.models import F
from django.utils import timezone

FIRST = ["Abebe", "Hana", "Mohammed", "Selam", "Yusuf", "Meron", "Kedir", "Tigist", "Ibrahim", "Liya", "Dawit", "Hiwot",
         "Ahmed", "Bethlehem", "Samuel", "Fatuma", "Yonas", "Rahel", "Nebil", "Saron", "Ermias", "Mekdes", "Abdi", "Lulit"]
LAST = ["Kebede", "Tesfaye", "Ali", "Girma", "Ahmed", "Alemu", "Hassen", "Bekele", "Omar", "Haile", "Mengistu", "Abdella",
        "Wolde", "Tadesse", "Mohammed", "Assefa", "Yimer", "Getachew"]


class Command(BaseCommand):
    help = "Simulate realistic demo activity (for charts and testing)."

    def add_arguments(self, parser):
        parser.add_argument("--customers", type=int, default=60)
        parser.add_argument("--bookings", type=int, default=70)
        parser.add_argument("--donations", type=int, default=400)
        parser.add_argument("--seed", type=int, default=7)

    def handle(self, *args, **o):
        from apps.accounts.models import User
        from apps.equb import services as equb
        from apps.equb.models import Contribution, EqubGroup, EqubMembership
        from apps.luckydraw import services as community
        from apps.luckydraw.models import CrowdfundingCampaign, Donation, Giveaway
        from apps.payments.models import PaymentTransaction
        from apps.payments.services import apply_status, initiate_payment
        from apps.travel.models import BaggageOption, Booking, FareClass, Flight, Seat, Ticket, TravelPackage
        from apps.travel.services import bookings as bsvc
        from apps.travel.services.seats import lock_seats

        if not Flight.objects.exists():
            raise CommandError("Run `python manage.py seed_demo` first.")
        rnd = random.Random(o["seed"])
        now = timezone.now()

        def past(max_days=30):
            return now - timedelta(days=rnd.randint(0, max_days - 1), hours=rnd.randint(0, 23), minutes=rnd.randint(0, 59))

        def pay(user, target, amount, purpose, when, ok=True):
            # Collected through the sandbox gateway, then labelled with a realistic provider for reporting.
            txn = initiate_payment(user=user, target=target, amount=amount, purpose=purpose, provider="mock")
            PaymentTransaction.objects.filter(pk=txn.pk).update(provider=rnd.choices(["telebirr", "chapa"], [4, 1])[0])
            txn = apply_status(txn.pk, "SUCCESS" if ok else rnd.choice(["FAILED", "CANCELLED"]))
            PaymentTransaction.objects.filter(pk=txn.pk).update(created_at=when, completed_at=when + timedelta(minutes=2))
            return txn

        # ---------------------------------------------------------- customers
        base = User.objects.filter(is_staff=False).count()
        customers = list(User.objects.filter(is_staff=False))
        for i in range(o["customers"]):
            phone = f"+25192{base + i:07d}"
            if User.objects.filter(phone=phone).exists():
                continue
            ref = rnd.choice(customers) if customers and rnd.random() < 0.35 else None
            u = User.objects.create_user(phone=phone, password="demo12345", referred_by=ref,
                                         full_name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}")
            User.objects.filter(pk=u.pk).update(date_joined=past(45))
            customers.append(u)
        self.stdout.write(f"customers: {len(customers)}")

        # ---------------------------------------------------------- flight bookings
        fares = list(FareClass.objects.filter(flight__departure_time__gt=now + timedelta(hours=6)).select_related("flight"))
        bags = list(BaggageOption.objects.filter(is_active=True))
        made = 0
        for _ in range(o["bookings"] * 3):
            if made >= o["bookings"]:
                break
            fare = rnd.choice(fares)
            user = rnd.choice(customers)
            n = rnd.choices([1, 2, 3], [6, 3, 1])[0]
            free = list(Seat.objects.filter(flight=fare.flight, cabin=fare.cabin, status="AVAILABLE")[:60])
            if len(free) < n or fare.seats_left < n:
                continue
            seats = rnd.sample(free, n)
            try:
                lock_seats(user, fare.flight, [s.id for s in seats])
                pax = [{"first_name": rnd.choice(FIRST), "last_name": user.full_name.split()[-1], "gender": rnd.choice("MF"),
                        "date_of_birth": f"19{rnd.randint(60, 99)}-0{rnd.randint(1, 9)}-1{rnd.randint(0, 9)}",
                        "passport_number": f"EP{rnd.randint(1000000, 9999999)}", "seat": s.id,
                        "baggage": [{"option": rnd.choice(bags).id, "quantity": 1}] if bags and rnd.random() < 0.3 else []}
                       for s in seats]
                b = bsvc.create_flight_booking(user=user, fare_class_id=fare.id, passengers=pax, contact_phone=user.phone)
            except Exception:  # noqa: BLE001 – inventory races in the simulation are fine to skip
                continue
            when = past(30)
            fate = rnd.random()
            if fate < 0.78:
                pay(user, b, b.total_amount, "BOOKING", when)
                Booking.objects.filter(pk=b.pk).update(created_at=when, confirmed_at=when + timedelta(minutes=3))
                Ticket.objects.filter(booking=b).update(created_at=when + timedelta(minutes=3))
                if fare.refundable and rnd.random() < 0.15:
                    bsvc.cancel_booking(Booking.objects.get(pk=b.pk), "Customer changed plans")
            elif fate < 0.9:
                pay(user, b, b.total_amount, "BOOKING", when, ok=False)
                Booking.objects.filter(pk=b.pk).update(created_at=when, expires_at=now - timedelta(minutes=1))
            else:
                Booking.objects.filter(pk=b.pk).update(created_at=now - timedelta(minutes=rnd.randint(1, 20)))
            made += 1
        bsvc.expire_unpaid_bookings()
        self.stdout.write(f"flight bookings: {made}")

        # ---------------------------------------------------------- travel desk (office payments, check-in, refunds)
        from apps.accounts.models import User as _Staff
        from apps.travel.services import operations as tops

        desk = _Staff.objects.filter(is_staff=True).order_by("-is_superuser").first()
        if desk:
            for b in [b for b in Booking.objects.filter(status="PENDING_PAYMENT") if b.is_payable][:2]:
                tops.record_office_payment(b, desk, method=rnd.choice(["cash", "bank"]), reference=f"CBE{rnd.randint(10**6, 10**7)}",
                                           note="Paid at the Bole office")
            soon = Booking.objects.filter(status="CONFIRMED", flight__departure_time__gt=now,
                                          flight__departure_time__lte=now + timedelta(hours=36))
            for b in soon[:4]:
                for t in list(b.tickets.all())[: rnd.randint(1, 2)]:
                    try:
                        tops.check_in(t, desk)
                    except Exception:  # noqa: BLE001
                        pass
            for b in list(tops.refunds_due())[1:]:  # leave one in the refund queue for the demo
                tops.refund_booking(b, desk, amount=tops.refundable_amount(b), method="telebirr",
                                    reference=f"TB{rnd.randint(10**7, 10**8)}", note="Customer changed plans",
                                    notify_customer=False)

        # ---------------------------------------------------------- package bookings
        for pkg in TravelPackage.objects.filter(is_active=True):
            for _ in range(rnd.randint(2, 6)):
                if pkg.seats_left < 2:
                    break
                user = rnd.choice(customers)
                try:
                    b = bsvc.create_package_booking(user=user, package_id=pkg.id, contact_phone=user.phone, passengers=[
                        {"first_name": user.full_name.split()[0], "last_name": user.full_name.split()[-1], "gender": "M",
                         "date_of_birth": "1985-05-05", "passport_number": f"EP{rnd.randint(1000000, 9999999)}"}])
                except Exception:  # noqa: BLE001
                    continue
                when = past(30)
                pay(user, b, b.total_amount, "BOOKING", when)
                Booking.objects.filter(pk=b.pk).update(created_at=when, confirmed_at=when)
                pkg.refresh_from_db()

        # Bookings were back-dated above – move their audit trail to the same moments.
        from apps.travel.models import BookingEvent

        for b in Booking.objects.all():
            BookingEvent.objects.filter(booking=b, kind="CREATED").update(created_at=b.created_at)
            if b.confirmed_at:
                BookingEvent.objects.filter(booking=b, kind="CONFIRMED").update(created_at=b.confirmed_at)
            BookingEvent.objects.filter(booking=b, kind="PAYMENT", message__contains="failed").update(
                created_at=b.created_at + timedelta(minutes=2))

        # ---------------------------------------------------------- equb
        for g in EqubGroup.objects.filter(status="OPEN"):
            pool = [c for c in customers if not g.memberships.filter(user=c).exists()]
            rnd.shuffle(pool)
            for u in pool:
                g.refresh_from_db()
                if g.status != "OPEN" or g.members_count >= g.max_members:
                    break
                try:
                    equb.join_group(u, g)
                except Exception:  # noqa: BLE001
                    break
            g.refresh_from_db()
            if g.status == "OPEN":
                equb.start_group(g)
        drew = 0
        for g in EqubGroup.objects.filter(status="ACTIVE"):
            rnd_obj = g.rounds.filter(status="COLLECTING").first()
            if not rnd_obj:
                continue
            for c in Contribution.objects.filter(round=rnd_obj, status="PENDING").select_related("membership__user"):
                if rnd.random() < (0.85 if drew == 0 else 0.6):  # leave some members behind for collections
                    when = now - timedelta(days=rnd.randint(0, 6), hours=rnd.randint(0, 20))
                    pay(c.membership.user, c, c.amount_due, "EQUB_CONTRIBUTION", when)
                    Contribution.objects.filter(pk=c.pk).update(paid_at=when)
            if drew == 0:  # demonstrate one completed draw
                equb.run_draw(rnd_obj)
                drew += 1
        self.stdout.write(f"equb: {EqubMembership.objects.count()} memberships, {drew} draw(s) run")

        # ---------------------------------------------------------- equb collections (for the call centre & reports)
        from apps.accounts.models import User as _User
        from apps.equb import operations as ops
        from apps.equb.models import CallLog, EqubRound

        staff = _User.objects.filter(is_staff=True).order_by("-is_superuser").first()
        groups = list(EqubGroup.objects.filter(status="ACTIVE").order_by("id"))
        late_group = next((g for g in groups if g.rounds.filter(status="DRAWN").count() == 0), None)
        if late_group:  # pretend this pool's first round fell due 12 days ago, so unpaid members are overdue
            r1 = late_group.rounds.get(number=1)
            EqubRound.objects.filter(pk=r1.pk).update(due_date=timezone.localdate() - timedelta(days=12),
                                                      draw_date=timezone.localdate() + timedelta(days=2))
            equb.sweep_late_contributions()
        office = 0
        for g in groups:
            for m in g.memberships.filter(status__in=["ACTIVE", "WON"]).select_related("user", "group")[:3]:
                plan = equb.payment_plan(m)
                if not plan["options"] or rnd.random() < 0.3:
                    continue
                rounds_ = min(len(plan["options"]), rnd.choice([1, 2, 3]))
                try:
                    equb.record_office_payment(m, staff, method=rnd.choice(["cash", "bank"]),
                                               rounds=plan["options"][rounds_ - 1]["rounds"],
                                               reference=f"FT{rnd.randint(100000, 999999)}", notify_member=False)
                    office += 1
                except Exception:  # noqa: BLE001
                    continue
        outcomes = ["PROMISED", "NO_ANSWER", "CALL_BACK", "UNREACHABLE", "HARDSHIP"]
        calls = 0
        from apps.equb import standing as _st
        members_ = list(_st.live_memberships())
        standings_ = _st.standings_for(members_)
        for m in members_:
            if standings_[m.id].arrears > 0 and rnd.random() < 0.7 and staff:
                outcome = rnd.choice(outcomes)
                ops.log_call(m, staff, outcome=outcome, feedback=rnd.choice(["Will pay after salary", "Phone was off",
                                                                             "Asked to call back tomorrow", "Travelling"]),
                             promised_date=timezone.localdate() + timedelta(days=rnd.randint(1, 7)) if outcome == "PROMISED" else None)
                calls += 1
        ops.escalate_standings()
        self.stdout.write(f"equb collections: {office} office payment(s), {calls} call(s) logged, "
                          f"{CallLog.objects.count()} call log rows")

        # ---------------------------------------------------------- community finance
        # Crowdfunding: voluntary gifts, the 5% platform fee on top.
        CrowdfundingCampaign.objects.filter(status="LIVE", donations_count=0).update(starts_at=now - timedelta(days=14))
        camps = list(CrowdfundingCampaign.objects.live())
        amounts = [50, 100, 100, 200, 250, 500, 500, 1000, 1000, 2000, 5000]
        for _ in range(o["donations"] if camps else 0):
            c = rnd.choice(camps)
            user = rnd.choice(customers)
            try:
                donation, txn = community.start_donation(user, c, rnd.choice(amounts), kind="DONATION",
                                                         provider="mock", is_anonymous=rnd.random() < 0.15)
            except Exception:  # noqa: BLE001 – e.g. the goal was reached
                continue
            when = c.starts_at + (now - c.starts_at) * rnd.random()
            PaymentTransaction.objects.filter(pk=txn.pk).update(provider=rnd.choices(["telebirr", "chapa"], [5, 1])[0])
            apply_status(txn.pk, "SUCCESS")
            PaymentTransaction.objects.filter(pk=txn.pk).update(created_at=when, completed_at=when)
            Donation.objects.filter(pk=donation.pk).update(created_at=when, paid_at=when)
        # Giveaways: friends who registered with a referral code during the giveaway earn their inviter free entries.
        Giveaway.objects.filter(status="LIVE", entries_issued=0).update(starts_at=now - timedelta(days=45))
        for g in Giveaway.objects.live():
            community.recount(g)
        self.stdout.write(f"community: {Donation.objects.filter(status='PAID').count()} paid donations, "
                          f"{sum(g.entries_issued for g in Giveaway.objects.all())} giveaway entries")
        self.stdout.write(self.style.SUCCESS("Demo activity generated. Open http://127.0.0.1:8000/console/"))
