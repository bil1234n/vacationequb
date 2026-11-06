"""Travel agencies: portal, customers & families, Equb places, bookings, Chapa split payments and the 5 % commission."""
import hashlib
import hmac
import json
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.core.testutils import make_equb, make_flight, make_package, make_user
from apps.equb.models import EqubMembership
from apps.payments.gateways.chapa import ChapaGateway
from apps.payments.models import PaymentTransaction
from apps.travel.models import Booking

from . import services
from .models import Agency, AgencyCustomer, AgencyLedgerEntry, AgencyStaff, FamilyMember

CHAPA = {"BASE_URL": "https://api.chapa.test/v1", "SECRET_KEY": "CHASECK-test", "WEBHOOK_SECRET": "hook-secret",
         "TIMEOUT": 5}
GATEWAYS = {"chapa": CHAPA, "telebirr": {}}


class FakeResponse:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


def chapa_api(verify_status="success", amount=None, sub_id="sub-123"):
    """Fake Chapa API: records every request and answers like Chapa does."""
    calls = []

    def request(method, url, headers=None, timeout=None, json=None, **kw):
        calls.append({"method": method, "url": url, "json": json})
        if url.endswith("/transaction/initialize"):
            return FakeResponse({"status": "success", "message": "Hosted Link",
                                 "data": {"checkout_url": f"https://checkout.chapa.test/{json['tx_ref']}"}})
        if "/transaction/verify/" in url:
            init = next(c for c in calls if c["url"].endswith("/initialize"))
            return FakeResponse({"status": "success", "data": {
                "status": verify_status, "amount": amount or init["json"]["amount"], "currency": "ETB",
                "reference": "APchapa1"}})
        if url.endswith("/subaccount"):
            return FakeResponse({"status": "success", "data": {"subaccount_id": sub_id}})
        if url.endswith("/banks"):
            return FakeResponse({"data": [{"id": 946, "name": "Commercial Bank of Ethiopia (CBE)", "acct_length": 13}]})
        return FakeResponse({"status": "failed", "message": "not found"}, 404)

    return calls, mock.patch("apps.payments.gateways.chapa.requests.request", side_effect=request)


def make_agency(name="Zemen Travel", split=True, **kw):
    a = Agency.objects.create(name=name, phone="+251911000111", commission_percent=Decimal("5"),
                              chapa_subaccount_id="sub-abc" if split else "", **kw)
    owner = make_user(f"{name} Owner")
    AgencyStaff.objects.create(agency=a, user=owner, role=AgencyStaff.Role.OWNER)
    return a, owner


@override_settings(PAYMENT_GATEWAYS=GATEWAYS)
class ChapaGatewayTests(TestCase):
    def setUp(self):
        self.gw = ChapaGateway(CHAPA)

    def test_signature_checks(self):
        body = json.dumps({"tx_ref": "x", "status": "success"}).encode()
        good = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(self.gw.signature_ok(body, {"x-chapa-signature": good}))
        self.assertTrue(self.gw.signature_ok(body, {"X-Chapa-Signature": good}))
        self.assertFalse(self.gw.signature_ok(body, {"x-chapa-signature": "nope"}))
        constant = hmac.new(b"hook-secret", b"hook-secret", hashlib.sha256).hexdigest()
        self.assertTrue(self.gw.signature_ok(body, {"Chapa-Signature": constant}))
        self.assertFalse(self.gw.signature_ok(body, {}))

    def test_payout_webhooks_are_not_payments(self):
        body = json.dumps({"event": "payout.success", "type": "Payout", "status": "success", "reference": "R"}).encode()
        sig = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        res = self.gw.parse_webhook(body, {"x-chapa-signature": sig})
        self.assertEqual(res.status, "PENDING")

    def test_signed_webhook_for_unknown_ref_is_acknowledged(self):
        body = json.dumps({"event": "charge.success", "tx_ref": "not-ours", "status": "success"}).encode()
        sig = hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        r = self.client.post("/api/payments/webhooks/chapa/", body, content_type="application/json",
                             HTTP_X_CHAPA_SIGNATURE=sig)
        self.assertEqual(r.status_code, 200)
        r = self.client.post("/api/payments/webhooks/chapa/", body, content_type="application/json",
                             HTTP_X_CHAPA_SIGNATURE="forged")
        self.assertEqual(r.status_code, 400)


