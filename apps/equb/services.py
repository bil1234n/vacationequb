"""
Equb engine: membership, round scheduling, contributions & payments, provably-fair draws, payouts.

Staff-side operations (call centre, terminations, exit requests, reminders) live in
``operations.py``; member standing in ``standing.py``; receivables & reports in ``reports.py``.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, F, Max, Sum
from django.utils import timezone

from apps.core import fairness
from apps.core.exceptions import BusinessRuleError
from apps.core.utils import generate_code
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.payments.models import PaymentTransaction
from apps.payments.services import initiate_payment

from . import standing as standing_mod
from .models import (Contribution, EqubDraw, EqubGroup, EqubMembership, EqubPayment, EqubPayout, EqubRound,
                     EqubSettings, add_months, is_monthly)

log = logging.getLogger(__name__)
ZERO = Decimal("0")
CENT = Decimal("0.01")


def money(v) -> str:
    v = Decimal(v or 0)
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"


def due_date_for(group: EqubGroup, number: int) -> date:
    """Round 1 is due on the start date; then every ``frequency_days`` (calendar months for ~30 days)."""
    offset = number - 1
    if is_monthly(group.frequency_days):
        return add_months(group.start_date, offset)
    return group.start_date + timedelta(days=group.frequency_days * offset)


# --- membership -------------------------------------------------------------
@transaction.atomic
def join_group(user, group: EqubGroup, *, team=None, dependent: dict | None = None, auto_start: bool = True,
               notify_member: bool = True) -> EqubMembership:
    """
    Give ``user`` a place in ``group``. With ``dependent`` ({name, phone}) the place is for a
    responsibility person whom ``user`` pays for. ``team`` links the place to a Group Equb.
    """
    group = EqubGroup.objects.select_for_update().get(pk=group.pk)
    if group.status not in (EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE):
        raise BusinessRuleError("This Equb is not accepting new members.")
    if group.members_count >= group.max_members:
        raise BusinessRuleError("This Equb is full.")
    position = (group.memberships.aggregate(m=Max("position"))["m"] or 0) + 1
    if dependent:
        name = (dependent.get("name") or "").strip()
        if not name:
            raise BusinessRuleError("Enter the responsibility person's name.")
        membership = EqubMembership.objects.create(
            group=group, user=user, position=position, team=team, dependent_name=name[:120],
            dependent_phone=(dependent.get("phone") or "")[:20])
        who = f"{membership.dependent_name} holds place #{membership.position} (you pay for them)."
    else:
        existing = group.memberships.filter(user=user, dependent_name="").first()
        if existing and existing.status == EqubMembership.Status.DEFAULTED:
            raise BusinessRuleError("Your place in this Equb was cancelled. Send a request to join again – "
                                    "our team will review it.")
        if existing and existing.status != EqubMembership.Status.LEFT:
            raise BusinessRuleError("You are already a member of this Equb.")
        if existing and team is None:
            from .operations import previous_place

            if previous_place(user, group) is not None:
                raise BusinessRuleError("You left this Equb after it started. Send a request to join again – "
                                        "our team will review it.")
        if existing:  # re-joining after leaving before start
            existing.status = EqubMembership.Status.ACTIVE
            existing.ended_at = None
            existing.team = team or existing.team
            existing.save(update_fields=["status", "ended_at", "team", "updated_at"])
            membership = existing
        else:
            membership = EqubMembership.objects.create(group=group, user=user, position=position, team=team)
        who = f"You hold place #{membership.position}."
    if group.status == EqubGroup.Status.ACTIVE:
        # Joining a running Equb: a slip for the round being collected now, and more rounds if they are needed.
        sync_running_group(group)
    if notify_member:
        notify(user, Notification.Kind.EQUB_REMINDER, f"Welcome to {group.name}",
               f"{who} Contributions of {money(group.contribution_amount)} ETB start when the Equb begins "
               f"({group.members_count}/{group.max_members} joined).",
               {"equb": group.slug, "membership": membership.id})
    if (auto_start and group.status == EqubGroup.Status.OPEN and group.auto_start_when_full
            and group.members_count >= group.max_members):
        start_group(group)
    return membership


def join_by_code(user, code: str) -> EqubMembership:
    """An Equb's join code, or a Group Equb's invite code (the person joins the Equb as part of that group)."""
    from .models import GroupEqub

    code = (code or "").strip().upper()
    if not code:
        raise BusinessRuleError("Enter the code you were given.")
    team = GroupEqub.objects.select_related("equb").filter(invite_code=code, is_active=True).first()
    if team:
        return join_group(user, team.equb, team=team)
    group = EqubGroup.objects.filter(join_code=code).first()
    if not group:
        raise BusinessRuleError("No Equb was found with that code. Check it and try again.")
    return join_group(user, group)


@transaction.atomic
def leave_group(user, group: EqubGroup):
    membership = (EqubMembership.objects.select_for_update().filter(group=group, user=user, dependent_name="")
                  .exclude(status=EqubMembership.Status.LEFT).first())
    if not membership:
        raise BusinessRuleError("You are not a member of this Equb.")
    if group.status not in (EqubGroup.Status.OPEN, EqubGroup.Status.DRAFT):
        raise BusinessRuleError("This Equb has started, so you can't leave from the app. "
                                "Send an exit request from your membership page and our team will contact you.")
    membership.status = EqubMembership.Status.LEFT
    membership.ended_at = timezone.now()
    membership.save(update_fields=["status", "ended_at", "updated_at"])


def round_count(group: EqubGroup, members: int | None = None) -> int:
    """How many rounds the Equb runs: the "Rounds drawn" staff set, else one round per winner group."""
    if group.rounds_drawn:
        draws = group.rounds_drawn
    else:
        members = group.members_count if members is None else members
        draws = -(-(members or group.max_members) // group.winners_per_round)
    # Everyone pays the full price per person (contribution × duration value), winners included, so the Equb
    # runs for as many rounds as there are payments; rounds after the last winner are payment-only.
    return max(draws, group.payment_rounds)


def draw_round_numbers(group: EqubGroup, rounds: list | None = None) -> set[int] | None:
    """
    Which rounds have a draw when staff set "Rounds drawn" lower than the number of payments
    (7 payments, 2 rounds drawn → draws in rounds 4 and 7; the other rounds only collect money).
    The draws still to come are spread evenly over the rounds still to come, the last one always in the final round,
    so the money builds up for the winners. ``None`` means every round has a draw.
    """
    if not group.rounds_drawn:
        return None
    rounds = list(group.rounds.select_related("draw").order_by("number")) if rounds is None else rounds
    if not rounds or group.rounds_drawn >= len(rounds):
        return None
    done = {r.number for r in rounds if r.status == EqubRound.Status.DRAWN
            and getattr(getattr(r, "draw", None), "executed_at", None)}
    remaining = [r.number for r in rounds if r.status != EqubRound.Status.DRAWN]
    left = max(group.rounds_drawn - len(done), 0)
    if left >= len(remaining):
        return done | set(remaining)

    def spread(numbers: list[int], k: int) -> set[int]:
        n = len(numbers)
        return {numbers[n - 1 - (j * n) // k] for j in range(k)} if k else set()

    # The plan over all rounds stays fixed; it is only spread again over the rounds left when a planned draw
    # was closed without a winner (e.g. not enough money yet).
    planned = spread([r.number for r in rounds], group.rounds_drawn) & set(remaining)
    return done | (planned if len(planned) == left else spread(remaining, left))


def is_draw_round(rnd: EqubRound) -> bool:
    numbers = draw_round_numbers(rnd.group)
    return numbers is None or rnd.number in numbers


def _schedule_rounds(group: EqubGroup, wanted: int, members: int) -> None:
    """
    Make the round schedule match ``wanted`` rounds without ever duplicating a round number:
    missing rounds are added, extra rounds nobody has paid into are removed, upcoming dates follow the schedule.
    """
    have = set(group.rounds.values_list("number", flat=True))
    for n in range(1, wanted + 1):
        if n in have:
            continue
        due = due_date_for(group, n)
        EqubRound.objects.get_or_create(group=group, number=n, defaults={
            "due_date": due, "draw_date": due + timedelta(days=group.grace_days),
            "expected_amount": group.contribution_amount * members})
    (group.rounds.filter(number__gt=wanted, status=EqubRound.Status.UPCOMING)
     .exclude(contributions__isnull=False).delete())
    for rnd in group.rounds.filter(status=EqubRound.Status.UPCOMING, dates_fixed=False):
        due = due_date_for(group, rnd.number)
        rnd.due_date, rnd.draw_date = due, due + timedelta(days=group.grace_days)
        rnd.expected_amount = group.contribution_amount * members
        rnd.save(update_fields=["due_date", "draw_date", "expected_amount", "updated_at"])


@transaction.atomic
def start_group(group: EqubGroup) -> EqubGroup:
    group = EqubGroup.objects.select_for_update().get(pk=group.pk)
    if group.status != EqubGroup.Status.OPEN:
        raise BusinessRuleError("Only open Equbs can be started.")
    members = group.members_count  # an Equb can start with any number of members – even none yet
    group.status = EqubGroup.Status.ACTIVE
    if group.start_date < timezone.localdate() and not group.rounds.exclude(status=EqubRound.Status.UPCOMING).exists():
        group.start_date = timezone.localdate()
    group.save()
    # A group that was started before (and set back to Open) keeps its rounds – they are reused, never re-created.
    rounds = max(round_count(group, members), group.current_round or 1)
    _schedule_rounds(group, rounds, members)
    current = (group.rounds.filter(status=EqubRound.Status.COLLECTING).order_by("number").first()
               or open_round(group.rounds.filter(status=EqubRound.Status.UPCOMING).order_by("number").first()
                             or group.rounds.get(number=1)))
    for m in group.memberships.filter(status=EqubMembership.Status.ACTIVE).select_related("user"):
        notify(m.user, Notification.Kind.EQUB_REMINDER, f"{group.name} has started",
               f"{rounds} rounds, one draw {group.every}. Round {current.number} contribution of "
               f"{money(group.contribution_amount)} ETB is due {current.due_date:%d %b}. You can pay now.",
               {"equb": group.slug, "membership": m.id})
    return group


def sync_running_group(group: EqubGroup) -> EqubGroup:
    """
    Keep a running Equb in step after staff edit it or someone joins late: the number of rounds staff set
    ("Rounds drawn"), upcoming rounds on the (new) schedule, and unpaid slips of the current round at the (new) amount.
    """
    group = EqubGroup.objects.get(pk=group.pk)
    if group.status != EqubGroup.Status.ACTIVE:
        return group
    members = group.members_count
    drawn = group.rounds.exclude(status=EqubRound.Status.UPCOMING).aggregate(m=Max("number"))["m"] or 0
    wanted = max(round_count(group, members), group.current_round or 1, drawn)
    _schedule_rounds(group, wanted, members)
    rnd = group.rounds.filter(status=EqubRound.Status.COLLECTING).first()
    if rnd:
        live = list(_active_members(group).exclude(contributions__round=rnd))
        Contribution.objects.bulk_create([Contribution(round=rnd, membership=m, amount=group.contribution_amount)
                                          for m in live], ignore_conflicts=True)
        rnd.contributions.filter(status__in=Contribution.UNSETTLED).exclude(
            amount=group.contribution_amount).update(amount=group.contribution_amount)
        rnd.expected_amount = sum(rnd.contributions.values_list("amount", flat=True), Decimal("0"))
        rnd.save(update_fields=["expected_amount", "updated_at"])
        for m in live:
            if m.prepaid_credit and m.prepaid_credit > 0:
                apply_credit(m)
    elif not group.rounds.exclude(status=EqubRound.Status.UPCOMING).exists() and group.rounds.exists():
        open_round(group.rounds.order_by("number").first())
    return group


def _active_members(group):
    return group.memberships.filter(status__in=EqubMembership.LIVE_STATUSES)


def open_round(rnd: EqubRound) -> EqubRound:
    """Create contribution slips, apply members' credit and publish the seed commitment for the draw."""
    group = rnd.group
    members = list(_active_members(group))
    Contribution.objects.bulk_create(
        [Contribution(round=rnd, membership=m, amount=group.contribution_amount) for m in members],
        ignore_conflicts=True,
    )
    rnd.expected_amount = group.contribution_amount * rnd.contributions.count()
    # Advance payments and lucky-draw prizes pay this round straight away.
    for m in members:
        if m.prepaid_credit and m.prepaid_credit > 0:
            apply_credit(m)
    seed = fairness.new_seed()
    EqubDraw.objects.get_or_create(round=rnd, defaults={"server_seed": seed, "seed_commitment": fairness.commitment_for(seed)})
    rnd.status = EqubRound.Status.COLLECTING
    rnd.save(update_fields=["status", "expected_amount", "updated_at"])
    EqubGroup.objects.filter(pk=group.pk).update(current_round=rnd.number)
    return rnd


