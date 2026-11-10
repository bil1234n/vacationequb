from datetime import date, timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.core.exceptions import BusinessRuleError
from apps.core.testutils import make_equb, client_for, make_package, make_user, passenger, plenty_of_money
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


@plenty_of_money()
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


@plenty_of_money()
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


@plenty_of_money()
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
                         [timedelta(days=0), timedelta(days=1), timedelta(days=2)])  # Days → one round a day
        self.assertEqual((self.group.frequency, self.group.get_frequency_display(), self.group.every),
                         ("DAILY", "Daily", "every day"))
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

    @plenty_of_money()
    def test_still_to_pay_is_price_per_person_and_winners_keep_paying(self):
        m = EqubMembership.objects.get(group=self.g, user=self.user)
        self.assertEqual(self.g.price_per_person, Decimal("40"))  # 10 × 4
        self.assertEqual(st.standing_for(m).remaining_amount, Decimal("40"))
        services.start_group(self.g)
        self.g.refresh_from_db()
        self.assertEqual(self.g.rounds.count(), 4)  # 4 payments although only 2 draws
        self.assertEqual(services.draw_round_numbers(self.g), {2, 4})  # the draws are spread out
        services.close_round_without_winner(self.g.rounds.get(number=1))  # round 1 only collects
        services.run_draw(self.g.rounds.get(number=2))
        m.refresh_from_db()
        self.assertTrue(m.has_won)
        self.g.refresh_from_db()
        self.assertEqual(self.g.status, "ACTIVE")  # not completed: the winner still owes rounds 3-4
        self.assertEqual(self.g.current_round, 3)
        services.close_round_without_winner(self.g.rounds.get(number=3))
        self.g.refresh_from_db()
        self.assertEqual(services.run_draw(self.g.rounds.get(number=4)), [])  # everyone won: payment-only round closes
        self.assertTrue(Contribution.objects.filter(membership=m, round__number=4).exists())

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


