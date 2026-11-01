from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.testutils import client_for, make_bag, make_flight, make_package, make_user, passenger
from apps.payments.models import PaymentTransaction

from .models import AircraftType, Booking, Seat
from .services import pricing
from .services.bookings import expire_unpaid_bookings
from .services.tickets import verify_payload


class SeatMapGenerationTests(TestCase):
    def test_positions(self):
        pos = AircraftType.positions_for("ABC|DEF")
        self.assertEqual(pos, {"A": "WINDOW", "B": "MIDDLE", "C": "AISLE", "D": "AISLE", "E": "MIDDLE", "F": "WINDOW"})
        pos = AircraftType.positions_for("ABC|DEFG|HJK")
        self.assertEqual((pos["D"], pos["E"], pos["G"], pos["K"]), ("AISLE", "MIDDLE", "AISLE", "WINDOW"))

    def test_generate_and_surcharges(self):
        f, _, _ = make_flight()
        self.assertEqual(f.seats.count(), 2 * 4 + 5 * 6)
        self.assertEqual(f.seats.get(number="10A").surcharge, Decimal("1800"))  # legroom + window
        self.assertEqual(f.seats.get(number="11C").surcharge, Decimal("200"))


class PricingTests(TestCase):
    def test_dynamic_pricing_rises_with_load_and_time(self):
        f, eco, _ = make_flight(days_ahead=70)
        early = pricing.current_fare(eco)
        self.assertEqual(early, Decimal("16200"))  # 18000 * 0.90
        eco.seats_sold = 18  # 90 % load
        self.assertGreater(pricing.current_fare(eco), early)
        f.departure_time = timezone.now() + timedelta(days=3)
        eco.seats_sold = 0
        self.assertEqual(pricing.current_fare(eco), Decimal("24840"))  # 18000*1.38