# --- contributions ----------------------------------------------------------
def contribution_for(user, rnd: EqubRound) -> Contribution:
    c = Contribution.objects.select_related("membership", "round__group").filter(round=rnd, membership__user=user).first()
    if not c:
        raise BusinessRuleError("You have no contribution due for this round.")
    return c


def start_contribution_payment(user, rnd: EqubRound, provider=None, payer_phone="") -> PaymentTransaction:
    """Pay one round's slip (kept for older app versions; new apps use ``start_member_payment``)."""
    if rnd.status != EqubRound.Status.COLLECTING:
        raise BusinessRuleError("This round is not collecting contributions.")
    c = contribution_for(user, rnd)
    if c.status in Contribution.SETTLED:
        raise BusinessRuleError("This contribution is already settled.")
    apply_late_fee(c)
    return initiate_payment(user=user, target=c, amount=c.amount_due, purpose=PaymentTransaction.Purpose.EQUB_CONTRIBUTION,
                            provider=provider, payer_phone=payer_phone)


def apply_late_fee(c: Contribution):
    group = c.round.group
    if c.status == Contribution.Status.PENDING and timezone.localdate() > c.round.due_date + timedelta(days=group.grace_days):
        c.status = Contribution.Status.LATE
        fields = ["status", "updated_at"]
        if group.late_fee and not c.late_fee_waived:
            c.late_fee = group.late_fee
            fields.append("late_fee")
        c.save(update_fields=fields)
        EqubMembership.objects.filter(pk=c.membership_id).update(missed_count=F("missed_count") + 1)
        return True
    return False