class DrawMoneyRuleTests(TestCase):
    """Each winner receives the price per person, so the Equb must hold that much money per winner."""

    def setUp(self):
        self.staff = make_user("Staff", staff=True)
        self.users = [make_user(f"Member {i}") for i in range(6)]
        self.g = make_equb(name="Money", contribution_amount=Decimal("1"), frequency_days=3, max_members=8,
                           duration_value=3, duration_unit="DAYS", start_date=timezone.localdate())
        for u in self.users:
            services.join_group(u, self.g)
        rules = EqubSettings.load()
        rules.min_paid_rounds_to_win = 0
        rules.save()
        services.start_group(self.g)
        self.rnd = self.g.rounds.get(number=1)

    def pay(self, n):
        Contribution.objects.filter(pk__in=list(self.rnd.contributions.values_list("pk", flat=True)[:n])).update(status="PAID")

    def test_no_draw_until_one_full_prize_is_collected(self):
        self.pay(2)  # 2 ETB collected, prize is 3 ETB (1 × 3)
        f = services.equb_funds(self.g, self.rnd)
        self.assertEqual((f["available"], f["prize"], f["max_winners"]), (Decimal("2.00"), Decimal("3.00"), 0))
        with self.assertRaisesMessage(BusinessRuleError, "1 ETB more must be collected"):
            services.run_draw(self.rnd)

    def test_number_of_winners_is_capped_by_money_and_each_gets_price_per_person(self):
        self.pay(6)  # 6 ETB → money for 2 winners of 3 ETB
        payouts = services.run_draw(self.rnd, count=5)
        self.assertEqual(len(payouts), 2)
        self.assertTrue(all(p.gross_amount == Decimal("3.00") for p in payouts))
        self.assertEqual(services.equb_funds(self.g)["available"], Decimal("0.00"))

    def test_percentage_of_the_draw(self):
        self.pay(6)
        self.assertEqual(services.winners_wanted(6, percent=50), 3)
        self.assertEqual(services.winners_wanted(6, percent=10), 1)  # at least one
        payouts = services.run_draw(self.rnd, percent=Decimal("30"))  # 30% of 6 = 2
        self.assertEqual(len(payouts), 2)

    def test_manual_pick_more_than_money_allows_is_refused(self):
        self.pay(3)  # money for 1 winner
        ids = list(self.g.memberships.values_list("id", flat=True)[:2])
        with self.assertRaisesMessage(BusinessRuleError, "enough for 1 winner"):
            services.run_draw(self.rnd, self.staff, winner_ids=ids, exact=False)

    def test_close_round_without_winner_keeps_the_money(self):
        self.pay(2)
        services.close_round_without_winner(self.rnd, self.staff)
        self.g.refresh_from_db()
        self.assertEqual(self.g.current_round, 2)
        self.assertEqual(services.equb_funds(self.g)["available"], Decimal("2.00"))

    def test_console_draw_with_count_and_percent(self):
        self.pay(6)
        self.client.force_login(self.staff)
        r = self.client.get(f"/console/equb/draws/new/?round={self.rnd.pk}")
        self.assertContains(r, "Money for winners")
        r = self.client.post("/console/equb/draws/new/", {"method": "auto", "round": self.rnd.pk, "scope": "member",
                                                          "win_mode": "percent", "win_percent": "100"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.rnd.payouts.count(), 2)  # 100% of 6 asked, money for 2


class ChanceRulesAndPlannerTests(TestCase):
    def setUp(self):
        from apps.core import fairness  # noqa: F401
        self.staff = make_user("Staff", staff=True)
        self.users = [make_user(f"M{i}") for i in range(4)]
        self.g = make_equb(name="Chance", contribution_amount=Decimal("1"), frequency_days=3, max_members=8,
                           duration_value=4, duration_unit="DAYS", start_date=timezone.localdate())
        for u in self.users:
            services.join_group(u, self.g)
        rules = EqubSettings.load()
        rules.min_paid_rounds_to_win = 0
        rules.save()
        services.start_group(self.g)
        self.rnd = self.g.rounds.get(number=1)
        slips = list(self.rnd.contributions.order_by("membership__position"))
        on_time = timezone.now()
        late = timezone.make_aware(timezone.datetime.combine(self.rnd.due_date + timedelta(days=1),
                                                             timezone.datetime.min.time())) + timedelta(hours=10)
        Contribution.objects.filter(pk__in=[slips[0].pk, slips[1].pk]).update(status="PAID", paid_at=on_time)
        Contribution.objects.filter(pk=slips[2].pk).update(status="PAID", paid_at=late)
        Contribution.objects.filter(pk=slips[3].pk).update(status="PAID", paid_at=on_time)
        # member 0 also paid round 2 in advance
        EqubMembership.objects.filter(pk=slips[0].membership_id).update(prepaid_credit=Decimal("1"))

    def test_weights_follow_the_rules(self):
        pool = services.draw_pool(self.rnd)
        w = {e["position"]: e["weight"] for e in pool["entries"]}
        self.assertEqual(w[1], 160)  # on time +50, in advance +10
        self.assertEqual(w[2], 150)  # on time
        self.assertEqual(w[3], 50)   # late −50
        self.assertAlmostEqual(sum(e["share"] for e in pool["entries"]), 100, delta=0.5)
        self.assertGreater(pool["entries"][0]["odds"], pool["entries"][2]["odds"])

    def test_rules_off_means_equal_chance(self):
        rules = EqubSettings.load()
        rules.weighted_draw = False
        rules.save()
        self.assertEqual({e["weight"] for e in services.draw_pool(self.rnd)["entries"]}, {100})

    def test_weighted_draw_is_verifiable(self):
        with plenty_of_money():
            services.run_draw(self.rnd, count=2)
        self.rnd.refresh_from_db()
        self.assertEqual(self.rnd.draw.mode, "WEIGHTED")
        self.assertEqual(len(self.rnd.draw.weights), 4)
        v = services.verify_draw(self.rnd)
        self.assertTrue(v["matches"])
        self.assertTrue(v["commitment_ok"])

    def test_weighted_pick_favours_heavier_entries(self):
        from apps.core import fairness
        wins = [0, 0]
        for i in range(400):
            idx = fairness.pick_weighted(fairness.new_seed(), f"t{i}", [300, 100], 1)[0]
            wins[idx] += 1
        self.assertGreater(wins[0], wins[1] * 2)  # ~75% vs 25%

    def test_planner_sets_dates_and_they_survive_a_sync(self):
        new_draw = self.g.rounds.get(number=2).draw_date + timedelta(days=5)
        r2 = self.g.rounds.get(number=2)
        ops = operations.plan_rounds(self.g, self.staff, {2: (r2.due_date + timedelta(days=5), new_draw)},
                                     shift_following=True)
        self.assertIn(2, ops)
        self.assertIn(3, ops)  # later rounds moved too
        services.sync_running_group(self.g)  # e.g. someone joins late – hand-set dates stay
        self.assertEqual(self.g.rounds.get(number=2).draw_date, new_draw)
        with self.assertRaises(BusinessRuleError):
            operations.plan_rounds(self.g, self.staff, {3: (r2.due_date, r2.due_date - timedelta(days=1))})
        self.assertGreaterEqual(operations.reset_round_dates(self.g), 1)
        self.assertFalse(self.g.rounds.get(number=2).dates_fixed)

    def test_console_pages(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(f"/console/equb/{self.g.slug}/rounds/"), "Round dates")
        self.assertContains(self.client.get("/console/equb/draws/?tab=members"), "Draw members")
        self.assertContains(self.client.get("/console/equb/draws/chance/"), "Example draw")
        r = self.client.post("/console/equb/draws/chance/", {"weighted_draw": "on", "chance_on_time_percent": 40,
                             "chance_prepaid_percent": 10, "chance_streak_percent": 5, "chance_streak_cap_percent": 25,
                             "chance_late_percent": 50, "chance_missed_percent": 25, "chance_min_percent": 10})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(EqubSettings.load().chance_on_time_percent, 40)
        r3 = self.g.rounds.get(number=3)
        data = {"action": "save", "shift": "on"}  # daily rounds: the later ones move with it
        for r in self.g.rounds.exclude(status="DRAWN"):
            data[f"due_{r.number}"] = r.due_date.isoformat()
            data[f"draw_{r.number}"] = (r.draw_date + timedelta(days=2 if r.number == 3 else 0)).isoformat()
        self.client.post(f"/console/equb/{self.g.slug}/rounds/", data)
        self.assertEqual(self.g.rounds.get(number=3).draw_date, r3.draw_date + timedelta(days=2))


@plenty_of_money()
class RoundsDrawnFewerThanPaymentsTests(TestCase):
    """12 places, 7 payments, Rounds drawn = 2 → 7 rounds, but only rounds 4 and 7 have a draw."""

    def setUp(self):
        self.users = [make_user(f"P{i}") for i in range(3)]
        self.g = make_equb(name="Seven pay two draws", contribution_amount=Decimal("1"), frequency_days=7, max_members=12,
                           duration_value=7, duration_unit="DAYS", rounds_drawn=2, start_date=timezone.localdate())
        for u in self.users:
            services.join_group(u, self.g)
        rules = EqubSettings.load()
        rules.min_paid_rounds_to_win = 0
        rules.save()
        services.start_group(self.g)
        self.g.refresh_from_db()

    def _pay_current(self):
        rnd = self.g.rounds.get(status="COLLECTING")
        rnd.contributions.update(status="PAID", paid_at=timezone.now())
        return rnd

    def test_only_the_rounds_drawn_have_a_draw(self):
        self.assertEqual(self.g.rounds.count(), 7)
        self.assertEqual(services.draw_round_numbers(self.g), {4, 7})
        self.assertEqual(self.g.winners_per_round, 6)
        rnd = self._pay_current()
        with self.assertRaises(BusinessRuleError):
            services.run_draw(rnd)
        services.close_round_without_winner(rnd)  # closes as a payment-only round
        rnd.refresh_from_db()
        self.assertIn("Payment-only", rnd.draw_note)
        for _ in range(2):
            services.close_round_without_winner(self._pay_current())
        rnd4 = self._pay_current()
        self.assertEqual(rnd4.number, 4)
        self.assertTrue(services.is_draw_round(rnd4))
        self.assertTrue(services.run_draw(rnd4))
        self.g.refresh_from_db()
        self.assertEqual(services.draw_round_numbers(self.g), {4, 7})

    def test_api_and_schedule_say_which_rounds_have_a_draw(self):
        m = self.g.memberships.get(user=self.users[0])
        rows = st.schedule_for(m)
        self.assertEqual([r["round"] for r in rows if r["has_draw"]], [4, 7])
        data = client_for(self.users[0]).get(f"/api/equb/memberships/{m.id}/").data
        self.assertFalse(data["current_round"]["has_draw"])
        fee = Decimal(self.g.service_fee_percent)
        self.assertEqual(Decimal(data["group"]["payout_per_winner"]), (Decimal("7") * (1 - fee / 100)).quantize(Decimal("0.01")))


class TermsDocumentTests(TestCase):
    TEXT = ("# Vacation Equb Agreement\nPAYMENT TERMS\n1. Members pay **on time** every round.\n1.1 Late payers lose chance.\n"
            "- Chapa only\n\nThe winner travels within a year.\n<script>x</script>")

    def test_parse_blocks_and_html(self):
        from . import terms
        types = [b["type"] for b in terms.parse(self.TEXT)]
        self.assertEqual(types, ["title", "heading", "clause", "clause", "bullet", "paragraph", "paragraph"])
        doc = terms.document(self.TEXT)
        self.assertEqual(doc["blocks"][3]["level"], 2)
        self.assertGreater(doc["words"], 15)
        html = terms.to_html(self.TEXT)
        self.assertIn("<strong>on time</strong>", html)
        self.assertNotIn("<script>", html)
        self.assertEqual(terms.lines(self.TEXT)[2], "Members pay on time every round.")

    def test_api_returns_the_document(self):
        g = make_equb(name="Terms", contribution_amount=Decimal("10"), max_members=4, frequency_days=7,
                       start_date=timezone.localdate(), rules_text=self.TEXT)
        data = APIClient().get(f"/api/equb/groups/{g.slug}/").data
        self.assertEqual(data["terms_document"]["blocks"][0]["text"], "Vacation Equb Agreement")
        self.assertIn("Payment Terms", data["terms_document"]["headings"])


class TermsRichStylesTests(TestCase):
    def test_inline_and_block_styles(self):
        from . import terms
        text = ("### Travel\n!! Read **carefully**\n> quoted\n:: centred\n---\n[sign] Member\n  - sub **__both__**\n"
                "a) first\nA. Smith agrees\nPay ==on time==, *never* ~~late~~ and 2*3*4 stays.")
        blocks = terms.parse(text)
        self.assertEqual([b["type"] for b in blocks],
                         ["subheading", "notice", "quote", "center", "divider", "signature", "bullet", "letter",
                          "paragraph", "paragraph"])
        self.assertEqual(blocks[6]["level"], 2)
        self.assertEqual(blocks[6]["spans"][1], {"t": "both", "b": True, "u": True})
        styles = {k for s in blocks[-1]["spans"] for k in s if k != "t"}
        self.assertEqual(styles, {"h", "i", "s"})
        self.assertIn("2*3*4 stays", blocks[-1]["text"])
        html = terms.to_html(text)
        for tag in ("<mark>on time</mark>", "<em>never</em>", "<s>late</s>", "<u><strong>both</strong></u>", 'class="terms-notice"', "terms-sign"):
            self.assertIn(tag, html)

    def test_amharic_agreement(self):
        from . import terms
        text = "አንቀጽ 1፡ የሸሪአ መርሆች\n1.1 የሸሪአ ተገዢነት፡- ይህ ዕቁብ\nሀ. ከሪባ መራቅ፡- ማንኛውም\nለ) ከጋራር መራቅ"
        blocks = terms.parse(text)
        self.assertEqual([b["type"] for b in blocks], ["heading", "clause", "letter", "letter"])
        self.assertEqual((blocks[2]["label"], blocks[3]["label"]), ("ሀ.", "ለ)"))
        self.assertEqual(blocks[1]["spans"][0], {"t": "የሸሪአ ተገዢነት", "b": True})  # lead term in bold


@plenty_of_money()
class LiveDrawTests(TestCase):
    def test_members_get_the_draw_to_replay(self):
        users = [make_user(f"L{i}") for i in range(3)]
        g = make_equb(name="Live", contribution_amount=Decimal("2"), frequency_days=3, max_members=6, duration_value=3,
                      duration_unit="DAYS", start_date=timezone.localdate())
        for u in users:
            services.join_group(u, g)
        rules = EqubSettings.load()
        rules.min_paid_rounds_to_win = 0
        rules.save()
        services.start_group(g)
        rnd = g.rounds.get(number=1)
        rnd.contributions.update(status="PAID", paid_at=timezone.now())
        self.assertEqual(client_for(users[0]).get("/api/equb/memberships/live-draws/").data["results"], [])
        services.run_draw(rnd, count=1)
        data = client_for(users[0]).get("/api/equb/memberships/live-draws/").data["results"]
        self.assertEqual(len(data), 1)
        d = data[0]
        self.assertEqual((d["round"], len(d["entries"]), len(d["winners"])), (1, 3, 1))
        self.assertEqual(sum(1 for e in d["entries"] if e["is_me"]), 1)
        self.assertEqual(Decimal(d["winners"][0]["amount"]), Decimal("6.00"))  # the price per person
        self.assertEqual(client_for(make_user("Outsider")).get("/api/equb/memberships/live-draws/").data["results"], [])


@plenty_of_money()
class EveryoneWinsTests(TestCase):
    """An Equb is never complete while a member is still waiting to win – nobody loses their money."""

    def setUp(self):
        self.a, self.b = make_user("Ab"), make_user("Be")
        self.g = make_equb(name="Everyone wins", contribution_amount=Decimal("1"), frequency_days=1, max_members=12,
                           duration_value=2, duration_unit="DAYS", start_date=timezone.localdate())
        for u in (self.a, self.b):
            services.join_group(u, self.g)
        rules = EqubSettings.load()
        rules.min_paid_rounds_to_win = 0
        rules.save()
        services.start_group(self.g)

    def _pay(self, rnd):
        rnd.contributions.update(status="PAID", paid_at=timezone.now())

    def test_extra_draw_round_until_everyone_has_won(self):
        r1 = self.g.rounds.get(number=1)
        self._pay(r1)
        services.close_round_without_winner(r1, note="Not enough money")
        r2 = self.g.rounds.get(number=2)
        self._pay(r2)
        services.run_draw(r2, count=1)
        self.g.refresh_from_db()
        self.assertEqual(self.g.status, "ACTIVE")  # one member still waiting
        r3 = self.g.rounds.get(number=3)
        self.assertTrue(r3.extra_draw)
        self.assertEqual(r3.status, "COLLECTING")
        self.assertEqual(r3.contributions.count(), 0)  # nobody is billed again
        waiting = self.g.memberships.get(has_won=False)
        entry = next(e for e in services.draw_pool(r3)["entries"] if e["membership_id"] == waiting.id)
        self.assertTrue(entry["eligible"], entry["reasons"])  # paid in full → in the draw, no "round 3 not paid"
        st_ = st.standing_for(waiting)
        self.assertEqual((st_.paid_rounds, st_.total_rounds), (2, 2))  # 2/2, not 2/3
        self.assertEqual(Decimal(st_.owes), 0)
        row = st.schedule_for(waiting)[-1]
        self.assertEqual((row["extra_draw"], row["amount"], row["has_draw"]), (True, "0.00", True))
        self.assertTrue(st.schedule_for(waiting)[0]["has_draw"])  # round 1 keeps its draw label
        self.assertFalse(services.payment_plan(waiting)["payable"])  # nothing more to pay
        payouts = services.run_draw(r3)  # the last draw: everyone still waiting wins
        self.assertEqual(len(payouts), 1)
        self.g.refresh_from_db()
        self.assertEqual(self.g.status, "COMPLETED")
        self.assertFalse(self.g.memberships.filter(has_won=False).exists())

    def test_wrongly_completed_equb_is_reopened(self):
        r1, r2 = self.g.rounds.get(number=1), self.g.rounds.get(number=2)
        self._pay(r1)
        services.run_draw(r1, count=1)
        self._pay(r2)
        services.close_round_without_winner(r2, note="x")
        EqubRound.objects.filter(group=self.g, number__gt=2).delete()
        EqubGroup.objects.filter(pk=self.g.pk).update(status="COMPLETED", current_round=2)  # the old behaviour
        g = services.reopen_if_short(EqubGroup.objects.get(pk=self.g.pk))
        self.assertEqual(g.status, "ACTIVE")
        self.assertTrue(g.rounds.get(number=3).extra_draw)
