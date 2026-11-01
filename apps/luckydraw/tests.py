from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import client_for, make_package, make_user, passenger
from apps.notifications.models import Notification
from apps.payments.models import PaymentTransaction
from apps.travel.models import Booking

from . import services
from .models import CrowdfundingCampaign, Donation, Giveaway, GiveawayParticipant


def pay(reference):
    r = APIClient().post(f"/api/payments/mock/checkout/{reference}/", {"result": "SUCCESS"}, format="json")
    assert r.data["status"] == "SUCCESS", r.data


class CrowdfundingTests(TestCase):
    def setUp(self):
        self.beneficiary = make_user("Fatuma Ali")
        self.c = CrowdfundingCampaign.objects.create(title="Help Ummi Fatuma", beneficiary_name="Fatuma Ali",
                                                     beneficiary=self.beneficiary, goal_amount=Decimal("300"))

    def test_fee_is_added_on_top_and_gift_goes_100_percent(self):
        api = client_for(make_user("Donor One"))
        q = api.get(f"/api/community/crowdfunding/{self.c.slug}/quote/?amount=100").data
        self.assertEqual((q["amount"], q["fee"], q["total"]), ("100.00", "5.00", "105.00"))
        r = api.post(f"/api/community/crowdfunding/{self.c.slug}/donate/",
                     {"amount": 100, "kind": "HIBAH", "provider": "mock", "message": "Dua for you"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["payment"]["amount"]), Decimal("105"))
        self.assertEqual(r.data["payment"]["purpose"], "DONATION")
        pay(r.data["payment"]["reference"])
        self.c.refresh_from_db()
        self.assertEqual((self.c.raised_amount, self.c.fees_collected, self.c.donors_count), (Decimal("100"), Decimal("5"), 1))
        self.assertTrue(Notification.objects.filter(user=self.beneficiary, kind="DONATION").exists())
        # nothing about tickets or prizes
        self.assertNotIn("entries", str(r.data))

    def test_goal_reached_minimum_and_payouts(self):
        api = client_for(make_user())
        r = api.post(f"/api/community/crowdfunding/{self.c.slug}/donate/", {"amount": 5, "provider": "mock"}, format="json")
        self.assertEqual(r.status_code, 409)
        for amount in (200, 150):
            r = api.post(f"/api/community/crowdfunding/{self.c.slug}/donate/", {"amount": amount, "provider": "mock"}, format="json")
            pay(r.data["payment"]["reference"])
        self.c.refresh_from_db()
        self.assertEqual(self.c.status, "FUNDED")
        r = api.post(f"/api/community/crowdfunding/{self.c.slug}/donate/", {"amount": 50, "provider": "mock"}, format="json")
        self.assertEqual(r.status_code, 409)  # stops at the goal
        with self.assertRaises(BusinessRuleError):
            services.record_disbursement(self.c, amount=400, paid_to="Fatuma")
        services.record_disbursement(self.c, amount=350, paid_to="Fatuma Ali", method="Paid to travel booking")
        self.c.refresh_from_db()
        self.assertEqual((self.c.disbursed_amount, self.c.available_to_disburse), (Decimal("350"), Decimal("0")))
        donors = APIClient().get(f"/api/community/crowdfunding/{self.c.slug}/donors/").data
        self.assertEqual(len(donors["donors"]), 2)
        self.assertEqual(len(donors["disbursements"]), 1)


class GiveawayTests(TestCase):
    def setUp(self):
        now = timezone.now()
        self.pkg = make_package(price=185000)
        self.g = Giveaway.objects.create(
            title="Umrah Giveaway", prize_type="TRAVEL_PACKAGE", prize_title="Umrah for one", prize_value=Decimal("185000"),
            prize_package=self.pkg, number_of_winners=1, referrals_per_entry=10, starts_at=now - timedelta(hours=1),
            ends_at=now + timedelta(days=1), draw_at=now + timedelta(days=1))
        self.inviter = make_user("Inviter Ahmed")

    def signup(self, i):
        r = APIClient().post("/api/auth/register/", {"phone": f"09770{i:05d}", "full_name": f"Friend {i}",
                                                     "password": "Str0ng-pass-123", "referral_code": self.inviter.referral_code},
                             format="json")
        assert r.status_code == 201, r.data

    def test_ten_referrals_one_free_entry_then_fair_draw_and_claim(self):
        for i in range(9):
            self.signup(i)
        p = GiveawayParticipant.objects.get(giveaway=self.g, user=self.inviter)
        self.assertEqual((p.referrals, p.entries), (9, 0))
        prog = client_for(self.inviter).get(f"/api/community/giveaways/{self.g.slug}/").data["my_progress"]
        self.assertEqual((prog["towards_next"], prog["needed_for_next"]), (9, 1))
        self.signup(9)
        p.refresh_from_db()
        self.g.refresh_from_db()
        self.assertEqual((p.referrals, p.entries, self.g.entries_issued), (10, 1, 1))
        self.assertTrue(Notification.objects.filter(user=self.inviter, title__icontains="Free entry").exists())
        # nobody paid anything to enter
        self.assertFalse(PaymentTransaction.objects.exists())

        with self.assertRaises(BusinessRuleError):
            services.close_and_draw(self.g)  # still open
        Giveaway.objects.filter(pk=self.g.pk).update(status="CLOSED")
        winners = services.close_and_draw(self.g)
        self.assertEqual([w.user for w in winners], [self.inviter])
        self.assertTrue(services.verify(Giveaway.objects.get(pk=self.g.pk))["matches"])

        api = client_for(self.inviter)
        win = api.get("/api/community/my-wins/").data["results"][0]
        r = api.post("/api/travel/bookings/", {"package": self.pkg.id, "contact_phone": self.inviter.phone, "passengers": [passenger()]}, format="json")
        r = api.post(f"/api/community/my-wins/{win['id']}/claim/", {"booking_reference": r.data["reference"]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(Booking.objects.get(user=self.inviter).status, "CONFIRMED")

    def test_referrals_outside_the_window_do_not_count(self):
        old = User.objects.create_user(phone="+251977999999", password="x", full_name="Old Friend", referred_by=self.inviter)
        User.objects.filter(pk=old.pk).update(date_joined=self.g.starts_at - timedelta(days=3))
        services.recount(self.g)
        self.assertFalse(GiveawayParticipant.objects.filter(giveaway=self.g, user=self.inviter, referrals__gt=0).exists())

    def test_summary_and_home(self):
        api = client_for(self.inviter)
        s = api.get("/api/community/summary/").data
        self.assertEqual((s["referral_code"], s["live_giveaways"]), (self.inviter.referral_code, 1))
        home = api.get("/api/home/dashboard/").data
        self.assertEqual(len(home["live_giveaways"]), 1)
        self.assertFalse(Donation.objects.exists())