def mark_contribution_paid(c: Contribution, txn: PaymentTransaction | None = None, record: EqubPayment | None = None,
                           *, status: str = Contribution.Status.PAID, note: str = "") -> Contribution:
    with transaction.atomic():
        c = Contribution.objects.select_for_update().get(pk=c.pk)
        if c.status in Contribution.SETTLED:
            return c
        c.status = status
        c.paid_at = timezone.now()
        if txn:
            c.payment = txn
        if record:
            c.payment_record = record
        if note:
            c.note = note[:160]
        c.save()
        EqubRound.objects.filter(pk=c.round_id).update(collected_amount=F("collected_amount") + c.amount)
        EqubMembership.objects.filter(pk=c.membership_id).update(total_paid=F("total_paid") + c.amount_due)
    return c


@transaction.atomic
def apply_credit(m: EqubMembership) -> int:
    """Settle unpaid rounds (oldest first) from the member's credit. Returns how many rounds it paid."""
    m = EqubMembership.objects.select_for_update().get(pk=m.pk)
    credit = Decimal(m.prepaid_credit or 0)
    paid = 0
    for c in (Contribution.objects.select_related("round__group").filter(membership=m, status__in=Contribution.UNSETTLED)
              .order_by("round__number")):
        apply_late_fee(c)
        if credit < c.amount_due:
            break
        mark_contribution_paid(c, status=Contribution.Status.WAIVED, note="Paid from your credit")
        credit -= c.amount_due
        paid += 1
    if paid:
        EqubMembership.objects.filter(pk=m.pk).update(prepaid_credit=credit)
    return paid


def on_contribution_paid(txn):
    target = txn.target
    if isinstance(target, EqubPayment):
        record = allocate_payment(target, txn)
        _notify_payment(record)
        return
    c = mark_contribution_paid(target, txn)
    after_payment(c.membership)
    notify(c.membership.user, Notification.Kind.PAYMENT, "Contribution received ✅",
           f"{money(c.amount_due)} ETB received for {c.round.group.name} – round {c.round.number}. You're in the draw!",
           {"equb": c.round.group.slug, "round": c.round.number, "membership": c.membership_id})


def on_contribution_failed(txn):
    target = txn.target
    if isinstance(target, EqubPayment) and target.status == EqubPayment.Status.PENDING:
        target.status = EqubPayment.Status.CANCELLED if txn.status == PaymentTransaction.Status.CANCELLED \
            else EqubPayment.Status.FAILED
        target.save(update_fields=["status", "updated_at"])


def sweep_late_contributions() -> int:
    """Mark contributions past due + grace as LATE (adds the pool's late fee). Notices go out via escalation."""
    n = 0
    for c in Contribution.objects.filter(status=Contribution.Status.PENDING, round__status=EqubRound.Status.COLLECTING
                                         ).select_related("round__group", "membership__user"):
        if apply_late_fee(c):
            n += 1
    return n


# --- payments (multi-round, office) ----------------------------------------------------
def payment_plan(m: EqubMembership, rules: EqubSettings | None = None, today: date | None = None) -> dict:
    """
    What paying N rounds costs right now. Owed rounds come first (oldest first, late fees included),
    then future rounds up to the advance limit. Existing credit is deducted.
    """
    rules = rules or EqubSettings.load()
    today = today or timezone.localdate()
    group = m.group
    contribution = Decimal(group.contribution_amount)
    unpaid = list(Contribution.objects.select_related("round").filter(membership=m, status__in=Contribution.UNSETTLED)
                  .order_by("round__number"))
    items = []
    for c in unpaid:
        fee = standing_mod.pending_fee(c, group, today)
        overdue = today > c.round.due_date + timedelta(days=group.grace_days)
        items.append({"round": c.round.number, "due_date": c.round.due_date.isoformat(), "amount": c.amount,
                      "late_fee": fee, "kind": "overdue" if overdue else "due"})
    payable = m.is_live and group.status == EqubGroup.Status.ACTIVE
    if payable:
        upcoming = list(group.rounds.filter(status=EqubRound.Status.UPCOMING).order_by("number")
                        .values_list("number", "due_date")[: rules.max_advance_rounds])
        for number, due in upcoming:
            items.append({"round": number, "due_date": due.isoformat(), "amount": contribution, "late_fee": ZERO,
                          "kind": "advance"})
    credit = Decimal(m.prepaid_credit or 0)
    options, running = [], ZERO
    for i, it in enumerate(items, start=1):
        running += it["amount"] + it["late_fee"]
        amount = (running - credit).quantize(CENT)
        if amount <= 0:
            continue
        kinds = {x["kind"] for x in items[:i]}
        options.append({
            "rounds": i, "amount": str(amount), "from_round": items[0]["round"], "to_round": it["round"],
            "includes_overdue": "overdue" in kinds, "includes_advance": "advance" in kinds,
        })
    owed = sum((it["amount"] + it["late_fee"] for it in items if it["kind"] != "advance"), ZERO)
    return {
        "membership_id": m.id, "payable": payable and bool(options), "contribution": str(contribution),
        "credit": str(credit.quantize(CENT)), "owed": str(max(owed - credit, ZERO).quantize(CENT)),
        "overdue_count": sum(1 for it in items if it["kind"] == "overdue"),
        "items": [{**it, "amount": str(it["amount"]), "late_fee": str(Decimal(it["late_fee"]).quantize(CENT))} for it in items],
        "options": options, "max_advance_rounds": rules.max_advance_rounds,
        "reason": None if payable else ("Payments open once the Equb starts." if m.is_live else "This place is no longer active."),
    }


def start_member_payment(user, m: EqubMembership, rounds: int, provider=None, payer_phone="") -> PaymentTransaction:
    if m.user_id != user.id:
        raise BusinessRuleError("You can only pay for your own Equb place.")
    plan = payment_plan(m)
    if not plan["payable"]:
        raise BusinessRuleError(plan["reason"] or "Nothing to pay right now.")
    option = next((o for o in plan["options"] if o["rounds"] == int(rounds)), None)
    if not option:
        lo, hi = plan["options"][0]["rounds"], plan["options"][-1]["rounds"]
        raise BusinessRuleError(f"Choose between {lo} and {hi} rounds.")
    amount = Decimal(option["amount"])
    provider = provider or None
    # Re-use an in-flight attempt instead of creating duplicates on double taps.
    pending = (EqubPayment.objects.filter(membership=m, status=EqubPayment.Status.PENDING, amount=amount,
                                          rounds_requested=option["rounds"], transaction__isnull=False)
               .select_related("transaction").order_by("-created_at").first())
    if pending and pending.transaction.status in (PaymentTransaction.Status.INITIATED, PaymentTransaction.Status.PENDING) \
            and pending.transaction.checkout_url and (not provider or pending.transaction.provider == provider):
        return pending.transaction
    record = EqubPayment.objects.create(membership=m, user=user, amount=amount, rounds_requested=option["rounds"],
                                        method=provider or "telebirr")
    try:
        txn = initiate_payment(user=user, target=record, amount=amount, purpose=PaymentTransaction.Purpose.EQUB_CONTRIBUTION,
                               provider=provider, payer_phone=payer_phone)
    except BusinessRuleError:
        record.status = EqubPayment.Status.FAILED
        record.save(update_fields=["status", "updated_at"])
        raise
    record.transaction = txn
    record.method = txn.provider
    record.save(update_fields=["transaction", "method", "updated_at"])
    return txn


