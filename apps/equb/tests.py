from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import make_equb, client_for, make_package, make_user, passenger
from apps.notifications.models import Notification
from apps.payments.models import PaymentTransaction
from apps.travel.models import Booking

from . import operations, services
from . import standing as st
from .models import (CallLog, Contribution, EqubGroup, EqubMembership, EqubPayment, EqubPayout, EqubRound, EqubSettings,
                     Termination)


def pay(api, round_id):
    r = api.post(f"/api/equb/rounds/{round_id}/contribute/", {"provider": "mock"}, format="json")
    assert r.status_code == 201, r.data
    r = APIClient().post(f"/api/payments/mock/checkout/{r.data['reference']}/", {"result": "SUCCESS"}, format="json")
    assert r.data["status"] == "SUCCESS"


def checkout(reference, result="SUCCESS"):
    return APIClient().post(f"/api/payments/mock/checkout/{reference}/", {"result": result}, format="json")


class EqubTests(TestCase):
    def setUp(self):
        self.pkg = make_package(price=60000)
        self.group = make_equb(
            name="Umrah Family Equb", destination="UMRAH", package=self.pkg, contribution_amount=Decimal("15000"),
            frequency="MONTHLY", max_members=4, min_members_to_start=2, start_date=timezone.localdate(),
            service_fee_percent=Decimal("0"))
        self.users = [make_user(f"Member {i}") for i in range(4)]
        self.apis = [client_for(u) for u in self.users]

    def test_add_months(self):
        self.assertEqual(services.add_months(date(2026, 1, 31), 1), date(2026, 2, 28))

    def test_full_cycle(self):
        for api in self.apis:
            r = api.post(f"/api/equb/groups/{self.group.slug}/join/")
            self.assertEqual(r.status_code, 201, r.data)
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, "ACTIVE")  # auto-started when full
        self.assertEqual(self.group.rounds.count(), 4)
        detail = self.apis[0].get(f"/api/equb/groups/{self.group.slug}/").data
        self.assertEqual(detail["current_round_detail"]["number"], 1)
        self.assertEqual(len(detail["current_round_detail"]["seed_commitment"]), 64)
        self.assertEqual(detail["my_current_contribution"]["status"], "PENDING")
        self.assertEqual(detail["my_standing"]["status"], st.CURRENT)
        self.assertTrue(detail["rules_summary"])

        winners = set()
        staff = client_for(make_user("Admin", staff=True))
        for n in range(1, 5):
            rnd = self.group.rounds.get(number=n)
            for api in self.apis:
                pay(api, rnd.id)
            r = staff.post(f"/api/equb/rounds/{rnd.id}/draw/")
            self.assertEqual(r.status_code, 200, r.data)
            self.assertTrue(r.data["verification"]["matches"])
            self.assertEqual(Decimal(r.data["winners"][0]["amount"]), Decimal("60000"))
            winners.add(EqubPayout.objects.get(round=rnd).membership.user_id)
        self.assertEqual(len(winners), 4)  # every member wins exactly once
        self.group.refresh_from_db()
        self.assertEqual(self.group.status, "COMPLETED")

        ledger = APIClient().get(f"/api/equb/groups/{self.group.slug}/ledger/").data
        self.assertEqual(ledger["total_collected"], "240000.00")
        self.assertNotIn(self.users[0].phone, str(ledger))  # phones are masked
        m = EqubMembership.objects.get(group=self.group, user=self.users[0])
        self.assertEqual(st.standing_for(m).status, st.COMPLETED)

    def test_unpaid_members_excluded_and_redeem(self):
        rules = EqubSettings.load(); rules.payout_basis = EqubGroup.PayoutBasis.COLLECTED; rules.save()
        self.group = EqubGroup.objects.get(pk=self.group.pk)
        self.group.save()
        for u in self.users[:3]:
            services.join_group(u, self.group)
        services.start_group(self.group)
        rnd = self.group.rounds.get(number=1)
        pay(self.apis[0], rnd.id)
        with self.assertRaises(BusinessRuleError):
            services.run_draw(self.group.rounds.get(number=2))  # out of order
        payouts = services.run_draw(rnd)
        self.assertEqual(payouts[0].membership.user, self.users[0])  # only payer eligible
        self.assertTrue(services.verify_draw(rnd)["matches"])
        snap = rnd.draw.__class__.objects.get(round=rnd).pool_snapshot
        self.assertEqual(snap["eligible_count"], 1)
        self.assertEqual(snap["excluded_count"], 2)
        self.assertIn("Round 1 contribution not paid", snap["excluded"][0]["reasons"])

        # winner books the Umrah package and redeems the payout (15 000 credit)
        api = self.apis[0]
        r = api.post("/api/travel/bookings/", {"package": self.pkg.id, "contact_phone": self.users[0].phone,
                                               "passengers": [passenger()]}, format="json")
        ref = r.data["reference"]
        r = api.post(f"/api/equb/payouts/{payouts[0].id}/redeem/", {"booking_reference": ref}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        b = Booking.objects.get(reference=ref)
        self.assertEqual(b.total_amount, Decimal("45000"))
        self.assertEqual(b.funding_source, "EQUB_PAYOUT")
        r = api.post(f"/api/equb/payouts/{payouts[0].id}/redeem/", {"booking_reference": ref}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_full_pot_payout_is_guaranteed(self):
        rules = EqubSettings.load(); rules.service_fee_percent = Decimal("2"); rules.save()
        self.group = EqubGroup.objects.get(pk=self.group.pk)
        self.group.save()
        for u in self.users[:3]:
            services.join_group(u, self.group)
        services.start_group(self.group)
        rnd = self.group.rounds.get(number=1)
        pay(self.apis[0], rnd.id)
        payout = services.run_draw(rnd)[0]
        self.assertEqual(payout.gross_amount, Decimal("45000.00"))  # 3 members x 15 000, though only one paid
        self.assertEqual(payout.fee_amount, Decimal("900.00"))
        self.assertEqual(payout.amount, Decimal("44100.00"))

    def test_join_rules(self):
        services.join_group(self.users[0], self.group)
        with self.assertRaises(BusinessRuleError):
            services.join_group(self.users[0], self.group)
        services.leave_group(self.users[0], self.group)
        services.join_group(self.users[0], self.group)
        self.assertEqual(self.group.members_count, 1)

    def test_late_fee(self):
        rules = EqubSettings.load(); rules.late_fee = Decimal("500"); rules.save()
        self.group = EqubGroup.objects.get(pk=self.group.pk)
        self.group.save()
        for u in self.users[:2]:
            services.join_group(u, self.group)
        services.start_group(self.group)
        rnd = self.group.rounds.get(number=1)
        rnd.due_date = timezone.localdate() - timedelta(days=10)
        rnd.save()
        self.assertEqual(services.sweep_late_contributions(), 2)
        c = Contribution.objects.filter(round=rnd).first()
        self.assertEqual(c.amount_due, Decimal("15500"))
        self.assertEqual(EqubMembership.objects.get(pk=c.membership_id).missed_count, 1)


class ProfessionalEqubTests(TestCase):
    """Standing, multi-round & office payments, draw eligibility, exits, terminations, reminders."""

    def setUp(self):
        self.group = make_equb(
            name="Dubai Traders", destination="DUBAI", contribution_amount=Decimal("1000"), frequency="WEEKLY",
            max_members=3, min_members_to_start=2, start_date=timezone.localdate(), late_fee=Decimal("50"),
            grace_days=2, service_fee_percent=Decimal("0"))
        self.users = [make_user(f"Trader {i}") for i in range(3)]
        self.apis = [client_for(u) for u in self.users]
        self.staff = make_user("Office", staff=True)
        for u in self.users:
            services.join_group(u, self.group)  # auto-starts when full
        self.group.refresh_from_db()
        self.m = [EqubMembership.objects.get(group=self.group, user=u) for u in self.users]
        self.r1 = self.group.rounds.get(number=1)

    def make_overdue(self, rnd, days=10):
        EqubRound.objects.filter(pk=rnd.pk).update(due_date=timezone.localdate() - timedelta(days=days))

    def test_standing_ladder(self):
        self.assertEqual(self.group.status, "ACTIVE")
        s = st.standing_for(self.m[0])
        self.assertEqual((s.status, s.missed_rounds, s.due_now), (st.CURRENT, 0, Decimal("1000")))
        self.make_overdue(self.r1)
        s = st.standing_for(self.m[0])
        self.assertEqual(s.status, st.NEVER_PAID)
        self.assertEqual(s.arrears, Decimal("1050"))  # late fee is anticipated before the sweep runs
        self.assertEqual(s.days_overdue, 10)
        rules = EqubSettings.load()
        rules.warn_at, rules.block_at, rules.suspend_at = 1, 1, 1
        rules.save()
        # Paid R1 late, then misses R2 → suspended under the stricter rules
        services.record_office_payment(self.m[0], self.staff, method="cash", rounds=1)
        services.run_draw(self.r1)
        r2 = self.group.rounds.get(number=2)
        self.make_overdue(r2, 5)
        s = st.standing_for(EqubMembership.objects.get(pk=self.m[0].pk))
        self.assertEqual(s.status, st.SUSPENDED)
        self.assertEqual(s.late_count, 1)

    def test_multi_round_payment_from_app(self):
        self.make_overdue(self.r1)
        api, m = self.apis[1], self.m[1]
        plan = api.get(f"/api/equb/memberships/{m.id}/pay-options/").data
        self.assertTrue(plan["payable"])
        self.assertEqual(plan["overdue_count"], 1)
        self.assertEqual([o["amount"] for o in plan["options"]], ["1050.00", "2050.00", "3050.00"])
        r = api.post(f"/api/equb/memberships/{m.id}/pay/", {"rounds": 3, "provider": "mock"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(Decimal(r.data["amount"]), Decimal("3050"))
        self.assertEqual(checkout(r.data["reference"]).data["status"], "SUCCESS")
        record = EqubPayment.objects.get(membership=m)
        self.assertEqual(record.status, "PAID")
        self.assertEqual([x["round"] for x in record.allocation["rounds"]], [1])
        m.refresh_from_db()
        self.assertEqual(m.prepaid_credit, Decimal("2000"))
        s = st.standing_for(m)
        self.assertEqual((s.status, s.advance_rounds, s.arrears), (st.AHEAD, 2, 0))
        detail = api.get(f"/api/equb/memberships/{m.id}/").data
        self.assertEqual([row["state"] for row in detail["schedule"]], ["paid_late", "prepaid", "prepaid"])
        self.assertEqual(detail["payments"][0]["rounds"], [1])
        self.assertTrue(Notification.objects.filter(user=self.users[1], title__startswith="Payment received").exists())
        # Credit pays the next round the moment it opens
        services.run_draw(self.r1)
        r2 = self.group.rounds.get(number=2)
        c2 = Contribution.objects.get(round=r2, membership=m)
        self.assertEqual(c2.status, "WAIVED")
        m.refresh_from_db()
        self.assertEqual(m.prepaid_credit, Decimal("1000"))
        # Asking for more rounds than allowed is refused
        r = api.post(f"/api/equb/memberships/{m.id}/pay/", {"rounds": 9, "provider": "mock"}, format="json")
        self.assertEqual(r.status_code, 409)

    def test_office_payment_and_partial_amounts(self):
        rec = services.record_office_payment(self.m[2], self.staff, method="cash", amount=Decimal("600"), reference="")
        self.assertEqual(rec.allocation["rounds"], [])  # not enough for a round yet – kept as credit
        rec = services.record_office_payment(self.m[2], self.staff, method="bank", amount=Decimal("400"), reference="CBE-778")
        self.assertEqual([x["round"] for x in rec.allocation["rounds"]], [1])
        txn = PaymentTransaction.objects.get(pk=rec.transaction_id)
        self.assertEqual((txn.provider, txn.status, txn.recorded_by), ("bank", "SUCCESS", self.staff))
        with self.assertRaises(BusinessRuleError):
            services.record_office_payment(self.m[2], self.staff, method="bank", amount=Decimal("10"))  # needs a reference
        c = Contribution.objects.get(round=self.r1, membership=self.m[2])
        self.assertEqual((c.status, c.payment_record_id), ("PAID", rec.id))

    def test_draw_pool_reasons_and_staff_block(self):
        for i in (0, 1):
            services.record_office_payment(self.m[i], self.staff, method="cash", rounds=1)
        operations.set_draw_block(self.m[1], self.staff, until=timezone.localdate() + timedelta(days=30), reason="Duplicate account check")
        pool = services.draw_pool(self.r1)
        by_id = {e["membership_id"]: e for e in pool["entries"]}
        self.assertTrue(by_id[self.m[0].id]["eligible"])
        self.assertIn("Duplicate account check", by_id[self.m[1].id]["reasons"][0])
        self.assertIn("Round 1 contribution not paid", by_id[self.m[2].id]["reasons"])
        self.assertEqual((pool["eligible_count"], pool["odds_percent"]), (1, 100.0))
        r = self.apis[2].get(f"/api/equb/rounds/{self.r1.id}/pool/").data
        self.assertFalse(r["me"]["eligible"])
        payouts = services.run_draw(self.r1)
        self.assertEqual(payouts[0].membership_id, self.m[0].id)
        verify = APIClient().get(f"/api/equb/rounds/{self.r1.id}/verify/").data
        self.assertTrue(verify["matches"])
        self.assertEqual(verify["excluded_count"], 2)

    def test_postpone_and_waive(self):
        operations.postpone_draw(self.r1, self.staff, new_date=timezone.localdate() + timedelta(days=20), reason="Public holiday")
        self.r1.refresh_from_db()
        self.assertEqual(self.r1.draw_note, "Public holiday")
        self.make_overdue(self.r1)
        services.sweep_late_contributions()
        c = Contribution.objects.get(round=self.r1, membership=self.m[0])
        self.assertEqual(c.late_fee, Decimal("50"))
        operations.waive_late_fee(c, self.staff, reason="Bank outage")
        c.refresh_from_db()
        self.assertEqual((c.late_fee, c.late_fee_waived), (Decimal("0"), True))
        self.assertEqual(st.standing_for(self.m[0]).arrears, Decimal("1000"))

    def test_exit_request_termination_and_reinstatement(self):
        api, m = self.apis[2], self.m[2]
        info = api.get(f"/api/equb/memberships/{m.id}/").data["exit"]
        self.assertTrue(info["can_request_exit"])
        r = api.post(f"/api/equb/groups/{self.group.slug}/leave/")
        self.assertEqual(r.status_code, 409)  # running Equb: needs a request
        r = api.post(f"/api/equb/memberships/{m.id}/request-exit/", {"reason": "Moving abroad"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIsNotNone(r.data["exit"]["exit_requested_at"])
        t = operations.terminate(m, self.staff, reason=Termination.Reason.MEMBER_REQUEST, note="Approved")
        m.refresh_from_db()
        self.assertEqual(m.status, "DEFAULTED")
        self.assertFalse(Contribution.objects.filter(round=self.r1, membership=m).exists())  # not yet due → dropped
        self.r1.refresh_from_db()
        self.assertEqual(self.r1.expected_amount, Decimal("2000"))
        self.assertEqual(st.standing_for(m).status, st.TERMINATED)
        operations.reinstate(t, self.staff)
        m.refresh_from_db()
        self.assertEqual(m.status, "ACTIVE")
        self.assertTrue(Contribution.objects.filter(round=self.r1, membership=m).exists())

    def test_call_centre_flags(self):
        self.make_overdue(self.r1)
        log = operations.log_call(self.m[0], self.staff, outcome=CallLog.Outcome.PROMISED,
                                  promised_date=timezone.localdate() + timedelta(days=2), feedback="Pays Friday")
        self.assertEqual(log.arrears_at_call, Decimal("1050"))
        self.m[0].refresh_from_db()
        self.assertIsNotNone(self.m[0].called_at)
        services.record_office_payment(self.m[0], self.staff, method="cash", rounds=1)
        self.m[0].refresh_from_db()
        self.assertIsNone(self.m[0].called_at)  # caught up → back to "uncalled" for next time

    def test_private_pool_join_by_code(self):
        private = make_equb(name="Family circle", destination="UMRAH", contribution_amount=Decimal("500"),
                                           max_members=5, start_date=timezone.localdate(), is_public=False)
        outsider = client_for(make_user("Outsider"))
        self.assertIsNone(outsider.get(f"/api/equb/groups/{private.slug}/").data["join_code"])  # code only for members
        r = outsider.post("/api/equb/groups/join-by-code/", {"code": "nope00"}, format="json")
        self.assertEqual(r.status_code, 409)
        r = outsider.post("/api/equb/groups/join-by-code/", {"code": private.join_code.lower()}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["join_code"], private.join_code)
        self.assertEqual(outsider.get(f"/api/equb/groups/{private.slug}/").status_code, 200)

    def test_reminders_and_escalation(self):
        rules = EqubSettings.load()
        EqubRound.objects.filter(pk=self.r1.pk).update(due_date=timezone.localdate() + timedelta(days=1))
        self.assertEqual(operations.send_due_reminders(rules), 3)
        self.assertEqual(operations.send_due_reminders(rules), 0)  # only once per round
        self.make_overdue(self.r1, 20)
        rules.warn_at = rules.block_at = rules.suspend_at = 1
        rules.save()
        out = operations.escalate_standings(EqubSettings.load())
        self.assertEqual(out["notified"], 3)
        self.assertEqual(out["flagged"], 3)
        self.assertEqual(operations.escalate_standings(EqubSettings.load())["notified"], 0)
        self.assertTrue(EqubMembership.objects.filter(termination_requested_at__isnull=False).count() == 3)

    def test_my_memberships_list(self):
        r = self.apis[0].get("/api/equb/memberships/")
        self.assertEqual(r.status_code, 200)
        row = r.data["results"][0]
        self.assertEqual(row["standing"]["status"], st.CURRENT)
        self.assertEqual(row["standing"]["next_due_amount"], "1000.00")


class DrawSelectionAndGroupEqubTests(TestCase):
    """Manual draws (staff pick the winner), frequency in days, and customer Group Equbs."""

    def setUp(self):
        self.staff = make_user("Ops", staff=True)
        self.group = make_equb(
            name="Pick Test", destination="UMRAH", contribution_amount=Decimal("1000"), frequency_days=10,
            duration_value=3, duration_unit="DAYS", max_members=3, min_members_to_start=3,
            start_date=timezone.localdate(), service_fee_percent=Decimal("0"), draw_type="MANUAL")
        self.users = [make_user(f"Picker {i}") for i in range(3)]
        self.ms = [services.join_group(u, self.group) for u in self.users]  # third join starts it
        self.group.refresh_from_db()
        self.rnd = self.group.rounds.get(number=1)

    def test_days_schedule_and_labels(self):
        self.assertEqual(self.group.status, "ACTIVE")
        self.assertEqual([r.due_date - self.group.start_date for r in self.group.rounds.order_by("number")],
                         [timedelta(days=0), timedelta(days=10), timedelta(days=20)])
        self.assertEqual((self.group.frequency, self.group.get_frequency_display(), self.group.every),
                         ("CUSTOM", "Every 10 days", "every 10 days"))
        self.assertEqual(self.group.end_date, self.group.start_date + timedelta(days=3))

    def test_staff_select_winner(self):
        for m in self.ms[:2]:
            services.mark_contribution_paid(self.rnd.contributions.get(membership=m))
        with self.assertRaises(BusinessRuleError):  # not paid → left out of the draw
            services.run_draw(self.rnd, self.staff, winner_ids=[self.ms[2].id])
        with self.assertRaises(BusinessRuleError):  # left-out member needs a reason
            services.run_draw(self.rnd, self.staff, winner_ids=[self.ms[2].id], include_ineligible=True)
        payouts = services.run_draw(self.rnd, self.staff, winner_ids=[self.ms[1].id], note="Travels first")
        self.assertEqual(payouts[0].membership, self.ms[1])
        self.rnd.refresh_from_db()
        self.assertEqual((self.rnd.draw.mode, self.rnd.draw.note), ("SELECTED", "Travels first"))
        v = services.verify_draw(self.rnd)
        self.assertEqual((v["mode"], v["executed"]), ("SELECTED", True))
        # the round page shows it, and the draws page lists it
        c = self.client
        c.force_login(self.staff)
        from django.urls import reverse
        self.assertContains(c.get(reverse("console:equb_round", args=[self.group.slug, 1])), "Winner chosen by staff")
        self.assertContains(c.get(reverse("console:equb_draws") + "?tab=history"), "Selected")

    def test_console_pick_and_scheduler_skips_manual(self):
        from django.core.management import call_command
        from django.urls import reverse

        services.mark_contribution_paid(self.rnd.contributions.get(membership=self.ms[0]))
        EqubRound.objects.filter(pk=self.rnd.pk).update(draw_date=timezone.localdate())
        call_command("run_scheduler", verbosity=0)
        self.rnd.refresh_from_db()
        self.assertEqual(self.rnd.status, "COLLECTING")  # manual group: the scheduler leaves it for staff
        self.client.force_login(self.staff)
        r = self.client.get(reverse("console:equb_draws"))
        self.assertContains(r, "Pick Test")
        r = self.client.post(reverse("console:equb_round", args=[self.group.slug, 1]),
                             {"action": "draw", "method": "select", "winner": [self.ms[0].id]}, follow=True)
        self.assertContains(r, "Winner chosen")
        self.ms[0].refresh_from_db()
        self.assertTrue(self.ms[0].has_won)
        # Draw settings can switch every unfinished group to automatic draws
        r = self.client.post(reverse("console:equb_draw_settings"),
                             {"min_paid_rounds_to_win": "1", "block_at": "2", "apply_draw_type": "AUTOMATIC"}, follow=True)
        self.assertContains(r, "Draw type changed on 1 group")
        self.group.refresh_from_db()
        self.assertEqual(self.group.draw_type, "AUTOMATIC")

    def test_group_equb_lifecycle(self):
        from .models import GroupEqub

        equb = make_equb(name="Family Umrah", contribution_amount=Decimal("2000"), frequency_days=30, max_members=6,
                         start_date=timezone.localdate() + timedelta(days=3))
        owner, friend = make_user("Org"), make_user("Friend")
        api = client_for(owner)
        r = api.post("/api/equb/group-equbs/", {"equb": equb.slug, "name": "Office friends", "kind": "FRIENDS",
                                                "dependents": [{"name": "Baby Sara"}]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        tid, code = r.data["id"], r.data["invite_code"]
        self.assertEqual((r.data["kind"], r.data["members_count"], r.data["dependents_count"]), ("FRIENDS", 2, 1))
        # friends join the Equb as part of the group with the group's invite code
        j = client_for(friend).post("/api/equb/groups/join-by-code/", {"code": code.lower()}, format="json")
        self.assertEqual(j.status_code, 201, j.data)
        self.assertEqual(GroupEqub.objects.get(pk=tid).live_memberships.count(), 3)
        self.assertEqual(len(client_for(friend).get("/api/equb/group-equbs/").data), 1)
        # the owner pays for the responsibility person: both places are in the owner's list
        mine = client_for(owner).get("/api/equb/memberships/").data["results"]
        self.assertEqual(sorted(m["dependent_name"] for m in mine), ["", "Baby Sara"])
        # only the owner manages it
        detail = client_for(friend).get(f"/api/equb/group-equbs/{tid}/").data
        friend_mid = next(m["membership_id"] for m in detail["members"] if m["is_me"])
        self.assertEqual(client_for(friend).post(f"/api/equb/group-equbs/{tid}/remove-member/",
                                                 {"membership_id": friend_mid}, format="json").status_code, 409)
        r = api.post(f"/api/equb/group-equbs/{tid}/remove-member/", {"membership_id": friend_mid}, format="json")
        self.assertEqual(r.data["members_count"], 2)
        r = api.post(f"/api/equb/group-equbs/{tid}/add-dependent/", {"name": "Grandma"}, format="json")
        self.assertEqual(r.data["dependents_count"], 2)

        # the Equb runs; "Draw now" within the Group Equb picks only from its members
        others = [make_user(f"Other {i}") for i in range(3)]
        for u in others:
            services.join_group(u, equb)
        equb.refresh_from_db()
        self.assertEqual(equb.status, "ACTIVE")  # full → started
        rnd = equb.rounds.get(number=1)
        for c in rnd.contributions.all():
            services.mark_contribution_paid(c)
        team = GroupEqub.objects.get(pk=tid)
        payouts = services.run_draw(rnd, self.staff, team=team)
        self.assertEqual(payouts[0].membership.team_id, tid)

    def test_group_search_filters(self):
        api = client_for(make_user("Browser"))
        make_equb(name="Cheap weekly", contribution_amount=Decimal("500"),
                                 frequency_days=7, max_members=5, start_date=timezone.localdate())
        r = api.get("/api/equb/groups/", {"max_amount": "600", "frequency_days": "7", "has_spots": "1"})
        self.assertEqual([g["name"] for g in r.data["results"]], ["Cheap weekly"])
        r = api.get("/api/equb/groups/", {"search": "cheap"})
        self.assertEqual(r.data["count"], 1)


class GroupEqubInviteTests(TestCase):
    """The owner invites by full phone number; the person accepts and pays for their own place."""

    def test_lookup_invite_accept_decline(self):
        from apps.equb.models import GroupEqubInvite
        owner, friend, other = make_user("Bilal Owner"), make_user("Hana Friend"), make_user("Sami Other")
        g = make_equb(name="Invite Equb", contribution_amount=Decimal("1000"), frequency="MONTHLY", max_members=6,
                      start_date=timezone.localdate() + timedelta(days=5))
        api = client_for(owner)
        r = api.post("/api/equb/group-equbs/", {"equb": g.slug, "name": "Bilal Family", "kind": "FAMILY",
                                                "invite_phones": [friend.phone]}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        tid = r.data["id"]
        self.assertEqual([i["status"] for i in r.data["invites"]], ["PENDING"])
        self.assertEqual(r.data["members_count"], 1)  # the friend is not added until they accept
        r = api.get(f"/api/equb/group-equbs/lookup-phone/?phone={other.phone}&team={tid}")
        self.assertTrue(r.data["found"])
        self.assertEqual(r.data["name"], "Sami O.")
        r = api.get("/api/equb/group-equbs/lookup-phone/?phone=+251999000111")
        self.assertFalse(r.data["found"])
        r = api.post(f"/api/equb/group-equbs/{tid}/invite/", {"phone": other.phone}, format="json")
        self.assertEqual(len(r.data["invites"]), 2)
        r = api.post(f"/api/equb/group-equbs/{tid}/invite/", {"phone": other.phone}, format="json")
        self.assertEqual(r.status_code, 409)  # already waiting

        fapi = client_for(friend)
        inv = fapi.get("/api/equb/group-invites/").data[0]
        self.assertEqual(inv["team_name"], "Bilal Family")
        r = fapi.post(f"/api/equb/group-invites/{inv['id']}/accept/")
        self.assertEqual(r.status_code, 200, r.data)
        m = EqubMembership.objects.get(pk=r.data["membership_id"])
        self.assertEqual((m.user, m.team_id, m.is_dependent), (friend, tid, False))
        oapi = client_for(other)
        oid = oapi.get("/api/equb/group-invites/").data[0]["id"]
        oapi.post(f"/api/equb/group-invites/{oid}/decline/")
        self.assertEqual(GroupEqubInvite.objects.get(pk=oid).status, "DECLINED")
        self.assertEqual(api.get(f"/api/equb/group-equbs/{tid}/").data["members_count"], 2)


class RoundsDrawnCalendarPayPrivacyTests(TestCase):
    def setUp(self):
        self.user = make_user("Liya Haile")
        self.other = make_user("Abel Kebede")
        self.g = make_equb(name="Test – Group", contribution_amount=Decimal("1"), frequency_days=3, max_members=4, rounds_drawn=4,
                           start_date=timezone.localdate() + timedelta(days=7))
        services.join_group(self.user, self.g)

    def test_rounds_drawn_sets_the_number_of_rounds_and_can_be_edited_while_running(self):
        services.start_group(self.g)
        self.g.refresh_from_db()
        self.assertEqual(self.g.rounds.count(), 4)  # one round per member place, not just one per joined member
        self.g.rounds_drawn = 2
        self.g.save()
        services.sync_running_group(self.g)
        self.assertEqual(self.g.rounds.count(), 2)
        self.assertEqual(self.g.total_rounds, 2)
        self.g.rounds_drawn = 4
        self.g.save()
        services.sync_running_group(self.g)
        self.assertEqual(sorted(self.g.rounds.values_list("number", flat=True)), [1, 2, 3, 4])

    def test_starting_again_never_duplicates_round_numbers(self):
        services.start_group(self.g)
        EqubGroup.objects.filter(pk=self.g.pk).update(status=EqubGroup.Status.OPEN)  # staff set it back to "Open"
        services.start_group(self.g)  # used to crash with uniq_round_number
        self.g.refresh_from_db()
        self.assertEqual(self.g.status, EqubGroup.Status.ACTIVE)
        self.assertEqual(self.g.rounds.filter(number=1).count(), 1)

    def test_console_edit_saves_rounds_drawn_and_reschedules(self):
        staff = make_user("Staff", staff=True)
        services.start_group(self.g)
        self.client.force_login(staff)
        self.g.refresh_from_db()
        data = {"equb_package": "", "name": self.g.name, "contribution_amount": "1", "frequency_days": "3",
                "duration_value": "3", "duration_unit": "DAYS", "max_members": "4", "rounds_drawn": "3",
                "start_date": self.g.start_date.isoformat(), "end_date": "", "status": "ACTIVE", "draw_type": "MANUAL",
                "rules_text": "Pay on time\n2. Be kind"}
        from apps.equb.models import EqubPackage
        pkg = EqubPackage.objects.create(name="Test")
        data["equb_package"] = pkg.pk
        r = self.client.post(f"/console/equb/{self.g.slug}/edit/", data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", {}) and r.context["form"].errors)
        self.g.refresh_from_db()
        self.assertEqual(self.g.rounds_drawn, 3)
        self.assertEqual(self.g.rounds.count(), 3)
        detail = client_for(self.user).get(f"/api/equb/groups/{self.g.slug}/").data
        self.assertEqual(detail["terms_content"], ["Pay on time", "Be kind"])

    def test_not_started_join_only_started_join_and_pay(self):
        api = client_for(self.other)
        self.assertEqual(api.post(f"/api/equb/groups/{self.g.slug}/join/").status_code, 201)
        m = EqubMembership.objects.get(group=self.g, user=self.other)
        self.assertFalse(api.get(f"/api/equb/memberships/{m.id}/pay-options/").data["payable"])
        services.start_group(self.g)
        late = make_user("Late Joiner")
        self.assertEqual(client_for(late).post(f"/api/equb/groups/{self.g.slug}/join/").status_code, 201)
        self.assertTrue(api.get(f"/api/equb/memberships/{m.id}/pay-options/").data["payable"])

    def test_ledger_and_winners_hide_other_members_names(self):
        services.join_group(self.other, self.g)
        services.start_group(self.g)
        rows = client_for(self.user).get(f"/api/equb/groups/{self.g.slug}/ledger/").data["members"]
        names = {r["name"] for r in rows}
        self.assertIn("Liya Haile (you)", names)
        self.assertIn("Member 2", names)
        self.assertNotIn("Abel Kebede", " ".join(names))
        self.assertTrue(all(r["phone_masked"] == "" for r in rows))

    def test_calendar_pay_sends_a_bill_at_the_chosen_time(self):
        from . import scheduled
        from .models import ScheduledPayment

        services.start_group(self.g)
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        api = client_for(self.user)
        when = timezone.now() + timedelta(days=2)
        r = api.post("/api/equb/scheduled-payments/", {"membership": m.id, "scheduled_for": when.isoformat(), "rounds": 2,
                                                       "repeat": "ONCE"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "SCHEDULED")
        self.assertEqual(scheduled.dispatch_due(), 0)  # not yet
        self.assertEqual(scheduled.dispatch_due(now=when + timedelta(minutes=1)), 1)
        sp = ScheduledPayment.objects.get(pk=r.data["id"])
        self.assertEqual(sp.status, "SENT")
        self.assertEqual(sp.amount, Decimal("2.00"))
        self.assertTrue(sp.transaction.checkout_url)
        self.assertTrue(Notification.objects.filter(user=self.user, data__scheduled_payment=sp.id,
                                                    data__checkout_url=sp.transaction.checkout_url).exists())
        self.assertEqual(checkout(sp.transaction.reference).data["status"], "SUCCESS")
        rows = api.get(f"/api/equb/scheduled-payments/?membership={m.id}").data
        self.assertEqual(rows[0]["status"], "PAID")

    def test_calendar_pay_rules(self):
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        api = client_for(self.user)
        too_soon = api.post("/api/equb/scheduled-payments/", {"membership": m.id,
                            "scheduled_for": (timezone.now() + timedelta(seconds=30)).isoformat()}, format="json")
        self.assertEqual(too_soon.status_code, 409)
        before_start = api.post("/api/equb/scheduled-payments/", {"membership": m.id,
                                "scheduled_for": (timezone.now() + timedelta(days=1)).isoformat()}, format="json")
        self.assertEqual(before_start.status_code, 409)  # the Equb hasn't started by then
        ok = api.post("/api/equb/scheduled-payments/", {"membership": m.id, "repeat": "EVERY_DUE",
                      "scheduled_for": (timezone.now() + timedelta(days=8)).isoformat()}, format="json")
        self.assertEqual(ok.status_code, 201, ok.data)
        other = client_for(self.other).post(f"/api/equb/scheduled-payments/{ok.data['id']}/cancel/")
        self.assertEqual(other.status_code, 404)
        self.assertEqual(api.post(f"/api/equb/scheduled-payments/{ok.data['id']}/cancel/").data["status"], "CANCELLED")


class PaymentRoundsRejoinPendingTests(TestCase):
    def setUp(self):
        self.user = make_user("Liya Haile")
        self.g = make_equb(name="Pay all", contribution_amount=Decimal("10"), frequency_days=3, max_members=2,
                           duration_value=4, duration_unit="DAYS", rounds_drawn=2,
                           start_date=timezone.localdate(), require_paid_to_draw=False)
        rules = EqubSettings.load()
        rules.min_paid_rounds_to_win = 0
        rules.save()
        services.join_group(self.user, self.g)

    def test_still_to_pay_is_price_per_person_and_winners_keep_paying(self):
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        self.assertEqual(self.g.price_per_person, Decimal("40"))  # 10 × 4
        self.assertEqual(st.standing_for(m).remaining_amount, Decimal("40"))
        services.start_group(self.g)
        self.g.refresh_from_db()
        self.assertEqual(self.g.rounds.count(), 4)  # 4 payments although only 2 draws
        services.run_draw(self.g.rounds.get(number=1))
        m.refresh_from_db()
        self.assertTrue(m.has_won)
        self.g.refresh_from_db()
        self.assertEqual(self.g.status, "ACTIVE")  # not completed: the winner still owes rounds 2-4
        self.assertEqual(self.g.current_round, 2)
        self.assertEqual(services.run_draw(self.g.rounds.get(number=2)), [])  # payment-only round closes
        self.g.refresh_from_db()
        self.assertEqual(self.g.current_round, 3)
        self.assertTrue(Contribution.objects.filter(membership=m, round__number=3).exists())

    def test_rounds_drawn_cannot_exceed_duration(self):
        from apps.console.forms import EqubForm
        form = EqubForm(instance=self.g, data={"name": "x", "contribution_amount": "10", "frequency_days": "3",
                                               "duration_value": "3", "duration_unit": "DAYS", "max_members": "8",
                                               "rounds_drawn": "5", "start_date": self.g.start_date.isoformat(),
                                               "status": "OPEN", "draw_type": "MANUAL"})
        self.assertFalse(form.is_valid())
        self.assertIn("rounds_drawn", form.errors)

    def test_removed_member_requests_rejoin_and_staff_approve(self):
        staff = make_user("Staff", staff=True)
        services.start_group(self.g)
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        operations.terminate(m, staff, reason="MEMBER_REQUEST")
        api = client_for(self.user)
        r = api.post(f"/api/equb/groups/{self.g.slug}/join/")
        self.assertEqual(r.status_code, 409)
        detail = api.get(f"/api/equb/groups/{self.g.slug}/").data
        self.assertTrue(detail["rejoin"]["needs_approval"])
        self.assertTrue(detail["rejoin"]["can_request"])
        r = api.post(f"/api/equb/groups/{self.g.slug}/request-join/", {"message": "I can pay now"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["rejoin"]["request"]["status"], "PENDING")
        self.assertEqual(api.post(f"/api/equb/groups/{self.g.slug}/request-join/").status_code, 409)  # only one pending
        from .models import JoinRequest
        self.client.force_login(staff)
        req = JoinRequest.objects.get()
        r = self.client.post("/console/equb/terminations/", {"action": "approve_rejoin", "join_request": req.pk}, follow=True)
        self.assertContains(r, "is back in the Equb")
        m.refresh_from_db()
        self.assertEqual(m.status, "ACTIVE")
        self.assertTrue(api.get(f"/api/equb/memberships/{m.id}/pay-options/").data["payable"])

    def test_member_cancels_pending_payment_and_late_success_is_still_credited(self):
        services.start_group(self.g)
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        api = client_for(self.user)
        r = api.post(f"/api/equb/memberships/{m.id}/pay/", {"rounds": 1, "provider": "mock"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        record = EqubPayment.objects.get(membership=m)
        r = api.post(f"/api/equb/memberships/{m.id}/cancel-payment/", {"payment_id": record.id}, format="json")
        self.assertEqual(r.data["status"], "CANCELLED")
        txn = PaymentTransaction.objects.get(pk=record.transaction_id)
        self.assertEqual(txn.status, "CANCELLED")
        # The member had actually finished paying on the provider page: the money is credited, not lost.
        from apps.payments.services import apply_status
        apply_status(txn.pk, "SUCCESS", amount=txn.amount)
        record.refresh_from_db()
        self.assertEqual(record.status, "PAID")

    def test_unpaid_payments_expire_after_the_configured_minutes(self):
        s = EqubSettings.load()
        s.pending_payment_minutes = 2
        s.save()
        services.start_group(self.g)
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        client_for(self.user).post(f"/api/equb/memberships/{m.id}/pay/", {"rounds": 1, "provider": "mock"}, format="json")
        record = EqubPayment.objects.get(membership=m)
        self.assertEqual(services.expire_pending_payments(), 0)  # still fresh
        EqubPayment.objects.filter(pk=record.pk).update(created_at=timezone.now() - timedelta(minutes=3))
        self.assertEqual(services.expire_pending_payments(), 1)
        record.refresh_from_db()
        self.assertEqual(record.status, "CANCELLED")


class AppContentTests(TestCase):
    def test_update_popup_normal_and_forced(self):
        from apps.core.models import AppRelease
        api = APIClient()
        self.assertFalse(api.get("/api/app/version/?platform=android&version=1.0.0").data["update_available"])
        AppRelease.objects.create(platform="android", version="1.2.0", kind="NORMAL", min_supported_version="1.1.0")
        r = api.get("/api/app/version/?platform=android&version=1.1.5").data
        self.assertTrue(r["update_available"]); self.assertFalse(r["force"])
        self.assertTrue(api.get("/api/app/version/?platform=android&version=1.0.9").data["force"])
        self.assertFalse(api.get("/api/app/version/?platform=android&version=1.2.0").data["update_available"])
        self.assertFalse(api.get("/api/app/version/?platform=ios&version=1.0.0").data["update_available"])

    def test_banners_in_dashboard_and_console(self):
        import tempfile
        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.test import override_settings
        from PIL import Image
        import io
        buf = io.BytesIO(); Image.new("RGB", (120, 52), "green").save(buf, "PNG")
        staff = make_user("Staff", staff=True)
        self.client.force_login(staff)
        with override_settings(MEDIA_ROOT=tempfile.mkdtemp()):
            r = self.client.post("/console/app/banners/new/", {
                "image": SimpleUploadedFile("b.png", buf.getvalue(), content_type="image/png"), "title": "Umrah 1448",
                "link_type": "EQUB", "sort_order": 0, "is_active": "on"})
            self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
            self.assertEqual(self.client.get("/console/app/").status_code, 200)
            data = APIClient().get("/api/home/dashboard/").data
            self.assertEqual(data["banners"][0]["title"], "Umrah 1448")
            self.assertTrue(data["banners"][0]["image"].startswith("http"))


class FollowUpFixesTests(TestCase):
    def setUp(self):
        self.user = make_user("Liya Haile")
        self.g = make_equb(name="Short", contribution_amount=Decimal("1"), frequency_days=3, max_members=2,
                           duration_value=2, duration_unit="DAYS", start_date=timezone.localdate())
        services.join_group(self.user, self.g)

    def test_group_service_fee_overrides_the_rules(self):
        self.assertEqual(self.g.service_fee_percent, EqubSettings.load().service_fee_percent)
        self.g.service_fee_override = Decimal("5")
        self.g.save()
        self.assertEqual(EqubGroup.objects.get(pk=self.g.pk).service_fee_percent, Decimal("5"))

    def test_equb_that_ended_early_reopens_so_members_pay_the_rest(self):
        services.start_group(self.g)
        # Simulate the old behaviour: finished after one round although the price per person is 2 payments.
        self.g.rounds.exclude(number=1).delete()
        self.g.rounds.filter(number=1).update(status="DRAWN")
        EqubGroup.objects.filter(pk=self.g.pk).update(status="COMPLETED")
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        data = client_for(self.user).get(f"/api/equb/memberships/{m.id}/").data
        self.assertEqual(data["group"]["status"], "ACTIVE")
        self.assertEqual(data["group"]["price_per_person"], "2.00")
        self.assertTrue(client_for(self.user).get(f"/api/equb/memberships/{m.id}/pay-options/").data["payable"])

    def test_cancelled_member_sees_rejoin_on_their_place(self):
        staff = make_user("Staff", staff=True)
        services.start_group(self.g)
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        operations.terminate(m, staff, reason="MEMBER_REQUEST")
        data = client_for(self.user).get(f"/api/equb/memberships/{m.id}/").data
        self.assertTrue(data["rejoin"]["can_request"])

    def test_sidebar_badges_match_pages(self):
        from apps.console.templatetags.console_tags import bust_equb_counts, equb_counts
        bust_equb_counts()
        counts = equb_counts()
        self.assertEqual(counts["uncalled"], 0)
        self.assertEqual(counts["terminations"], 0)
