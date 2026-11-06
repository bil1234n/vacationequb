from datetime import timedelta
from io import StringIO
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.core.testutils import make_equb, client_for, make_flight, make_package, make_user, passenger
from apps.equb.models import EqubGroup
from apps.equb.services import join_group, start_group
from apps.luckydraw.models import CrowdfundingCampaign, Giveaway


class ConsoleTests(TestCase):
    def setUp(self):
        self.staff = make_user("vacation Admin", staff=True)
        self.staff.set_password("pass12345")
        self.staff.save()
        self.customer = make_user("Abebe Kebede")
        self.flight, self.eco, _ = make_flight()
        self.pkg = make_package()
        api = client_for(self.customer)
        r = api.post("/api/travel/bookings/", {"package": self.pkg.id, "contact_phone": self.customer.phone,
                                               "passengers": [passenger()]}, format="json")
        self.ref = r.data["reference"]
        self.group = make_equb(name="Umrah Equb", destination="UMRAH", contribution_amount=Decimal("1000"),
                                              max_members=3, min_members_to_start=2, start_date=timezone.localdate())
        now = timezone.now()
        self.camp = Giveaway.objects.create(title="Umrah Giveaway", prize_type="TRAVEL_CREDIT", prize_title="Credit",
                                                     prize_value=Decimal("1000"), starts_at=now - timedelta(days=1),
                                                     ends_at=now + timedelta(days=1), draw_at=now + timedelta(days=1))

    def login(self):
        r = self.client.post(reverse("console:login"), {"phone": self.staff.phone, "password": "pass12345"})
        self.assertRedirects(r, reverse("console:dashboard"))

    def test_requires_login_and_staff(self):
        r = self.client.get(reverse("console:dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/console/login/", r["Location"])
        self.customer.set_password("pass12345")
        self.customer.save()
        r = self.client.post(reverse("console:login"), {"phone": self.customer.phone, "password": "pass12345"})
        self.assertContains(r, "does not have staff access")
        r = self.client.post(reverse("console:login"), {"phone": self.staff.phone, "password": "wrong"})
        self.assertContains(r, "Incorrect phone number or password")

    def test_every_page_renders(self):
        self.login()
        urls = [
            reverse("console:dashboard"), reverse("console:dashboard") + "?days=7", reverse("console:bookings") + "?q=Kebede",
            reverse("console:booking", args=[self.ref]), reverse("console:flights"), reverse("console:flights") + "?when=past",
            reverse("console:flight", args=[self.flight.pk]), reverse("console:packages"), reverse("console:equbs"),
            reverse("console:equb", args=[self.group.slug]), reverse("console:community"), reverse("console:giveaways"),
            reverse("console:giveaway", args=[self.camp.slug]), reverse("console:payments") + "?status=SUCCESS",
            reverse("console:users"), reverse("console:user", args=[self.customer.pk]), reverse("console:profile"),
        ]
        for url in urls:
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        self.assertContains(self.client.get(reverse("console:dashboard")), 'id="chart-config"')

    def test_actions_and_profile_and_logout(self):
        self.login()
        for u in (self.customer, make_user("B")):
            join_group(u, self.group)
        r = self.client.post(reverse("console:equb", args=[self.group.slug]), {"action": "start"}, follow=True)
        self.assertContains(r, "Equb started")
        r = self.client.post(reverse("console:booking", args=[self.ref]), {"action": "cancel"}, follow=True)
        self.assertContains(r, "cancelled")
        r = self.client.post(reverse("console:giveaway", args=[self.camp.slug]), {"action": "draw"}, follow=True)
        self.assertContains(r, "Nobody has earned an entry yet")
        r = self.client.post(reverse("console:profile"), {"form": "profile", "full_name": "Bilal Admin", "email": "b@vacation.et"}, follow=True)
        self.staff.refresh_from_db()
        self.assertEqual(self.staff.full_name, "Bilal Admin")
        r = self.client.post(reverse("console:profile"), {"form": "password", "old_password": "pass12345",
                                                          "new_password1": "N3w-strong-pass", "new_password2": "N3w-strong-pass"}, follow=True)
        self.assertContains(r, "Password changed")
        r = self.client.post(reverse("console:logout"), follow=True)
        self.assertContains(r, "signed out")
        self.assertEqual(self.client.get(reverse("console:dashboard")).status_code, 302)

    def test_landing_page(self):
        r = self.client.get("/")
        self.assertContains(r, "Staff console")


class ConsoleManageTests(TestCase):
    """Create/edit screens, dashboard KPIs and the reset_data command."""

    def setUp(self):
        from apps.travel.models import AircraftType, Airport
        from apps.core.testutils import LAYOUT

        self.staff = make_user("Admin", staff=True, is_superuser=True)
        self.client.force_login(self.staff)
        self.add = Airport.objects.create(iata="ADD", name="Bole", city="Addis Ababa", country="Ethiopia")
        self.dxb = Airport.objects.create(iata="DXB", name="DXB", city="Dubai", country="UAE")
        self.ac = AircraftType.objects.create(code="T1", name="Test 737", layout=LAYOUT)  # 8 business + 30 economy

    def flight_post(self, **over):
        data = {"airline_code": "ET", "airline_name": "Ethiopian Airlines", "flight_number": "ET600", "aircraft": self.ac.pk,
                "status": "SCHEDULED", "origin": self.add.pk, "destination": self.dxb.pk, "departure_time": "2030-01-10T09:00",
                "arrival_time": "2030-01-10T13:00", "is_international": "on", "terminal": "", "gate": "",
                "fares-TOTAL_FORMS": "2", "fares-INITIAL_FORMS": "0", "fares-MIN_NUM_FORMS": "0", "fares-MAX_NUM_FORMS": "1000",
                "fares-0-cabin": "ECONOMY", "fares-0-code": "K", "fares-0-name": "Saver", "fares-0-base_fare": "18000",
                "fares-0-taxes": "3000", "fares-0-seats_allocated": "30", "fares-0-checked_baggage_kg": "23",
                "fares-0-cabin_baggage_kg": "7", "fares-0-change_fee": "0",
                "fares-1-cabin": "BUSINESS", "fares-1-code": "J", "fares-1-name": "Business", "fares-1-base_fare": "50000",
                "fares-1-taxes": "3000", "fares-1-seats_allocated": "8", "fares-1-checked_baggage_kg": "64",
                "fares-1-cabin_baggage_kg": "14", "fares-1-change_fee": "0"}
        data.update(over)
        return self.client.post(reverse("console:flight_add"), data)

    def test_create_flight_generates_seats_and_validates_capacity(self):
        from apps.travel.models import Flight

        r = self.flight_post(**{"fares-0-seats_allocated": "31"})
        self.assertContains(r, "Economy fares allocate 31 seats but the aircraft has 30")
        r = self.flight_post(arrival_time="2030-01-10T08:00")
        self.assertContains(r, "Arrival must be after departure")
        r = self.flight_post()
        f = Flight.objects.get()
        self.assertRedirects(r, reverse("console:flight", args=[f.pk]))
        self.assertEqual(f.seats.count(), 38)
        self.assertEqual(f.fares.count(), 2)
        self.assertEqual(self.client.get(reverse("console:flight_edit", args=[f.pk])).status_code, 200)

    def test_package_equb_campaign_forms(self):
        from apps.travel.models import TravelPackage

        r = self.client.post(reverse("console:package_add"), {
            "name": "Umrah 15 days", "category": "UMRAH", "summary": "s", "destination_city": "Makkah",
            "destination_country": "Saudi Arabia", "departure_date": "2030-02-01", "return_date": "2030-02-16",
            "duration_days": "15", "price": "185000", "deposit_amount": "0", "capacity": "30",
            "includes": "Visa\n• Flight\n\nHotel", "is_active": "on"})
        self.assertRedirects(r, reverse("console:packages"))
        pkg = TravelPackage.objects.get()
        self.assertEqual(pkg.includes, ["Visa", "Flight", "Hotel"])

        from apps.equb.models import EqubPackage

        equb_pkg = EqubPackage.objects.create(name="Umrah Equb Package")
        # Any frequency, duration and number of members – no "at least 2 contributions" rule.
        r = self.client.post(reverse("console:equb_add"), {
            "equb_package": equb_pkg.pk, "name": "Umrah Equb", "contribution_amount": "15500", "frequency_days": "90",
            "max_members": "1000", "duration_value": "3", "duration_unit": "MONTHS", "start_date": "2030-01-01",
            "status": "OPEN", "draw_type": "MANUAL", "rules_text": "Travel within a year"})
        self.assertRedirects(r, reverse("console:equb", args=["umrah-equb"]))
        g = EqubGroup.objects.get()
        self.assertEqual(g.organizer, self.staff)
        self.assertEqual((g.max_members, g.frequency_days, str(g.end_date), g.draw_type, g.total_rounds),
                         (1000, 90, "2030-04-01", "MANUAL", 1000))
        self.assertEqual(g.price_per_person, Decimal("46500"))  # 15,500 × 3
        # 12 members over 3 rounds drawn → 4 winners in each draw
        r = self.client.post(reverse("console:equb_add"), {
            "equb_package": equb_pkg.pk, "name": "Umrah Raha", "contribution_amount": "70000", "frequency_days": "90",
            "max_members": "12", "rounds_drawn": "3", "duration_value": "3", "duration_unit": "MONTHS",
            "start_date": "2030-01-01", "status": "OPEN", "draw_type": "MANUAL"})
        raha = EqubGroup.objects.get(name="Umrah Raha")
        self.assertEqual((raha.price_per_person, raha.winners_per_round, raha.total_rounds), (Decimal("210000"), 4, 3))
        r = self.client.post(reverse("console:equb_add"), {
            "equb_package": equb_pkg.pk, "name": "Bad", "contribution_amount": "100", "frequency_days": "7",
            "max_members": "3", "rounds_drawn": "5", "duration_value": "1", "duration_unit": "MONTHS",
            "start_date": "2030-01-01", "status": "OPEN", "draw_type": "MANUAL"})
        self.assertContains(r, "more draws than members")

        base = {"title": "Umrah Giveaway", "prize_type": "TRAVEL_PACKAGE", "prize_title": "Umrah", "prize_value": "160000",
                "number_of_winners": "1", "referrals_per_entry": "10", "sponsor_type": "COMPANY", "sponsor_name": "vacation Travel",
                "starts_at": "2030-01-01T10:00", "ends_at": "2030-01-10T10:00", "draw_at": "2030-01-10T11:00", "status": "LIVE"}
        r = self.client.post(reverse("console:giveaway_add"), base)
        self.assertContains(r, "Choose the package the winner travels on")
        r = self.client.post(reverse("console:giveaway_add"), {**base, "prize_package": pkg.pk})
        self.assertRedirects(r, reverse("console:giveaway", args=["umrah-giveaway"]))
        r = self.client.post(reverse("console:campaign_add"), {
            "title": "Help Ummi Fatuma", "category": "UMRAH", "beneficiary_name": "Fatuma Ali", "goal_amount": "185000",
            "platform_fee_percent": "5", "min_donation": "10", "starts_at": "2026-01-01T10:00", "status": "LIVE"})
        self.assertRedirects(r, reverse("console:campaign", args=["help-ummi-fatuma"]))
        self.assertContains(self.client.get(reverse("console:community")), "+ New Crowdfunding Campaign")

    def test_settings_and_staff(self):
        self.assertEqual(self.client.get(reverse("console:settings")).status_code, 200)
        r = self.client.post(reverse("console:staff_add"), {"full_name": "Sara Ops", "phone": "0911000111", "password": "S3cure-pass"})
        self.assertRedirects(r, reverse("console:settings"))
        from apps.accounts.models import User
        self.assertTrue(User.objects.get(phone="+251911000111").is_staff)
        r = self.client.post(reverse("console:baggage_add"), {"code": "B10", "name": "Extra 10kg", "weight_kg": "10", "price": "1800", "is_active": "on"})
        self.assertRedirects(r, reverse("console:settings"))

    def test_dashboard_customers_and_profit(self):
        from apps.payments.services import apply_status, initiate_payment

        cust = make_user("Customer")
        pkg = make_package(price=100000)
        api = client_for(cust)
        ref = api.post("/api/travel/bookings/", {"package": pkg.id, "contact_phone": cust.phone, "passengers": [passenger()]},
                       format="json").data["reference"]
        from apps.travel.models import Booking
        txn = initiate_payment(user=cust, target=Booking.objects.get(reference=ref), amount=Decimal("100000"), purpose="BOOKING", provider="mock")
        apply_status(txn.pk, "SUCCESS")
        r = self.client.get(reverse("console:dashboard"))
        kpis = {k["label"]: k for k in r.context["kpis"]}
        self.assertEqual(kpis["Customers"]["value"], "1")
        self.assertEqual(kpis["Company profit (5%)"]["value"], Decimal("5000.00"))

    def test_reset_data(self):
        from django.core.management import call_command
        from apps.accounts.models import User
        from apps.travel.models import Airport, Flight

        self.flight_post()
        make_user("Customer")
        call_command("reset_data", yes=True, stdout=StringIO())
        self.assertFalse(User.objects.filter(is_staff=False).exists())
        self.assertTrue(User.objects.filter(pk=self.staff.pk).exists())
        self.assertEqual(Flight.objects.count(), 0)
        self.assertEqual(Airport.objects.count(), 2)


class ConsoleEqubOperationsTests(TestCase):
    """Collections, call centre, terminations, reports, payouts and the member page."""

    def setUp(self):
        from apps.equb.models import EqubRound

        self.staff = make_user("Ops", staff=True, is_superuser=True)
        self.client.force_login(self.staff)
        self.group = make_equb(name="Dubai Weekly", destination="DUBAI", contribution_amount=Decimal("1000"),
                                              frequency="WEEKLY", max_members=3, min_members_to_start=2, late_fee=Decimal("50"),
                                              grace_days=1, start_date=timezone.localdate())
        self.users = [make_user(f"Member {i}") for i in range(3)]
        for u in self.users:
            join_group(u, self.group)
        self.group.refresh_from_db()
        r1 = self.group.rounds.get(number=1)
        EqubRound.objects.filter(pk=r1.pk).update(due_date=timezone.localdate() - timedelta(days=20))
        from apps.equb.models import EqubMembership
        self.m = [EqubMembership.objects.get(group=self.group, user=u) for u in self.users]

    def test_pages_render_and_export(self):
        from apps.equb import operations as ops

        ops.log_call(self.m[0], self.staff, outcome="NO_ANSWER")
        ops.request_termination(self.m[1], self.staff, note="test")
        ops.request_exit(self.users[2], self.m[2], "Moving abroad")
        urls = [reverse("console:equbs"), reverse("console:equb_collections"), reverse("console:call_center"),
                reverse("console:call_center") + "?tab=called", reverse("console:terminations"),
                reverse("console:terminations") + "?tab=exits", reverse("console:terminations") + "?tab=candidates",
                reverse("console:terminations") + "?tab=terminated", reverse("console:equb_draws"),
                reverse("console:equb_draws") + "?tab=history", reverse("console:equb_draws") + "?tab=payouts",
                reverse("console:equb_draws") + "?tab=history&export=csv", reverse("console:equb_draw_settings"),
                reverse("console:group_equbs"), reverse("console:equb_rules"),
                reverse("console:equb_member", args=[self.m[0].pk]),
                reverse("console:equb_round", args=[self.group.slug, 1])]
        urls += [reverse("console:equb", args=[self.group.slug]) + f"?tab={t}" for t in ("overview", "members", "payments", "draws")]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        r = self.client.get(reverse("console:call_center"))
        self.assertEqual(r.context["counts"]["behind"], 3)
        self.assertContains(self.client.get(reverse("console:equb_collections")), "Never paid")
        csv = self.client.get(reverse("console:equb_collections") + "?export=csv")
        self.assertEqual(csv["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("Member 0", csv.content.decode("utf-8-sig"))

    def test_member_page_actions(self):
        from apps.equb.models import Contribution, EqubPayment, Termination

        url = reverse("console:equb_member", args=[self.m[0].pk])
        r = self.client.post(url, {"action": "record_payment", "method": "cash", "rounds": "1", "notify": "1"}, follow=True)
        self.assertContains(r, "paid round 1")
        rec = EqubPayment.objects.get(membership=self.m[0])
        self.assertEqual(rec.amount, Decimal("1050.00"))
        self.assertEqual(Contribution.objects.get(membership=self.m[0]).status, "PAID")
        r = self.client.post(url, {"action": "log_call", "outcome": "PROMISED", "promised_date": "", "feedback": "x"}, follow=True)
        self.assertContains(r, "Enter the date the member promised to pay")
        url1 = reverse("console:equb_member", args=[self.m[1].pk])
        self.client.post(url1, {"action": "terminate", "reason": "NON_PAYMENT", "note": "No answer for weeks"})
        self.m[1].refresh_from_db()
        self.assertEqual(self.m[1].status, "DEFAULTED")
        self.assertEqual(Termination.objects.get().arrears_at_termination, Decimal("1050.00"))
        self.client.post(url1, {"action": "reinstate"})
        self.m[1].refresh_from_db()
        self.assertEqual(self.m[1].status, "ACTIVE")
        r = self.client.get(reverse("console:equb_round", args=[self.group.slug, 1]))
        self.assertEqual(r.context["pool"]["eligible_count"], 1)
        r = self.client.post(reverse("console:equb_round", args=[self.group.slug, 1]), {"action": "draw"}, follow=True)
        self.assertContains(r, "Winner drawn: Member 0")


class ConsoleTravelOperationsTests(TestCase):
    """Bookings work queues, the booking page actions, the check-in desk and the flight manifest."""

    def setUp(self):
        self.staff = make_user("Desk", staff=True)
        self.client.force_login(self.staff)
        self.customer = make_user("Hana Tesfaye")
        self.flight, self.eco, self.bus = make_flight()
        api = client_for(self.customer)
        seats = [self.flight.seats.get(number=n) for n in ("10A", "10B")]
        api.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [s.id for s in seats]}, format="json")
        r = api.post("/api/travel/bookings/", {"fare_class": self.eco.id, "contact_phone": self.customer.phone, "passengers": [
            passenger(seat=s.id, first_name=f"P{i}") for i, s in enumerate(seats)]}, format="json")
        self.ref = r.data["reference"]
        self.url = reverse("console:booking", args=[self.ref])

    def test_booking_page_payment_desk_refund(self):
        from apps.travel.models import Booking

        self.assertEqual(self.client.get(reverse("console:bookings") + "?queue=pending").context["page"].paginator.count, 1)
        r = self.client.post(self.url, {"action": "record_payment", "method": "bank", "reference": ""}, follow=True)
        self.assertContains(r, "Enter the transfer reference")
        r = self.client.post(self.url, {"action": "record_payment", "method": "cash", "note": "Receipt 12"}, follow=True)
        self.assertContains(r, "is confirmed and tickets are issued")
        b = Booking.objects.get(reference=self.ref)
        self.assertEqual(b.status, "CONFIRMED")
        self.client.post(self.url, {"action": "note", "note": "Customer asked for a window seat"})
        self.assertContains(self.client.get(self.url), "Customer asked for a window seat")

        # desk: find by reference, check everyone in, board one
        desk = reverse("console:check_in")
        r = self.client.get(desk, {"q": self.ref})
        self.assertEqual(r.context["booking"], b)
        self.client.post(desk, {"action": "check_in_all", "booking": b.pk, "q": self.ref})
        self.assertEqual(b.tickets.filter(status="CHECKED_IN").count(), 2)
        t = b.tickets.first()
        self.client.post(desk, {"action": "board", "ticket": t.pk, "q": self.ref})
        t.refresh_from_db()
        self.assertEqual(t.status, "BOARDED")
        r = self.client.get(desk, {"q": t.qr_payload})
        self.assertEqual(r.context["ticket"], t)

        manifest = reverse("console:flight_manifest", args=[self.flight.pk])
        r = self.client.get(manifest)
        self.assertEqual(r.context["total"], 2)
        self.assertEqual(r.context["statuses"]["BOARDED"], 1)
        csv = self.client.get(manifest + "?export=csv")
        self.assertIn("KEBEDE", csv.content.decode("utf-8-sig"))

        # staff cancel → refund due → refund
        r = self.client.post(self.url, {"action": "cancel", "reason": "Customer called", "notify": "1"}, follow=True)
        self.assertContains(r, "cancelled and inventory released")
        self.assertEqual(self.client.get(reverse("console:bookings") + "?queue=refund_due").context["page"].paginator.count, 1)
        r = self.client.post(self.url, {"action": "refund", "amount": "999999999", "method": "Cash"}, follow=True)
        self.assertContains(r, "was paid and not yet refunded")
        r = self.client.post(self.url, {"action": "refund", "amount": str(b.total_amount), "method": "Cash"}, follow=True)
        self.assertContains(r, "Refund of")
        b.refresh_from_db()
        self.assertEqual(b.status, "REFUNDED")
        for q in ("all", "pending", "departing", "refund_due", "paid_after_expiry"):
            self.assertEqual(self.client.get(reverse("console:bookings") + f"?queue={q}").status_code, 200, q)
        export = self.client.get(reverse("console:bookings") + "?export=csv")
        self.assertIn(self.ref, export.content.decode("utf-8-sig"))
        self.assertEqual(self.client.get(reverse("console:flight", args=[self.flight.pk])).status_code, 200)


class ConsoleEqubPackageTests(TestCase):
    """Equb package → Equb group → Equb membership, the NiyaEqubBank way."""

    def setUp(self):
        self.staff = make_user("Ops", staff=True)
        self.client.force_login(self.staff)

    def test_package_group_membership_flow(self):
        from apps.equb.models import EqubGroup, EqubMembership, EqubPackage

        # the package form is exactly NiyaEqubBank's: name, frequency (days), terms, active
        r = self.client.post(reverse("console:equb_package_add"), {
            "name": "Umrah Savers", "is_active": "on", "terms_content": "Travel within a year"})
        pkg = EqubPackage.objects.get(name="Umrah Savers")
        self.assertRedirects(r, reverse("console:equb_package", args=[pkg.slug]))

        # the new-group form starts from the package
        r = self.client.get(reverse("console:equb_add") + f"?package={pkg.pk}")
        self.assertEqual(r.context["form"].initial["rules_text"], "Travel within a year")
        self.assertNotContains(r, "More settings")
        r = self.client.post(reverse("console:equb_add"), {
            "equb_package": pkg.pk, "name": "Umrah Savers – Group 1", "contribution_amount": "5000",
            "frequency_days": "30", "max_members": "10", "duration_value": "10", "duration_unit": "MONTHS",
            "start_date": timezone.localdate().isoformat(), "draw_type": "AUTOMATIC", "status": "OPEN"})
        g = EqubGroup.objects.get(name="Umrah Savers – Group 1")
        self.assertEqual((g.equb_package, g.max_members), (pkg, 10))
        # a group must come from a package
        r = self.client.post(reverse("console:equb_add"), {"name": "No package"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.context["form"].errors.get("equb_package"))

        customer = make_user("Amina Yusuf")
        r = self.client.post(reverse("console:equb_membership_add"), {"group": g.pk})
        self.assertContains(r, "Choose at least one customer")
        r = self.client.post(reverse("console:equb_membership_add"), {"group": g.pk, "new_phone": "0999999999"})
        self.assertContains(r, "Enter the new customer")
        r = self.client.post(reverse("console:equb_membership_add"), {"group": g.pk, "customers": [customer.pk]}, follow=True)
        self.assertContains(r, "joined Umrah Savers – Group 1")
        self.assertTrue(EqubMembership.objects.filter(group=g, user=customer).exists())
        # register a brand-new customer and add them in one step
        r = self.client.post(reverse("console:equb_membership_add"),
                             {"group": g.pk, "new_name": "Hawa Ali", "new_phone": "0977000111"}, follow=True)
        self.assertContains(r, "Hawa Ali (#2) joined")

        for url in [reverse("console:equb_packages"), reverse("console:equb_package", args=[pkg.slug]),
                    reverse("console:equb_memberships"), reverse("console:equb_memberships") + f"?package={pkg.slug}",
                    reverse("console:equbs") + f"?package={pkg.slug}", reverse("console:equb", args=[g.slug]),
                    reverse("console:equb_package_edit", args=[pkg.slug])]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(reverse("console:equb_memberships")), "Amina Yusuf")
        csv = self.client.get(reverse("console:equb_memberships") + "?export=csv")
        self.assertIn("Umrah Savers", csv.content.decode("utf-8-sig"))
        # a package with groups can't be deleted
        self.client.post(reverse("console:equb_package_delete", args=[pkg.slug]))
        self.assertTrue(EqubPackage.objects.filter(pk=pkg.pk).exists())

        api = client_for(customer).get("/api/equb/packages/")
        row = next(p for p in api.data if p["slug"] == pkg.slug)
        self.assertEqual((row["open_groups"], row["summary"]["durations"]), (1, ["10 months"]))

        # Group Equb created by staff: owner + member + a responsibility person, then a draw within the group
        from apps.equb.models import GroupEqub

        owner, friend = make_user("Group Owner"), make_user("Group Friend")
        r = self.client.post(reverse("console:group_equb_add"), {
            "equb": g.pk, "name": "Owner Family", "kind": "FAMILY", "owner": owner.pk, "members": [friend.pk],
            "description": "", "people-TOTAL_FORMS": "3", "people-INITIAL_FORMS": "0",
            "people-0-name": "Little One", "people-0-phone": ""})  # rows 1–2 removed on the page
        team = GroupEqub.objects.get(name="Owner Family")
        self.assertRedirects(r, reverse("console:group_equb", args=[team.pk]))
        places = list(team.live_memberships.order_by("position"))
        self.assertEqual([(m.user_id, m.dependent_name) for m in places],
                         [(owner.pk, ""), (friend.pk, ""), (owner.pk, "Little One")])
        for url in [reverse("console:group_equbs"), reverse("console:group_equb", args=[team.pk]),
                    reverse("console:group_equb_add")]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        self.assertContains(self.client.get(reverse("console:group_equbs")), "Owner Family")
        groups = client_for(customer).get("/api/equb/groups/", {"package": pkg.slug})
        self.assertEqual(groups.data["results"][0]["equb_package"]["slug"], pkg.slug)


class ConsoleDrawNowAndRunningEditTests(TestCase):
    """Start with no members, edit a running Equb, and the Draw now page (automatic / manual, member / Group Equb)."""

    def setUp(self):
        from apps.equb import services, teams
        self.services = services
        self.staff = make_user("Desk", staff=True)
        self.client.force_login(self.staff)
        self.g = make_equb(name="Family Pot", contribution_amount=Decimal("1000"), frequency="MONTHLY", max_members=6,
                           start_date=timezone.localdate())
        self.a, self.b, self.c, self.d = (make_user(n) for n in ("Abel One", "Bina Two", "Caleb Three", "Dina Four"))
        self.team = teams.create_group_equb(self.a, self.g, name="Abel Family", kind="FAMILY", members=[self.b],
                                            created_by=self.staff)
        for u in (self.c, self.d):
            services.join_group(u, self.g)
        services.start_group(self.g)
        self.rnd = self.g.rounds.get(number=1)

    def test_start_with_zero_members_and_join_late(self):
        e = make_equb(name="Empty Start", contribution_amount=Decimal("500"), frequency="MONTHLY", max_members=4, start_date=timezone.localdate())
        self.services.start_group(e)
        e.refresh_from_db()
        self.assertEqual(e.status, "ACTIVE")
        self.assertEqual(e.rounds.count(), 4)
        m = self.services.join_group(make_user("Late Joiner"), e)
        self.assertTrue(m.contributions.filter(round__number=1).exists())

    def test_edit_running_equb_is_not_locked(self):
        r = self.client.get(reverse("console:equb_edit", args=[self.g.slug]))
        self.assertNotContains(r, "Locked while the Equb is running")
        data = {k: v for k, v in r.context["form"].initial.items() if v is not None}
        data.update(equb_package=self.g.equb_package_id or "", name=self.g.name, contribution_amount="1500",
                    frequency_days="30", max_members="6", duration_value="6", duration_unit="MONTHS",
                    start_date=self.g.start_date.isoformat(), status="ACTIVE", draw_type="MANUAL", rules_text="")
        if not data["equb_package"]:
            from apps.equb.models import EqubPackage
            data["equb_package"] = EqubPackage.objects.create(name="Pkg").pk
        r = self.client.post(reverse("console:equb_edit", args=[self.g.slug]), data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        self.assertEqual(set(self.rnd.contributions.values_list("amount", flat=True)), {Decimal("1500")})

    def test_draw_page_renders(self):
        r = self.client.get(reverse("console:equb_draw_now") + f"?round={self.rnd.pk}")
        self.assertContains(r, "How do you want to draw?")
        self.assertContains(r, "Abel Family")

    def test_manual_member_and_whole_group(self):
        url = reverse("console:equb_draw_now")
        cm = self.g.memberships.get(user=self.c)
        r = self.client.post(url, {"method": "manual", "round": self.rnd.pk, "scope": "member", "winners": [cm.pk]})
        self.assertContains(self.client.get(url), "")  # page still fine
        cm.refresh_from_db()
        self.assertFalse(cm.has_won)  # not paid up → a reason is required
        r = self.client.post(url, {"method": "manual", "round": self.rnd.pk, "scope": "member", "winners": [cm.pk],
                                   "note": "Agreed at the meeting"})
        cm.refresh_from_db()
        self.assertTrue(cm.has_won)
        rnd2 = self.g.rounds.get(number=2)
        r = self.client.post(url, {"method": "manual", "round": rnd2.pk, "scope": "group", "team": self.team.pk,
                                   "note": "Family wins together"})
        self.assertEqual(set(rnd2.payouts.values_list("membership__user", flat=True)), {self.a.pk, self.b.pk})

    def test_automatic_within_group(self):
        from apps.equb.models import Contribution
        Contribution.objects.filter(round=self.rnd).update(status="PAID")
        r = self.client.post(reverse("console:equb_draw_now"),
                             {"method": "auto", "round": self.rnd.pk, "scope": "group", "team": self.team.pk})
        self.assertEqual(r.status_code, 302)
        winners = set(self.rnd.payouts.values_list("membership__user", flat=True))
        self.assertTrue(winners and winners <= {self.a.pk, self.b.pk})