@transaction.atomic
def allocate_payment(record: EqubPayment, txn: PaymentTransaction | None = None) -> EqubPayment:
    """Spread a payment over the member's unpaid rounds (oldest first); the rest becomes credit. Idempotent."""
    record = EqubPayment.objects.select_for_update().get(pk=record.pk)
    if record.status == EqubPayment.Status.PAID:
        return record
    m = EqubMembership.objects.select_for_update().select_related("group").get(pk=record.membership_id)
    credit_before = Decimal(m.prepaid_credit or 0)
    available = Decimal(record.amount) + credit_before
    settled = []
    for c in (Contribution.objects.select_related("round__group").filter(membership=m, status__in=Contribution.UNSETTLED)
              .order_by("round__number")):
        apply_late_fee(c)
        if available < c.amount_due:
            break
        mark_contribution_paid(c, txn, record, note=f"Receipt {record.reference}")
        available -= c.amount_due
        settled.append({"round": c.round.number, "amount": str(c.amount), "late_fee": str(c.late_fee)})
    EqubMembership.objects.filter(pk=m.pk).update(prepaid_credit=available)
    record.status = EqubPayment.Status.PAID
    record.paid_at = timezone.now()
    if txn:
        record.transaction = txn
    record.allocation = {"rounds": settled, "credit_before": str(credit_before), "credit_after": str(available),
                         "credit_used": str(max(credit_before - available, ZERO))}
    record.save()
    after_payment(m)
    return record


def _notify_payment(record: EqubPayment):
    m = record.membership
    rounds = record.allocation.get("rounds", [])
    covered = (f"round {rounds[0]['round']}" if len(rounds) == 1 else
               f"rounds {rounds[0]['round']}–{rounds[-1]['round']}" if rounds else "")
    credit = Decimal(record.allocation.get("credit_after") or 0)
    extra = (f" Covers {covered}." if covered else "") + (f" Credit for next rounds: {money(credit)} ETB." if credit > 0 else "")
    notify(record.user, Notification.Kind.PAYMENT, "Payment received ✅",
           f"{money(record.amount)} ETB received for {m.group.name} (receipt {record.reference}).{extra}",
           {"equb": m.group.slug, "membership": m.id, "receipt": record.reference})


@transaction.atomic
def record_office_payment(m: EqubMembership, staff, *, method: str, amount=None, rounds: int | None = None,
                          reference: str = "", note: str = "", notify_member: bool = True) -> EqubPayment:
    """Cash / bank / manual mobile-money payment taken at the office. Settles rounds like an app payment."""
    if method not in EqubPayment.OFFICE_METHODS:
        raise BusinessRuleError("Choose cash, bank transfer or mobile money.")
    m = EqubMembership.objects.select_related("group", "user").get(pk=m.pk)
    if m.status == EqubMembership.Status.LEFT:
        raise BusinessRuleError("This member has left the Equb.")
    if m.group.status not in (EqubGroup.Status.ACTIVE, EqubGroup.Status.COMPLETED):
        raise BusinessRuleError("Payments can be recorded once the Equb has started.")
    plan = payment_plan(m)
    if rounds:
        option = next((o for o in plan["options"] if o["rounds"] == int(rounds)), None)
        if not option:
            raise BusinessRuleError("That number of rounds isn't payable right now.")
        amount = Decimal(option["amount"])
    amount = Decimal(str(amount or 0)).quantize(CENT)
    if amount <= 0:
        raise BusinessRuleError("Enter the amount received.")
    if not plan["payable"] and amount > Decimal(plan["owed"]):
        raise BusinessRuleError(f"Only {money(plan['owed'])} ETB is owed on this place; it can't take advance payments.")
    if method in (EqubPayment.Method.BANK, EqubPayment.Method.MOBILE) and not reference.strip():
        raise BusinessRuleError("Enter the transfer reference so the payment can be traced.")
    record = EqubPayment.objects.create(membership=m, user=m.user, amount=amount, method=method,
                                        rounds_requested=int(rounds or 0), external_reference=reference.strip()[:64],
                                        recorded_by=staff, note=note[:255])
    txn = PaymentTransaction.objects.create(
        user=m.user, provider=method, purpose=PaymentTransaction.Purpose.EQUB_CONTRIBUTION, amount=amount,
        status=PaymentTransaction.Status.SUCCESS, target=record, provider_reference=reference.strip()[:128] or record.reference,
        payer_phone=m.user.phone, completed_at=timezone.now(), fulfilled=True, recorded_by=staff,
        raw_request={"recorded_by": getattr(staff, "full_name", ""), "note": note[:255]},
    )
    record = allocate_payment(record, txn)
    if notify_member:
        transaction.on_commit(lambda: _notify_payment(record))
    return record


def after_payment(m: EqubMembership):
    """Housekeeping once money arrives: clear collection flags when the member has caught up."""
    m = EqubMembership.objects.select_related("group").get(pk=m.pk)
    st = standing_mod.standing_for(m)
    fields = {}
    if st.arrears <= 0:
        if m.called_at:
            fields["called_at"] = None
            fields["called_by"] = None
        if m.termination_requested_at:
            fields["termination_requested_at"] = None
            fields["termination_note"] = ""
        if m.standing_notified:
            fields["standing_notified"] = ""
    if fields:
        EqubMembership.objects.filter(pk=m.pk).update(**fields)


# --- draw -------------------------------------------------------------------
def chance_factors(m: EqubMembership, slip, st, rnd: EqubRound, rules: EqubSettings, ahead: set,
                   earlier_unpaid: int) -> tuple[int, list[dict]]:
    """
    A member's weight in the automatic draw (100 = a normal chance) and why:
    paid this round on time / late, paid future rounds ahead, on-time streak, earlier rounds still unpaid.
    """
    if not rules.weighted_draw:
        return 100, []
    f = []
    if slip is not None and slip.status == Contribution.Status.PAID and slip.paid_at:
        if timezone.localtime(slip.paid_at).date() <= rnd.due_date:
            f.append({"label": "Paid on time", "pct": rules.chance_on_time_percent})
        else:
            f.append({"label": "Paid late", "pct": -rules.chance_late_percent})
    if m.id in ahead or (m.prepaid_credit or 0) > 0:
        f.append({"label": "Paid in advance", "pct": rules.chance_prepaid_percent})
    if st.on_time_streak > 1 and rules.chance_streak_percent:
        bonus = min(st.on_time_streak * rules.chance_streak_percent, rules.chance_streak_cap_percent)
        f.append({"label": f"{st.on_time_streak} on time in a row", "pct": bonus})
    if earlier_unpaid and rules.chance_missed_percent:
        f.append({"label": f"{earlier_unpaid} earlier round{'s' if earlier_unpaid > 1 else ''} unpaid",
                  "pct": -min(earlier_unpaid * rules.chance_missed_percent, 100)})
    f = [x for x in f if x["pct"]]
    weight = max(rules.chance_min_percent, 100 + sum(x["pct"] for x in f))
    return weight, f