@override_settings(PAYMENT_GATEWAYS=GATEWAYS)
class AgencyFlowTests(TestCase):
    def setUp(self):
        self.agency, self.owner = make_agency()
        self.client.force_login(self.owner)
        self.pkg = make_package(price=100000)

    def _add_customer(self, phone="0912345678", name="Hawa Mohammed"):
        r = self.client.post("/agency/customers/new/", {"full_name": name, "phone": phone, "password": "secret12"})
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context.get("form").errors)
        return AgencyCustomer.objects.get(user__phone="+251" + phone[1:])

    def test_login_page_sends_agency_staff_to_portal(self):
        self.client.logout()
        r = self.client.post("/console/login/", {"phone": self.owner.phone, "password": "pass12345"})
        self.assertRedirects(r, "/agency/", fetch_redirect_response=False)
        r = self.client.get("/console/")  # the staff console sends them back to their portal
        self.assertRedirects(r, "/agency/", fetch_redirect_response=False)

    def test_customer_family_equb_booking_and_chapa_split(self):
        cust = self._add_customer()
        self.assertTrue(cust.user.check_password("secret12"))
        r = self.client.post(f"/agency/customers/{cust.pk}/family/new/", {
            "first_name": "Amina", "last_name": "Ali", "relation": "CHILD", "gender": "F", "date_of_birth": "2018-05-01"})
        self.assertEqual(r.status_code, 302)
        kid = FamilyMember.objects.get(customer=cust)

        # Equb: a place for the child, paid by the customer (responsibility person)
        g = make_equb(name="Umrah 2027", contribution_amount=Decimal("1000"), frequency_days=30, max_members=5,
                      start_date=timezone.localdate() + timedelta(days=10))
        r = self.client.post(f"/agency/equb/{g.slug}/enrol/", {"customer": cust.pk, "who": kid.pk})
        self.assertEqual(r.status_code, 302)
        m = EqubMembership.objects.get(group=g)
        self.assertEqual((m.user, m.dependent_name), (cust.user, "Amina Ali"))

        # Booking: customer + child on a package
        r = self.client.post(f"/agency/book/package/{self.pkg.pk}/", {
            "customer": cust.pk, "pax-TOTAL_FORMS": 2, "pax-INITIAL_FORMS": 0, "pax-MIN_NUM_FORMS": 1,
            "pax-MAX_NUM_FORMS": 9,
            "pax-0-first_name": "Hawa", "pax-0-last_name": "Mohammed", "pax-0-gender": "F", "pax-0-date_of_birth": "1985-02-02",
            "pax-1-first_name": "Amina", "pax-1-last_name": "Ali", "pax-1-gender": "F", "pax-1-date_of_birth": "2018-05-01"})
        self.assertEqual(r.status_code, 302)
        b = Booking.objects.get(user=cust.user)
        self.assertEqual(b.passengers.filter(passenger_type="CHD").count(), 1)

        calls, patch = chapa_api()
        with patch:
            r = self.client.post(f"/agency/bookings/{b.reference}/", {"action": "pay"})
            self.assertEqual(r.status_code, 302)
            txn = PaymentTransaction.objects.get(object_id=b.pk, purpose="BOOKING")
            self.assertEqual(txn.provider, "chapa")
            self.assertEqual(txn.agency, self.agency)
            self.assertTrue(txn.split_settled)
            init = calls[0]["json"]
            self.assertEqual(init["subaccounts"], {"id": "sub-abc", "split_type": "percentage", "split_value": 0.05})
            self.assertTrue(init["callback_url"].endswith("/api/payments/chapa/callback/"))
            self.assertLessEqual(len(init["customization"]["title"]), 16)
            # Chapa's GET callback → we verify with the API → booking confirmed and commission recorded
            r = self.client.get("/api/payments/chapa/callback/", {"trx_ref": txn.reference.hex, "status": "success"})
            self.assertEqual(r.json()["status"], "SUCCESS")
        b.refresh_from_db()
        self.assertEqual(b.status, "CONFIRMED")
        e = AgencyLedgerEntry.objects.get(payment=txn)
        self.assertEqual(e.gross_amount, txn.amount)
        self.assertEqual(e.commission, (txn.amount * Decimal("0.05")).quantize(Decimal("0.01")))
        self.assertEqual(e.agency_amount + e.commission, txn.amount)
        self.assertEqual(e.payout_status, AgencyLedgerEntry.Payout.SETTLED_BY_CHAPA)
        # a repeated callback doesn't double count
        with chapa_api()[1]:
            self.client.get("/api/payments/chapa/callback/", {"trx_ref": txn.reference.hex})
        self.assertEqual(AgencyLedgerEntry.objects.filter(agency=self.agency).count(), 1)

        for url in ("/agency/", "/agency/customers/", f"/agency/customers/{cust.pk}/", "/agency/equb/",
                    f"/agency/equb/{g.slug}/enrol/", f"/agency/book/?customer={cust.pk}", "/agency/bookings/",
                    f"/agency/bookings/{b.reference}/", "/agency/earnings/", "/agency/team/", "/agency/profile/",
                    f"/agency/book/package/{self.pkg.pk}/?customer={cust.pk}&who=self&who={kid.pk}"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_flight_booking_without_subaccount_is_owed_and_paid_out(self):
        self.agency.chapa_subaccount_id = ""
        self.agency.save()
        cust = self._add_customer()
        _, eco, _ = make_flight()
        r = self.client.get(f"/agency/book/flight/{eco.pk}/?customer={cust.pk}")
        self.assertContains(r, "Traveller 1")
        r = self.client.post(f"/agency/book/flight/{eco.pk}/", {
            "customer": cust.pk, "pax-TOTAL_FORMS": 1, "pax-INITIAL_FORMS": 0, "pax-MIN_NUM_FORMS": 1, "pax-MAX_NUM_FORMS": 9,
            "pax-0-first_name": "Hawa", "pax-0-last_name": "Mohammed", "pax-0-gender": "F", "pax-0-date_of_birth": "1985-02-02",
            "pax-0-passport_number": "EP99"})
        self.assertEqual(r.status_code, 302)
        b = Booking.objects.get(user=cust.user)
        calls, patch = chapa_api()
        with patch:
            txn = services.start_checkout(b)
            self.assertNotIn("subaccounts", calls[0]["json"])
            self.client.get("/api/payments/chapa/callback/", {"trx_ref": txn.reference.hex})
        e = AgencyLedgerEntry.objects.get(payment=txn)
        self.assertEqual(e.payout_status, AgencyLedgerEntry.Payout.OWED)

        staff = make_user("VE Admin", staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get("/console/agencies/").status_code, 200)
        self.assertContains(self.client.get(f"/console/agencies/{self.agency.pk}/"), e.customer.full_name)
        self.client.post(f"/console/agencies/{self.agency.pk}/", {"action": "mark_paid", "entries": [e.pk],
                                                                   "reference": "CBE-TRX-1"})
        e.refresh_from_db()
        self.assertEqual((e.payout_status, e.paid_reference), (AgencyLedgerEntry.Payout.PAID, "CBE-TRX-1"))

    def test_customer_paying_from_the_app_counts_for_their_agency(self):
        cust = self._add_customer()
        from apps.travel.services.bookings import create_package_booking
        from apps.core.testutils import passenger
        b = create_package_booking(user=cust.user, package_id=self.pkg.pk, passengers=[passenger()],
                                   contact_phone=cust.user.phone)
        calls, patch = chapa_api()
        with patch:
            from apps.payments.services import initiate_payment
            txn = initiate_payment(user=cust.user, target=b, amount=b.total_amount, purpose="BOOKING", provider="chapa")
        self.assertEqual(txn.agency, self.agency)
        self.assertIn("subaccounts", calls[0]["json"])

    def test_equb_contributions_and_non_customers_are_not_agency_sales(self):
        outsider = make_user("Walk-in")
        self.assertIsNone(services.agency_for_payment(make_package(), outsider))
        cust = self._add_customer()
        from apps.equb.models import Contribution
        self.assertIsNone(services.agency_for_payment(Contribution(), cust.user))

    def test_equb_payout_credit_sale_is_owed_to_agency(self):
        cust = self._add_customer()
        from apps.core.testutils import passenger
        from apps.travel.services.bookings import book_with_credit, create_package_booking
        b = create_package_booking(user=cust.user, package_id=self.pkg.pk, passengers=[passenger()],
                                   contact_phone=cust.user.phone)
        book_with_credit(b, b.total_amount, Booking.Funding.EQUB_PAYOUT, "EQV-1")
        e = AgencyLedgerEntry.objects.get(booking=b, kind=AgencyLedgerEntry.Kind.CREDIT)
        self.assertEqual((e.gross_amount, e.payout_status), (Decimal("100000.00"), AgencyLedgerEntry.Payout.OWED))
        self.assertEqual(e.commission, Decimal("5000.00"))

    def test_agencies_cannot_see_each_other(self):
        cust = self._add_customer()
        other, other_owner = make_agency("Other Tours")
        self.client.force_login(other_owner)
        self.assertEqual(self.client.get(f"/agency/customers/{cust.pk}/").status_code, 404)
        self.assertNotContains(self.client.get("/agency/customers/"), f"/agency/customers/{cust.pk}/")
        # and a customer can't be signed up by two agencies
        r = self.client.post("/agency/customers/new/", {"full_name": "Hawa", "phone": "0912345678"})
        self.assertContains(r, "another agency")

    def test_non_agency_user_is_refused(self):
        self.client.force_login(make_user("Plain customer"))
        self.assertRedirects(self.client.get("/agency/"), "/console/login/", fetch_redirect_response=False)


@override_settings(PAYMENT_GATEWAYS=GATEWAYS)
class ConsoleAgencyAdminTests(TestCase):
    def setUp(self):
        self.client.force_login(make_user("VE Admin", staff=True))

    def test_create_agency_with_owner_then_chapa_subaccount(self):
        calls, patch = chapa_api(sub_id="ACCT-777")
        with patch:
            r = self.client.get("/console/agencies/new/")
            self.assertContains(r, "Commercial Bank of Ethiopia")
            r = self.client.post("/console/agencies/new/", {
                "name": "Habesha Tours", "phone": "0911223300", "city": "Addis Ababa", "commission_percent": "5",
                "status": "ACTIVE", "owner_name": "Selam Tadesse", "owner_phone": "0911223301", "owner_password": "owner123",
                "bank_code": "946", "account_name": "Habesha Tours PLC", "account_number": "1000123456789"})
            self.assertEqual(r.status_code, 302, r.context and r.context["form"].errors)
            a = Agency.objects.get(name="Habesha Tours")
            self.assertEqual(a.bank_name, "Commercial Bank of Ethiopia (CBE)")
            owner = a.staff.get().user
            self.assertTrue(owner.check_password("owner123"))
            self.client.post(f"/console/agencies/{a.pk}/", {"action": "subaccount"})
        a.refresh_from_db()
        self.assertEqual(a.chapa_subaccount_id, "ACCT-777")
        sent = next(c for c in calls if c["url"].endswith("/subaccount"))["json"]
        self.assertEqual((sent["bank_code"], sent["split_type"], sent["split_value"]), (946, "percentage", 0.05))
        self.assertEqual(self.client.get(f"/console/agencies/{a.pk}/edit/").status_code, 200)