class BookingFlowTests(TestCase):
    def setUp(self):
        self.flight, self.eco, self.bus = make_flight()
        self.user = make_user("Abebe Kebede")
        self.api = client_for(self.user)
        self.bag = make_bag()

    def seat(self, number):
        return self.flight.seats.get(number=number)

    def test_search_and_seat_map(self):
        r = APIClient().get("/api/travel/flights/", {"origin": "ADD", "destination": "dxb", "passengers": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["count"], 1)
        r = APIClient().get(f"/api/travel/flights/{self.flight.id}/seat-map/")
        self.assertEqual([c["cabin"] for c in r.data["cabins"]], ["BUSINESS", "ECONOMY"])
        self.assertEqual(len(r.data["cabins"][1]["rows"]), 5)

    def test_seat_lock_conflict_between_users(self):
        s = self.seat("11A")
        r = self.api.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [s.id]}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        other = client_for(make_user("Hana"))
        r = other.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [s.id]}, format="json")
        self.assertEqual(r.status_code, 409)
        # other user sees it as occupied, owner sees it as theirs
        m = other.get(f"/api/travel/flights/{self.flight.id}/seat-map/").data
        seat = [x for row in m["cabins"][1]["rows"] for x in row["seats"] if x["number"] == "11A"][0]
        self.assertEqual(seat["status"], "OCCUPIED")
        # expired holds become available again
        Seat.objects.filter(pk=s.pk).update(locked_until=timezone.now() - timedelta(seconds=1))
        r = other.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [s.id]}, format="json")
        self.assertEqual(r.status_code, 200)

    def _book(self, seats, **extra):
        self.api.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [s.id for s in seats]}, format="json")
        body = {"fare_class": self.eco.id, "contact_phone": self.user.phone,
                "passengers": [passenger(seat=s.id, first_name=f"P{i}") for i, s in enumerate(seats)]}
        body["passengers"][0]["baggage"] = [{"option": self.bag.id, "quantity": 1}]
        body.update(extra)
        return self.api.post("/api/travel/bookings/", body, format="json")

    def test_full_booking_lifecycle(self):
        r = self._book([self.seat("10A"), self.seat("11B")])
        self.assertEqual(r.status_code, 201, r.data)
        ref = r.data["reference"]
        fare = pricing.current_fare(self.eco)
        expected = 2 * (fare + Decimal("3200")) + Decimal("1800") + Decimal("0") + Decimal("3900")
        self.assertEqual(Decimal(r.data["total_amount"]), expected)
        self.eco.refresh_from_db()
        self.assertEqual(self.eco.seats_sold, 2)

        r = self.api.post(f"/api/travel/bookings/{ref}/pay/", {"provider": "mock"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertIn("/api/payments/mock/checkout/", r.data["checkout_url"])
        # simulate the customer completing the sandbox checkout
        r = APIClient().post(f"/api/payments/mock/checkout/{r.data['reference']}/", {"result": "SUCCESS"}, format="json")
        self.assertEqual(r.data["status"], "SUCCESS")

        b = Booking.objects.get(reference=ref)
        self.assertEqual(b.status, Booking.Status.CONFIRMED)
        self.assertEqual(b.tickets.count(), 2)
        self.assertEqual(self.seat("10A").status, Seat.Status.BOOKED)
        data = verify_payload(b.tickets.first().qr_payload)
        self.assertEqual(data["p"], ref)

        pdf = self.api.get(f"/api/travel/bookings/{ref}/itinerary/")
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))
        qr = self.api.get(f"/api/travel/bookings/{ref}/qr/{b.tickets.first().ticket_number}/")
        self.assertEqual(qr["Content-Type"], "image/png")

        staff = client_for(make_user("Agent", staff=True))
        r = staff.post("/api/travel/tickets/verify/", {"qr_payload": b.tickets.first().qr_payload}, format="json")
        self.assertTrue(r.data["valid"])
        r = staff.post("/api/travel/tickets/verify/", {"qr_payload": "tampered" + b.tickets.first().qr_payload}, format="json")
        self.assertEqual(r.status_code, 400)

        # duplicate webhooks don't double-fulfil
        txn = PaymentTransaction.objects.get(object_id=b.id)
        from apps.payments.services import apply_status
        apply_status(txn.pk, "SUCCESS")
        self.assertEqual(b.tickets.count(), 2)

    def test_cannot_book_seat_without_hold(self):
        body = {"fare_class": self.eco.id, "contact_phone": self.user.phone, "passengers": [passenger(seat=self.seat("12A").id)]}
        r = self.api.post("/api/travel/bookings/", body, format="json")
        self.assertEqual(r.status_code, 409)

    def test_cabin_mismatch_rejected(self):
        self.api.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [self.seat("1A").id]}, format="json")
        body = {"fare_class": self.eco.id, "contact_phone": self.user.phone, "passengers": [passenger(seat=self.seat("1A").id)]}
        self.assertEqual(self.api.post("/api/travel/bookings/", body, format="json").status_code, 409)

    def test_expiry_releases_inventory(self):
        r = self._book([self.seat("13C")])
        Booking.objects.filter(reference=r.data["reference"]).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(expire_unpaid_bookings(), 1)
        self.assertEqual(self.seat("13C").status, Seat.Status.AVAILABLE)
        self.eco.refresh_from_db()
        self.assertEqual(self.eco.seats_sold, 0)
        # seat can be booked again by someone else
        other = make_user("Other")
        c = client_for(other)
        c.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [self.seat("13C").id]}, format="json")
        r = c.post("/api/travel/bookings/", {"fare_class": self.eco.id, "contact_phone": other.phone,
                                             "passengers": [passenger(seat=self.seat("13C").id)]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)

    def test_package_booking_and_quote(self):
        pkg = make_package()
        r = self.api.post("/api/travel/bookings/quote/", {"package": pkg.id, "passengers": [{"passenger_type": "ADT"}, {"passenger_type": "CHD"}]}, format="json")
        self.assertEqual(Decimal(r.data["total"]), Decimal("185000") + Decimal("157250"))
        r = self.api.post("/api/travel/bookings/", {"package": pkg.id, "contact_phone": self.user.phone,
                                                    "passengers": [passenger()]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        pkg.refresh_from_db()
        self.assertEqual(pkg.seats_sold, 1)

    def test_infant_requires_adult(self):
        r = self.api.post("/api/travel/bookings/", {"fare_class": self.eco.id, "contact_phone": self.user.phone,
                                                    "passengers": [passenger(passenger_type="INF")]}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_dashboard(self):
        r = self.api.get("/api/home/dashboard/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("upcoming_flights", r.data)
        self.assertEqual(r.data["user"]["full_name"], "Abebe Kebede")


class TravelOperationsTests(TestCase):
    """Office payments, 'My trips' tabs, cancellation policy, refunds, check-in desk and reminders."""

    def setUp(self):
        from .services import operations as ops

        self.ops = ops
        self.flight, self.eco, self.bus = make_flight()
        self.user = make_user("Sara Ali")
        self.api = client_for(self.user)
        self.staff = make_user("Desk Agent", staff=True)

    def _book(self, fare, seat_numbers):
        seats = [self.flight.seats.get(number=n) for n in seat_numbers]
        self.api.post(f"/api/travel/flights/{self.flight.id}/lock-seats/", {"seat_ids": [s.id for s in seats]}, format="json")
        r = self.api.post("/api/travel/bookings/", {"fare_class": fare.id, "contact_phone": self.user.phone, "passengers": [
            passenger(seat=s.id, first_name=f"P{i}") for i, s in enumerate(seats)]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        return Booking.objects.get(reference=r.data["reference"])

    def test_office_payment_confirms_and_tabs(self):
        b = self._book(self.eco, ["10A"])
        tabs = self.api.get("/api/travel/bookings/counts/").data
        self.assertEqual(tabs, {"pending": 1, "upcoming": 0, "past": 0})
        with self.assertRaises(Exception):
            self.ops.record_office_payment(b, self.staff, method="bank")  # bank transfer needs a reference
        b = self.ops.record_office_payment(b, self.staff, method="cash", note="Receipt 77")
        self.assertEqual(b.status, Booking.Status.CONFIRMED)
        self.assertEqual(b.tickets.count(), 1)
        txn = PaymentTransaction.objects.get(object_id=b.id, provider="cash")
        self.assertEqual(txn.recorded_by, self.staff)
        self.assertTrue(b.events.filter(kind="PAYMENT").exists())
        self.assertTrue(b.events.filter(kind="CONFIRMED").exists())
        r = self.api.get("/api/travel/bookings/", {"tab": "upcoming"})
        self.assertEqual([x["reference"] for x in r.data["results"]], [b.reference])
        self.assertEqual(self.api.get("/api/travel/bookings/", {"tab": "pending"}).data["count"], 0)
        dash = self.api.get("/api/home/dashboard/").data
        self.assertEqual(dash["my_upcoming_bookings"][0]["reference"], b.reference)
        self.assertEqual(dash["my_pending_bookings"], [])

    def test_customer_policy_and_staff_refund(self):
        saver = self._book(self.eco, ["11A"])
        self.ops.record_office_payment(saver, self.staff, method="cash")
        detail = self.api.get(f"/api/travel/bookings/{saver.reference}/").data
        self.assertFalse(detail["cancel_policy"]["can_cancel"])
        r = self.api.post(f"/api/travel/bookings/{saver.reference}/cancel/")
        self.assertEqual(r.status_code, 409)  # non-refundable fare: office only

        # refundable business fare with a change fee
        self.bus.change_fee = Decimal("2000")
        self.bus.save()
        biz = self._book(self.bus, ["1A"])
        self.ops.record_office_payment(biz, self.staff, method="cash")
        biz.refresh_from_db()
        policy = self.api.get(f"/api/travel/bookings/{biz.reference}/").data["cancel_policy"]
        self.assertTrue(policy["can_cancel"])
        self.assertEqual(Decimal(policy["refund_estimate"]), biz.total_amount - Decimal("2000"))
        r = self.api.post(f"/api/travel/bookings/{biz.reference}/cancel/")
        self.assertEqual(r.data["status"], "CANCELLED")
        biz.refresh_from_db()
        self.assertTrue(biz.refund_due)
        self.assertIn(biz, self.ops.refunds_due())
        self.assertEqual(self.flight.seats.get(number="1A").status, Seat.Status.AVAILABLE)

        with self.assertRaises(Exception):
            self.ops.refund_booking(biz, self.staff, amount=biz.total_amount + 1, method="Cash")
        biz = self.ops.refund_booking(biz, self.staff, amount=biz.total_amount - Decimal("2000"), method="Cash")
        self.assertEqual(biz.status, Booking.Status.REFUNDED)
        self.assertNotIn(biz, self.ops.refunds_due())
        txn = PaymentTransaction.objects.get(object_id=biz.id)
        self.assertEqual(txn.refunded_amount, biz.total_amount - Decimal("2000"))
        self.assertEqual(txn.status, "SUCCESS")  # partly refunded money stays on the payment

        # staff can cancel a non-refundable fare and refund it in one step
        saver = self.ops.refund_booking(saver, self.staff, amount=saver.total_amount, method="Bank transfer",
                                        reference="CBE-1")
        self.assertEqual(saver.status, Booking.Status.REFUNDED)
        self.assertEqual(PaymentTransaction.objects.get(object_id=saver.id).status, "REFUNDED")
        self.assertEqual(saver.tickets.filter(status="VOID").count(), 1)
        self.assertEqual(self.api.get("/api/travel/bookings/counts/").data["past"], 2)

    def test_desk_check_in_and_board(self):
        b = self.ops.record_office_payment(self._book(self.eco, ["12A", "12B"]), self.staff, method="cash")
        t1, t2 = list(b.tickets.order_by("id"))
        booking, ticket, _ = self.ops.find_for_desk(t1.qr_payload)
        self.assertEqual((booking, ticket), (b, t1))
        self.assertEqual(self.ops.find_for_desk(b.reference.lower())[0], b)
        self.assertEqual(self.ops.find_for_desk(t2.ticket_number)[1], t2)
        self.assertIn("not a valid", self.ops.find_for_desk("x" * 40)[2])
        with self.assertRaises(Exception):
            self.ops.board(t1, self.staff)  # must check in first
        self.ops.check_in(t1, self.staff)
        self.ops.board(t1, self.staff)
        t1.refresh_from_db()
        self.assertEqual(t1.status, "BOARDED")
        self.ops.undo_desk(t1, self.staff)
        t1.refresh_from_db()
        self.assertEqual(t1.status, "CHECKED_IN")
        staff_api = client_for(self.staff)
        r = staff_api.post("/api/travel/tickets/verify/", {"qr_payload": t2.qr_payload, "action": "check_in"}, format="json")
        self.assertTrue(r.data["valid"])
        self.assertEqual(r.data["status"], "CHECKED_IN")
        self.assertEqual(r.data["seat"], "12B")

    def test_trip_reminder_and_paid_after_expiry(self):
        b = self.ops.record_office_payment(self._book(self.eco, ["13A"]), self.staff, method="cash")
        Booking.objects.filter(pk=b.pk)  # departs in 40 days – nothing yet
        self.assertEqual(self.ops.send_trip_reminders(), 0)
        later = self.flight.departure_time - timedelta(hours=20)
        self.assertEqual(self.ops.send_trip_reminders(now=later), 1)
        self.assertEqual(self.ops.send_trip_reminders(now=later), 0)  # only once

        late = self._book(self.eco, ["13B"])
        Booking.objects.filter(pk=late.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        expire_unpaid_bookings()
        from django.contrib.contenttypes.models import ContentType
        PaymentTransaction.objects.create(user=self.user, provider="mock", purpose="BOOKING", amount=late.total_amount,
                                          status="SUCCESS", content_type=ContentType.objects.get_for_model(Booking),
                                          object_id=late.pk)
        self.assertIn(late, self.ops.paid_after_expiry())
        late = self.ops.refund_booking(Booking.objects.get(pk=late.pk), self.staff, amount=late.total_amount, method="Cash")
        self.assertEqual(late.status, Booking.Status.REFUNDED)
        self.assertNotIn(late, self.ops.paid_after_expiry())