def draw_pool(rnd: EqubRound, rules: EqubSettings | None = None, today: date | None = None) -> dict:
    """
    Who is in this round's draw and who is not – with the reason for every exclusion.
    Entries are in join order (position), the canonical order used for the fairness proof.
    """
    rules = rules or EqubSettings.load()
    today = today or timezone.localdate()
    group = rnd.group
    members = list(group.memberships.exclude(status=EqubMembership.Status.LEFT)
                   .select_related("user", "group", "won_round").order_by("position"))
    standings = standing_mod.standings_for(members, rules, today)
    slips = {c.membership_id: c for c in rnd.contributions.all()}
    earlier = {r["membership_id"]: r for r in Contribution.objects.filter(
        round__group=group, round__number__lt=rnd.number, status__in=Contribution.UNSETTLED
    ).values("membership_id").annotate(n=Count("id"), s=Sum("amount"))}
    ahead = set(Contribution.objects.filter(round__group=group, round__number__gt=rnd.number,
                                            status=Contribution.Status.PAID).values_list("membership_id", flat=True))
    entries = []
    for m in members:
        st = standings[m.id]
        reasons = []
        if m.status == EqubMembership.Status.DEFAULTED:
            reasons.append("Terminated from this Equb")
        elif m.has_won:
            reasons.append(f"Already won in round {st.won_round}" if st.won_round else "Already won")
        else:
            if m.is_draw_blocked:
                reasons.append(f"Held out of draws by staff until {m.draw_blocked_until:%d %b %Y}"
                               + (f" – {m.draw_block_reason}" if m.draw_block_reason else ""))
            slip = slips.get(m.id)
            if group.require_paid_to_draw:
                if not slip or slip.status not in Contribution.SETTLED:
                    reasons.append(f"Round {rnd.number} contribution not paid")
                if m.id in earlier:
                    e = earlier[m.id]
                    reasons.append(f"Owes {e['n']} earlier round{'s' if e['n'] > 1 else ''} ({money(e['s'])} ETB)")
            elif st.status in (standing_mod.BLOCKED, standing_mod.SUSPENDED, standing_mod.NEVER_PAID):
                reasons.append(f"{st.missed_rounds} round{'s' if st.missed_rounds != 1 else ''} behind ({money(st.arrears)} ETB)")
            unpaid_round = any("contribution not paid" in r for r in reasons)
            if st.paid_rounds < rules.min_paid_rounds_to_win and not unpaid_round:
                reasons.append(f"Needs at least {rules.min_paid_rounds_to_win} paid round"
                               f"{'s' if rules.min_paid_rounds_to_win > 1 else ''}")
        weight, factors = chance_factors(m, slips.get(m.id), st, rnd, rules, ahead,
                                         earlier.get(m.id, {}).get("n", 0))
        entries.append({"membership_id": m.id, "position": m.position, "name": m.display_name, "team_id": m.team_id,
                        "phone": m.user.phone, "eligible": not reasons, "reasons": reasons,
                        "standing": st.status, "standing_label": st.status_label, "_m": m,
                        "weight": weight, "factors": factors})
    eligible = [e for e in entries if e["eligible"]]
    k = min(group.winners_per_round, len(eligible))
    odds = round(100 * k / len(eligible), 2) if eligible else 0.0
    total_w = sum(e["weight"] for e in eligible) or 1
    for e in entries:
        # chance of being one of k winners ≈ k × weight share (capped at 100%)
        e["odds"] = round(min(100.0, 100 * k * e["weight"] / total_w), 1) if e["eligible"] else 0.0
        e["share"] = round(100 * e["weight"] / total_w, 1) if e["eligible"] else 0.0
    waiting = [e for e in entries if e["_m"].status == EqubMembership.Status.ACTIVE and not e["_m"].has_won]
    return {"round": rnd.number, "entries": entries, "eligible_count": len(eligible),
            "excluded_count": len(waiting) - len(eligible), "waiting_count": len(waiting),
            "winners_to_draw": k, "odds_percent": odds}


def eligible_memberships(rnd: EqubRound) -> list[EqubMembership]:
    return [e["_m"] for e in draw_pool(rnd)["entries"] if e["eligible"]]


def _snapshot(pool: dict, rules: EqubSettings) -> dict:
    return {
        "eligible_count": pool["eligible_count"], "excluded_count": pool["excluded_count"],
        "waiting_count": pool["waiting_count"], "odds_percent": pool["odds_percent"],
        "eligible": [{"membership_id": e["membership_id"], "position": e["position"], "name": e["name"],
                      "weight": e["weight"], "factors": e["factors"]}
                     for e in pool["entries"] if e["eligible"]],
        "excluded": [{"membership_id": e["membership_id"], "position": e["position"], "name": e["name"],
                      "reasons": e["reasons"]} for e in pool["entries"]
                     if not e["eligible"] and e["_m"].status == EqubMembership.Status.ACTIVE and not e["_m"].has_won],
        "rules": {"block_at": rules.block_at, "min_paid_rounds_to_win": rules.min_paid_rounds_to_win,
                  "weighted_draw": rules.weighted_draw},
    }


def prize_for(group: EqubGroup, rnd: EqubRound | None = None) -> Decimal:
    """
    What one winner receives before the service fee: the price per person (contribution × duration) – the full
    trip value every member pays in. Equbs without a duration fall back to the round's pot split between winners.
    """
    if group.payment_rounds:
        return Decimal(group.price_per_person).quantize(CENT)
    rnd = rnd or group.rounds.filter(status=EqubRound.Status.COLLECTING).first()
    if rnd is None:
        return Decimal(group.contribution_amount).quantize(CENT)
    if group.payout_basis == EqubGroup.PayoutBasis.FULL_POT:
        return (Decimal(rnd.expected_amount) / group.winners_per_round).quantize(CENT)
    return Decimal(rnd.collected_amount).quantize(CENT)


def payout_for(group: EqubGroup, rnd: EqubRound, winners: int) -> tuple[Decimal, Decimal, Decimal]:
    """(gross, fee, net) per winner for this round."""
    gross = prize_for(group, rnd)
    fee = (gross * Decimal(group.service_fee_percent) / 100).quantize(CENT)
    return gross, fee, gross - fee


def equb_funds(group: EqubGroup, rnd: EqubRound | None = None) -> dict:
    """
    The money this Equb actually holds for payouts: every contribution members paid (plus credit paid ahead)
    minus what earlier winners already received. Each winner needs one full prize, so this decides how many
    people can win right now.
    """
    paid_in = (Contribution.objects.filter(round__group=group, status=Contribution.Status.PAID)
               .aggregate(s=Sum("amount"))["s"] or Decimal("0"))
    credit = (group.memberships.filter(status__in=EqubMembership.LIVE_STATUSES)
              .aggregate(s=Sum("prepaid_credit"))["s"] or Decimal("0"))
    paid_out = EqubPayout.objects.filter(round__group=group).aggregate(s=Sum("gross_amount"))["s"] or Decimal("0")
    available = max(paid_in + credit - paid_out, Decimal("0")).quantize(CENT)
    prize = prize_for(group, rnd)
    fee = (prize * Decimal(group.service_fee_percent) / 100).quantize(CENT)
    max_winners = int(available // prize) if prize > 0 else 0
    return {"collected": (paid_in + credit).quantize(CENT), "paid_out": paid_out.quantize(CENT), "available": available,
            "prize": prize, "fee": fee, "net": prize - fee, "max_winners": max_winners,
            "short_for_one": max(prize - available, Decimal("0")).quantize(CENT)}


def winners_wanted(pool_size: int, *, count: int | None = None, percent: Decimal | float | None = None,
                   default: int = 1) -> int:
    """How many winners staff asked for: a number, or a percentage of the people in the draw (at least 1)."""
    if percent is not None and percent != "":
        pct = max(Decimal("0"), min(Decimal(str(percent)), Decimal("100")))
        return max(1, int((Decimal(pool_size) * pct / 100).to_integral_value(rounding="ROUND_HALF_UP"))) if pool_size else 0
    if count:
        return max(1, int(count))
    return default


def selectable_members(pool_info: dict, include_ineligible: bool = False) -> list[dict]:
    """Members staff may pick as winners: the eligible ones, or (with a reason) anyone still waiting to win."""
    return [e for e in pool_info["entries"]
            if e["eligible"] or (include_ineligible and e["_m"].status == EqubMembership.Status.ACTIVE
                                 and not e["_m"].has_won)]


@transaction.atomic
def run_draw(rnd: EqubRound, executed_by=None, *, winner_ids: list[int] | None = None, note: str = "",
             include_ineligible: bool = False, team=None, exact: bool = True, count: int | None = None,
             percent=None) -> list[EqubPayout]:
    """
    Draw the round's winner(s).

    Without ``winner_ids`` it is the provably-fair random draw of ``count`` winners (or ``percent`` of the people
    in the draw; default: the Equb's winners per round). With ``winner_ids`` staff choose the winners: they must
    be in the draw, unless ``include_ineligible`` is set with a ``note`` saying why.
    Every winner receives the full price per person, so the Equb must hold that much money for each winner.
    """
    rnd = EqubRound.objects.select_for_update().select_related("group").get(pk=rnd.pk)
    group = rnd.group
    if rnd.status != EqubRound.Status.COLLECTING:
        raise BusinessRuleError("This round is not ready for a draw.")
    if group.current_round != rnd.number:
        raise BusinessRuleError("Draws must run in round order.")
    if not group.memberships.filter(status=EqubMembership.Status.ACTIVE, has_won=False).exists():
        return close_payment_round(rnd)  # everyone has won: this round only collects the remaining payments
    numbers = draw_round_numbers(group)
    if numbers is not None and rnd.number not in numbers:
        planned = ", ".join(str(n) for n in sorted(numbers)) or "none left"
        raise BusinessRuleError(
            f"Round {rnd.number} is a payment-only round: this Equb has {group.rounds_drawn} draw"
            f"{'s' if group.rounds_drawn > 1 else ''} (rounds {planned}). Close it with “No winner this round”, "
            "or change Rounds Drawn on the group.")
    draw = EqubDraw.objects.select_for_update().get(round=rnd)
    if draw.is_executed:
        raise BusinessRuleError("This round has already been drawn.")

    rules = EqubSettings.load()
    pool_info = draw_pool(rnd, rules)
    funds = equb_funds(group, rnd)
    if funds["max_winners"] < 1:
        raise BusinessRuleError(
            f"Not enough money for a winner yet: the Equb holds {money(funds['available'])} ETB and each winner "
            f"receives {money(funds['prize'])} ETB (price per person). {money(funds['short_for_one'])} ETB more "
            "must be collected first.")
    if winner_ids:
        note = (note or "").strip()
        if include_ineligible and not note:
            raise BusinessRuleError("Write why you are choosing members who are not in the draw.")
        pool = [e["_m"] for e in selectable_members(pool_info, include_ineligible)]
        if not pool:
            raise BusinessRuleError("Nobody can be chosen for this round.")
        ids = [m.id for m in pool]
        chosen = list(dict.fromkeys(int(i) for i in winner_ids))
        k = min(group.winners_per_round, len(pool))
        if any(i not in ids for i in chosen):
            raise BusinessRuleError("One of the chosen members can't win this round.")
        if not chosen:
            raise BusinessRuleError("Choose who wins this round.")
        if not exact:  # Draw now page: staff may hand the round to one person or to a whole Group Equb
            k = len(chosen)
        if len(chosen) != k:
            raise BusinessRuleError(f"Choose exactly {k} winner{'s' if k > 1 else ''} for this round.")
        if k > funds["max_winners"]:
            raise BusinessRuleError(
                f"The Equb holds {money(funds['available'])} ETB – enough for {funds['max_winners']} winner"
                f"{'s' if funds['max_winners'] > 1 else ''} at {money(funds['prize'])} ETB each. Choose fewer people.")
        indices = [ids.index(i) for i in chosen]
        public_input = f"equb:{group.id}:round:{rnd.number}:selected"
        draw.mode, draw.note = "SELECTED", (note or (f"Whole Group Equb “{team.name}” chosen" if team else ""))[:255]
    else:
        pool = [e["_m"] for e in pool_info["entries"] if e["eligible"] and (team is None or e["team_id"] == team.id)]
        if not pool:
            raise BusinessRuleError(f"Nobody in {team.name} can win this round yet – they must be paid up and not have won."
                                    if team else "No eligible members – nobody is paid up for this round yet.")
        ids = [m.id for m in pool]
        scope = f":team:{team.id}" if team else ""
        public_input = fairness.build_public_input(f"equb:{group.id}:round:{rnd.number}{scope}", ids)
        if team:
            draw.note = f"Drawn within the Group Equb “{team.name}”"[:255]
        wanted = winners_wanted(len(pool), count=count, percent=percent, default=group.winners_per_round)
        k = min(wanted, len(pool), funds["max_winners"])
        weights = [e["weight"] for e in pool_info["entries"] if e["eligible"] and (team is None or e["team_id"] == team.id)]
        if rules.weighted_draw and len(set(weights)) > 1:
            indices = fairness.pick_weighted(draw.server_seed, public_input, weights, k)
            draw.weights = weights
        else:
            indices = fairness.pick_many(draw.server_seed, public_input, len(pool), k)
            draw.weights = []
        if k < wanted:
            draw.note = ((draw.note + " · ") if draw.note else "") + \
                f"{wanted} winners asked, {k} drawn ({'money for ' + str(funds['max_winners']) if k == funds['max_winners'] else 'people in the draw'})"
            draw.note = draw.note[:255]
        draw.mode = "WEIGHTED" if draw.weights else "RANDOM"

    draw.public_input = public_input
    draw.eligible_memberships = ids
    draw.winner_indices = indices
    draw.pool_snapshot = _snapshot(pool_info, rules)
    draw.executed_at = timezone.now()
    draw.executed_by = executed_by
    draw.save()

    gross, fee, payout_amount = payout_for(group, rnd, k)
    payouts = []
    for idx in indices:
        winner = pool[idx]
        winner.has_won = True
        winner.status = EqubMembership.Status.WON
        winner.won_round = rnd
        winner.save(update_fields=["has_won", "status", "won_round", "updated_at"])
        payouts.append(EqubPayout.objects.create(round=rnd, membership=winner, amount=payout_amount, gross_amount=gross,
                                                 fee_amount=fee, voucher_code="EQ" + generate_code(8)))

    rnd.status = EqubRound.Status.DRAWN
    rnd.save(update_fields=["status", "updated_at"])

    winner_names = ", ".join(p.membership.display_name.split(" ")[0] for p in payouts)
    for m in _active_members(group).select_related("user"):
        is_winner = any(p.membership_id == m.id for p in payouts)
        notify(m.user, Notification.Kind.EQUB_WIN if is_winner else Notification.Kind.EQUB_REMINDER,
               (f"🎉 {m.dependent_name} won the Equb draw!" if m.is_dependent else "🎉 You won the Equb draw!")
               if is_winner else f"Round {rnd.number} winner: {winner_names}",
               (f"Your {money(payout_amount)} ETB travel payout for {group.name} is ready. Redeem it on your trip."
                if is_winner else (f"{len(pool)} members were in the draw. Verify it in the app – the next contribution is due soon."
                                   if draw.mode in ("RANDOM", "WEIGHTED") else "The next contribution is due soon.")),
               {"equb": group.slug, "round": rnd.number, "membership": m.id})

    _advance(group, rnd)
    return payouts


def _advance(group: EqubGroup, rnd: EqubRound) -> None:
    """Open the next round – winners keep paying until every payment round is done – or finish the Equb."""
    nxt = group.rounds.filter(number=rnd.number + 1).first()
    if nxt:
        open_round(nxt)
        return
    EqubGroup.objects.filter(pk=group.pk).update(status=EqubGroup.Status.COMPLETED)
    for m in _active_members(group).filter(prepaid_credit__gt=0).select_related("user"):
        notify(m.user, Notification.Kind.EQUB_REMINDER, f"{group.name} is complete",
               f"Every round is done. Your unused credit of {money(m.prepaid_credit)} ETB will be refunded – "
               "our team will contact you.", {"equb": group.slug, "membership": m.id})


def reopen_if_short(group: EqubGroup) -> EqubGroup:
    """
    An Equb that finished before every payment round was collected (it used to end once everyone had won)
    is opened again so members can pay the rest of the price per person.
    """
    if group.status != EqubGroup.Status.COMPLETED or not group.payment_rounds:
        return group
    wanted = round_count(group)
    if group.rounds.count() >= wanted:
        return group
    with transaction.atomic():
        group = EqubGroup.objects.select_for_update().get(pk=group.pk)
        if group.status != EqubGroup.Status.COMPLETED:
            return group
        group.status = EqubGroup.Status.ACTIVE
        group.save(update_fields=["status", "updated_at"])
        _schedule_rounds(group, wanted, group.members_count)
        if not group.rounds.filter(status=EqubRound.Status.COLLECTING).exists():
            nxt = group.rounds.filter(status=EqubRound.Status.UPCOMING).order_by("number").first()
            if nxt:
                open_round(nxt)
    return EqubGroup.objects.get(pk=group.pk)


@transaction.atomic
def close_round_without_winner(rnd: EqubRound, staff=None, note: str = "") -> EqubRound:
    """
    Staff close a round with nobody winning – usually because the Equb doesn't hold one full prize yet.
    The money stays in the Equb for the next draw and the next round opens.
    """
    rnd = EqubRound.objects.select_for_update().select_related("group").get(pk=rnd.pk)
    group = rnd.group
    if rnd.status != EqubRound.Status.COLLECTING or group.current_round != rnd.number:
        raise BusinessRuleError("Only the round being collected now can be closed.")
    if not is_draw_round(rnd) and not (note or "").strip():
        return close_payment_round(rnd, "Payment-only round – no draw this round")
    reason = (note or "").strip() or "Not enough money for a winner – kept for the next draw"
    rnd.status = EqubRound.Status.DRAWN
    rnd.draw_note = f"No winner this round: {reason}"[:255]
    rnd.save(update_fields=["status", "draw_note", "updated_at"])
    for m in _active_members(group).select_related("user"):
        notify(m.user, Notification.Kind.EQUB_REMINDER, f"{group.name}: no winner in round {rnd.number}",
               "The money collected stays in the Equb and goes to the next draw. Keep paying on time to be in it.",
               {"equb": group.slug, "round": rnd.number, "membership": m.id})
    _advance(group, rnd)
    return rnd


def close_payment_round(rnd: EqubRound, note: str = "") -> list:
    """
    A round with nothing to draw – everyone has already won, or it isn't one of the "Rounds drawn":
    it is closed on its draw day, the money stays for the next draw and the next round opens.
    """
    group = rnd.group
    rnd.status = EqubRound.Status.DRAWN
    rnd.draw_note = (note or "Payment-only round – every member has already won")[:255]
    rnd.save(update_fields=["status", "draw_note", "updated_at"])
    _advance(group, rnd)
    return []


def verify_draw(rnd: EqubRound) -> dict:
    draw = rnd.draw
    if not draw.is_executed:
        return {"executed": False, "seed_commitment": draw.seed_commitment}
    if draw.is_selected:  # winners chosen by staff – there is no random result to recompute
        return {"executed": True, "mode": "SELECTED", "note": draw.note, "seed_commitment": draw.seed_commitment,
                "eligible_memberships": draw.eligible_memberships, "winner_indices": draw.winner_indices,
                "eligible_count": len(draw.eligible_memberships), "executed_at": draw.executed_at.isoformat(),
                "valid": None, "matches": None, "commitment_ok": None, "recomputed_indices": []}
    if draw.weights:
        result = fairness.verify_weighted(draw.server_seed, draw.seed_commitment, draw.public_input, draw.weights,
                                          draw.winner_indices)
    else:
        result = fairness.verify(draw.server_seed, draw.seed_commitment, draw.public_input,
                                 len(draw.eligible_memberships), draw.winner_indices)
    snap = draw.pool_snapshot or {}
    return {
        "executed": True,
        "seed_commitment": draw.seed_commitment,
        "server_seed": draw.server_seed,
        "public_input": draw.public_input,
        "eligible_memberships": draw.eligible_memberships,
        "winner_indices": draw.winner_indices,
        "eligible_count": len(draw.eligible_memberships),
        "excluded_count": snap.get("excluded_count"),
        "executed_at": draw.executed_at.isoformat(),
        "mode": draw.mode,
        "weights": draw.weights,
        "algorithm": ("weighted: key_i = ln(u_i) / weight_i with u_i from HMAC_SHA256(server_seed, public_input#(10000000+i)); "
                      "the largest keys win" if draw.weights else
                      "index = HMAC_SHA256(server_seed, public_input#counter) mod n (rejection sampled)"),
        **result.as_dict(),
    }


# --- payouts ----------------------------------------------------------------
@transaction.atomic
def redeem_payout(payout: EqubPayout, booking) -> EqubPayout:
    from apps.travel.models import Booking
    from apps.travel.services.bookings import book_with_credit

    payout = EqubPayout.objects.select_for_update().get(pk=payout.pk)
    if payout.status != EqubPayout.Status.ISSUED:
        raise BusinessRuleError("This payout has already been used.")
    if booking.user_id != payout.membership.user_id:
        raise BusinessRuleError("You can only redeem a payout on your own booking.")
    if booking.status != Booking.Status.PENDING_PAYMENT:
        raise BusinessRuleError("Payouts can only be applied to bookings awaiting payment.")
    book_with_credit(booking, payout.amount, Booking.Funding.EQUB_PAYOUT, payout.voucher_code)
    payout.status = EqubPayout.Status.REDEEMED
    payout.booking = booking
    payout.redeemed_at = timezone.now()
    payout.save()
    return payout


@transaction.atomic
def mark_payout_paid_out(payout: EqubPayout, staff, *, method: str, reference: str = "") -> EqubPayout:
    payout = EqubPayout.objects.select_for_update().select_related("membership__user", "round__group").get(pk=payout.pk)
    if payout.status != EqubPayout.Status.ISSUED:
        raise BusinessRuleError("Only an unused payout can be paid out.")
    payout.status = EqubPayout.Status.PAID_OUT
    payout.paid_out_at = timezone.now()
    payout.paid_out_by = staff
    payout.paid_out_method = method[:20]
    payout.paid_out_reference = reference[:64]
    payout.save()
    notify(payout.membership.user, Notification.Kind.EQUB_WIN, "Payout sent",
           f"Your {money(payout.amount)} ETB payout from {payout.round.group.name} was paid by {method}"
           + (f" (ref {reference})." if reference else "."),
           {"equb": payout.round.group.slug, "membership": payout.membership_id})
    return payout


# --- ledgers ----------------------------------------------------------------
def public_name(m: EqubMembership, viewer=None) -> str:
    """Members see only their own name; everyone else is "Member <place>". Staff (and the console) see real names."""
    if viewer is None or getattr(viewer, "is_staff", False):
        return m.display_name
    if viewer.is_authenticated and m.user_id == viewer.id:
        return f"{m.display_name} (you)" if not m.is_dependent else f"{m.dependent_name} (your place)"
    return f"Member {m.position}"


def member_ledger(group: EqubGroup, viewer=None) -> list[dict]:
    rows = []
    memberships = list(group.memberships.exclude(status=EqubMembership.Status.LEFT).select_related("user", "won_round", "group"))
    standings = standing_mod.standings_for(memberships)
    contributions = Contribution.objects.filter(round__group=group).select_related("round")
    by_member: dict[int, list] = {}
    for c in contributions:
        by_member.setdefault(c.membership_id, []).append(
            {"round": c.round.number, "status": c.status, "amount": str(c.amount_due),
             "paid_at": c.paid_at.isoformat() if c.paid_at else None})
    for m in memberships:
        st = standings[m.id]
        rows.append({
            "membership_id": m.id, "position": m.position, "name": public_name(m, viewer),
            "is_me": bool(viewer is not None and viewer.is_authenticated and m.user_id == viewer.id),
            "phone_masked": (m.user.phone[:4] + "*****" + m.user.phone[-3:])
            if viewer is None or getattr(viewer, "is_staff", False) else "",
            "status": m.status, "has_won": m.has_won, "won_round": m.won_round.number if m.won_round else None,
            "total_paid": str(m.total_paid), "missed_count": st.missed_rounds, "standing": st.status,
            "standing_label": st.status_label, "standing_tone": st.tone,
            "contributions": sorted(by_member.get(m.id, []), key=lambda r: r["round"]),
        })
    return rows


def group_totals(group: EqubGroup) -> dict:
    agg = Contribution.objects.filter(round__group=group, status__in=Contribution.SETTLED).aggregate(
        s=Sum("amount"), fees=Sum("late_fee"))
    payouts = EqubPayout.objects.filter(round__group=group).aggregate(n=Count("id"), s=Sum("amount"), fee=Sum("fee_amount"))
    return {"total_collected": str(agg["s"] or 0), "late_fees_collected": str(agg["fees"] or 0),
            "payouts_issued": payouts["n"] or 0, "payouts_total": str(payouts["s"] or 0),
            "service_fees": str(payouts["fee"] or 0),
            "rounds_drawn": group.rounds.filter(status=EqubRound.Status.DRAWN).count()}


# --- pending (unfinished) app payments ----------------------------------------------
GATEWAY_METHODS = (EqubPayment.Method.CHAPA, EqubPayment.Method.TELEBIRR, EqubPayment.Method.MOCK)
SCHEDULED_BILL_HOURS = 24  # a calendar-pay bill stays payable for a day


def cancel_pending_payment(record: EqubPayment, *, reason: str = "Cancelled by the member") -> EqubPayment:
    """
    Cancel an app payment that was started but not finished. Chapa is asked first: if the member did pay,
    the payment is credited instead of being cancelled.
    """
    from apps.payments.services import apply_status, refresh_from_provider

    record = EqubPayment.objects.select_related("transaction").get(pk=record.pk)
    if record.status != EqubPayment.Status.PENDING:
        raise BusinessRuleError("Only payments that are still waiting can be cancelled.")
    txn = record.transaction
    if txn is not None:
        txn = refresh_from_provider(txn)
        if txn.status == PaymentTransaction.Status.SUCCESS:
            record.refresh_from_db()
            return record
        if not txn.is_final:
            apply_status(txn.pk, PaymentTransaction.Status.CANCELLED, reason=reason)
    record.refresh_from_db()
    if record.status == EqubPayment.Status.PENDING:
        record.status = EqubPayment.Status.CANCELLED
        record.note = (record.note or reason)[:255]
        record.save(update_fields=["status", "note", "updated_at"])
    return record


def expire_pending_payments(rules: EqubSettings | None = None, membership: EqubMembership | None = None) -> int:
    """Cancel app payments left unpaid longer than the Equb rules allow (calendar-pay bills get a day)."""
    from .models import ScheduledPayment

    rules = rules or EqubSettings.load()
    now = timezone.now()
    qs = EqubPayment.objects.filter(status=EqubPayment.Status.PENDING, method__in=GATEWAY_METHODS,
                                    created_at__lt=now - timedelta(minutes=rules.pending_payment_minutes))
    if membership is not None:
        qs = qs.filter(membership=membership)
    bills = set(ScheduledPayment.objects.filter(transaction__isnull=False, status=ScheduledPayment.Status.SENT,
                                                sent_at__gte=now - timedelta(hours=SCHEDULED_BILL_HOURS))
                .values_list("transaction_id", flat=True))
    n = 0
    for record in qs.select_related("transaction")[:200]:
        if record.transaction_id in bills:
            continue
        try:
            if cancel_pending_payment(record, reason="Not paid in time").status == EqubPayment.Status.CANCELLED:
                n += 1
        except BusinessRuleError:
            pass
    return n


def live_draws(user, hours: int = 48, limit: int = 10) -> list[dict]:
    """
    Recent draws with winners in the Equbs this user is in – the app plays them as a live spin-the-wheel reveal.
    Members are shown by place number only (privacy); the app marks the user's own place as "You".
    """
    since = timezone.now() - timedelta(hours=hours)
    places = {m.id: m for m in EqubMembership.objects.filter(user=user).exclude(status=EqubMembership.Status.LEFT)}
    if not places:
        return []
    groups = {m.group_id for m in places.values()}
    draws = (EqubDraw.objects.filter(round__group_id__in=groups, executed_at__gte=since)
             .select_related("round__group").prefetch_related("round__payouts__membership").order_by("-executed_at")[:limit])
    out = []
    for d in draws:
        payouts = list(d.round.payouts.all())
        if not payouts:
            continue  # closed without a winner – nothing to reveal
        group = d.round.group
        eligible = (d.pool_snapshot or {}).get("eligible") or []
        entries = [{"membership_id": e["membership_id"], "position": e["position"],
                    "is_me": e["membership_id"] in places, "weight": e.get("weight", 100)} for e in eligible]
        known = {e["membership_id"] for e in entries}
        for p in payouts:  # staff-selected winners may not be in the snapshot
            if p.membership_id not in known:
                entries.append({"membership_id": p.membership_id, "position": p.membership.position,
                                "is_me": p.membership_id in places, "weight": 100})
        total_w = sum(e["weight"] for e in entries) or 1
        for e in entries:
            e["share"] = round(100 * e["weight"] / total_w, 1)
        out.append({
            "id": d.id, "executed_at": d.executed_at.isoformat(), "mode": d.mode,
            "group": {"name": group.name, "slug": group.slug}, "round": d.round.number,
            "total_rounds": group.total_rounds, "entries": entries,
            "winners": [{"membership_id": p.membership_id, "position": p.membership.position,
                         "is_me": p.membership_id in places, "amount": str(p.gross_amount or p.amount)}
                        for p in sorted(payouts, key=lambda p: p.membership.position)],
            "seed_commitment": d.seed_commitment,
        })
    return out
