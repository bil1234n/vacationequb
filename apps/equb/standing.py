"""
Member standing: what a member owes, what they have paid ahead, and how they have behaved.

One definition of "behind", used everywhere (app, console, draws, call centre, reports):

    overdue round   = a contribution not settled by its due date + the pool's grace days
    arrears         = everything owed on overdue rounds (late fees included)
    missed rounds   = how many rounds are overdue
    days overdue    = days since the oldest unpaid round fell due
    credit          = money paid ahead, applied automatically as new rounds open

The status ladder (warned → blocked → suspended) is set in the console under Equb rules,
so a change there moves every member at once.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from django.utils import timezone

from .models import Contribution, EqubGroup, EqubMembership, EqubRound, EqubSettings

ZERO = Decimal("0")

CURRENT = "current"
AHEAD = "ahead"
WARNED = "warned"
BLOCKED = "blocked"
SUSPENDED = "suspended"
NEVER_PAID = "never_paid"
NOT_STARTED = "not_started"
COMPLETED = "completed"
CLOSED = "closed"
LEFT = "left"
TERMINATED = "terminated"

STATUS_COLORS = {CURRENT: "#0F382B", AHEAD: "#3E8E6C", WARNED: "#C9A227", BLOCKED: "#D98E04", SUSPENDED: "#C0392B",
                 NEVER_PAID: "#8E2A20", NOT_STARTED: "#B8C4BE", COMPLETED: "#8FBFA8", CLOSED: "#B8C4BE",
                 LEFT: "#B8C4BE", TERMINATED: "#5B6F66"}
STATUS_META = {
    CURRENT: ("Up to date", "green"),
    AHEAD: ("Paid ahead", "green"),
    WARNED: ("Behind – warning", "gold"),
    BLOCKED: ("Behind – out of draws", "red"),
    SUSPENDED: ("Suspended", "red"),
    NEVER_PAID: ("Never paid", "red"),
    NOT_STARTED: ("Waiting to start", "grey"),
    COMPLETED: ("Completed", "green"),
    CLOSED: ("Pool closed", "grey"),
    LEFT: ("Left", "grey"),
    TERMINATED: ("Terminated", "grey"),
}
BEHIND_STATUSES = {WARNED, BLOCKED, SUSPENDED, NEVER_PAID}


@dataclass
class Standing:
    membership_id: int
    status: str
    status_label: str
    tone: str
    contribution: Decimal
    total_rounds: int
    rounds_opened: int
    paid_rounds: int
    missed_rounds: int
    expected_to_date: Decimal
    paid_amount: Decimal
    paid_due: Decimal
    fees_paid: Decimal
    arrears: Decimal
    due_now: Decimal
    credit: Decimal
    advance_rounds: int
    days_overdue: int
    oldest_overdue_date: date | None
    on_time_streak: int
    late_count: int
    clean_record: bool
    has_won: bool
    won_round: int | None
    critical: bool
    next_due_date: date | None
    next_due_amount: Decimal
    next_due_round: int | None
    next_due_covered: bool
    remaining_amount: Decimal
    draw_blocked: bool
    draw_block_reason: str
    collection_rate: float
    overdue_rounds: list[int] = field(default_factory=list)

    @property
    def is_behind(self) -> bool:
        return self.status in BEHIND_STATUSES or self.arrears > 0

    @property
    def owes(self) -> Decimal:
        """Everything payable right now: overdue rounds plus the round that is due."""
        return self.arrears + self.due_now

    def as_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, Decimal):
                d[k] = str(v.quantize(Decimal("0.01")))
            elif isinstance(v, date):
                d[k] = v.isoformat()
        d["is_behind"] = self.is_behind
        d["owes"] = str(self.owes.quantize(Decimal("0.01")))
        return d


# --------------------------------------------------------------------------- helpers
def _grace_deadline(due: date, group: EqubGroup) -> date:
    return due + timedelta(days=group.grace_days)


def _is_overdue(c: Contribution, group: EqubGroup, today: date) -> bool:
    return c.status in Contribution.UNSETTLED and today > _grace_deadline(c.round.due_date, group)


def pending_fee(c: Contribution, group: EqubGroup, today: date) -> Decimal:
    """Late fee this contribution carries (or will carry once the nightly sweep runs)."""
    if c.late_fee:
        return Decimal(c.late_fee)
    if c.late_fee_waived or not group.late_fee:
        return ZERO
    return Decimal(group.late_fee) if _is_overdue(c, group, today) else ZERO


def _paid_on_time(c: Contribution, group: EqubGroup) -> bool:
    if c.status == Contribution.Status.WAIVED:
        return True  # covered from credit the moment the round opened
    if c.late_fee or c.late_fee_waived:
        return False
    if not c.paid_at:
        return True
    return timezone.localtime(c.paid_at).date() <= _grace_deadline(c.round.due_date, group)


# --------------------------------------------------------------------------- core
def compute(m: EqubMembership, contributions: list[Contribution], rounds: list[EqubRound],
            rules: EqubSettings, today: date) -> Standing:
    """Standing for one membership. ``contributions`` must have ``round`` loaded; ``rounds`` = the pool's rounds."""
    group = m.group
    contribution = Decimal(group.contribution_amount)
    contributions = sorted(contributions, key=lambda c: c.round.number)
    total_rounds = len(rounds) if rounds else group.total_rounds

    settled = [c for c in contributions if c.status in Contribution.SETTLED]
    overdue = [c for c in contributions if _is_overdue(c, group, today)]
    open_unpaid = [c for c in contributions if c.status in Contribution.UNSETTLED and c not in overdue]

    arrears = sum((c.amount + pending_fee(c, group, today) for c in overdue), ZERO)
    due_now = sum((c.amount + pending_fee(c, group, today) for c in open_unpaid), ZERO)
    paid_amount = sum((c.amount for c in settled), ZERO)
    fees_paid = sum((Decimal(c.late_fee) for c in settled), ZERO)
    expected_to_date = sum((c.amount for c in contributions if c.round.due_date <= today), ZERO)
    paid_due = sum((c.amount for c in settled if c.round.due_date <= today), ZERO)
    credit = Decimal(m.prepaid_credit or 0)
    advance_rounds = int(credit // contribution) if contribution > 0 else 0

    oldest = overdue[0].round.due_date if overdue else None
    days_overdue = (today - oldest).days if oldest else 0

    on_time = [_paid_on_time(c, group) for c in settled]
    late_count = sum(1 for ok in on_time if not ok)
    streak = 0
    for ok in reversed(on_time):
        if not ok:
            break
        streak += 1

    # Next payment the member should think about.
    unpaid = overdue + open_unpaid
    next_round = next_date = None
    next_amount, covered = ZERO, False
    if unpaid:
        first = unpaid[0]
        next_round, next_date = first.round.number, first.round.due_date
        next_amount = first.amount + pending_fee(first, group, today)
    else:
        upcoming = [r for r in rounds if r.status == EqubRound.Status.UPCOMING]
        if upcoming and group.status == EqubGroup.Status.ACTIVE:
            nxt = min(upcoming, key=lambda r: r.number)
            next_round, next_date, next_amount = nxt.number, nxt.due_date, contribution
            covered = credit >= contribution

    missed = len(overdue)
    status = _status(m, group, rules, missed, len(settled), advance_rounds, arrears)
    label, tone = STATUS_META[status]
    # What the member still owes in total: the price per person (contribution × payments) – winners keep paying too.
    owed_total = Decimal(group.price_per_person) if group.payment_rounds else contribution * total_rounds
    remaining = max(owed_total - paid_amount - credit, ZERO) if m.is_live else ZERO
    rate = float(round(paid_due / expected_to_date * 100, 1)) if expected_to_date else 100.0

    return Standing(
        membership_id=m.id, status=status, status_label=label, tone=tone, contribution=contribution,
        total_rounds=total_rounds, rounds_opened=len(contributions), paid_rounds=len(settled), missed_rounds=missed,
        expected_to_date=expected_to_date, paid_amount=paid_amount, paid_due=paid_due, fees_paid=fees_paid, arrears=arrears,
        due_now=due_now, credit=credit, advance_rounds=advance_rounds, days_overdue=days_overdue,
        oldest_overdue_date=oldest, on_time_streak=streak, late_count=late_count,
        clean_record=late_count == 0 and missed == 0 and bool(settled), has_won=m.has_won,
        won_round=next((r.number for r in rounds if r.id == m.won_round_id), None) if m.won_round_id else None,
        critical=m.has_won and arrears > 0, next_due_date=next_date, next_due_amount=next_amount,
        next_due_round=next_round, next_due_covered=covered, remaining_amount=remaining,
        draw_blocked=m.is_draw_blocked, draw_block_reason=m.draw_block_reason if m.is_draw_blocked else "",
        collection_rate=min(rate, 100.0), overdue_rounds=[c.round.number for c in overdue],
    )


def _status(m: EqubMembership, group: EqubGroup, rules: EqubSettings, missed: int, paid_rounds: int,
            advance_rounds: int, arrears: Decimal) -> str:
    if m.status == EqubMembership.Status.LEFT:
        return LEFT
    if m.status == EqubMembership.Status.DEFAULTED:
        return TERMINATED
    if group.status in (EqubGroup.Status.DRAFT, EqubGroup.Status.OPEN):
        return NOT_STARTED
    if group.status == EqubGroup.Status.CANCELLED:
        return CLOSED
    if missed and paid_rounds == 0:
        return NEVER_PAID
    if missed >= rules.suspend_at:
        return SUSPENDED
    if missed >= rules.block_at:
        return BLOCKED
    if missed >= rules.warn_at:
        return WARNED
    if group.status == EqubGroup.Status.COMPLETED and arrears <= 0:
        return COMPLETED
    return AHEAD if advance_rounds > 0 else CURRENT


# --------------------------------------------------------------------------- loaders
def _contrib_qs():
    return Contribution.objects.select_related("round").only(
        "id", "membership_id", "amount", "late_fee", "late_fee_waived", "status", "paid_at",
        "round__id", "round__number", "round__due_date", "round__draw_date", "round__status", "round__group_id")


def standing_for(m: EqubMembership, rules: EqubSettings | None = None, today: date | None = None) -> Standing:
    rules = rules or EqubSettings.load()
    today = today or timezone.localdate()
    contributions = list(_contrib_qs().filter(membership=m))
    rounds = list(m.group.rounds.all())
    return compute(m, contributions, rounds, rules, today)


def standings_for(memberships: Iterable[EqubMembership], rules: EqubSettings | None = None,
                  today: date | None = None) -> dict[int, Standing]:
    """Standing for many memberships with two queries. Memberships need ``group`` (and ideally ``won_round``) loaded."""
    memberships = list(memberships)
    if not memberships:
        return {}
    rules = rules or EqubSettings.load()
    today = today or timezone.localdate()
    by_member: dict[int, list[Contribution]] = defaultdict(list)
    for c in _contrib_qs().filter(membership_id__in=[m.id for m in memberships]):
        by_member[c.membership_id].append(c)
    rounds_by_group: dict[int, list[EqubRound]] = defaultdict(list)
    for r in EqubRound.objects.filter(group_id__in={m.group_id for m in memberships}).only(
            "id", "group_id", "number", "due_date", "draw_date", "status"):
        rounds_by_group[r.group_id].append(r)
    return {m.id: compute(m, by_member.get(m.id, []), rounds_by_group.get(m.group_id, []), rules, today)
            for m in memberships}


def live_memberships():
    """Memberships that can owe money: active places in pools that are running or finished."""
    return (EqubMembership.objects
            .filter(status__in=EqubMembership.LIVE_STATUSES,
                    group__status__in=[EqubGroup.Status.ACTIVE, EqubGroup.Status.COMPLETED])
            .select_related("group", "user", "won_round", "called_by"))


# --------------------------------------------------------------------------- schedule
def schedule_for(m: EqubMembership, today: date | None = None) -> list[dict]:
    """Every round of the pool from this member's point of view (paid, late, due, prepaid, upcoming…)."""
    today = today or timezone.localdate()
    group = m.group
    contribution = Decimal(group.contribution_amount)
    contribs = {c.round_id: c for c in Contribution.objects.select_related("round", "payment_record").filter(membership=m)}
    rounds = list(group.rounds.select_related("draw").prefetch_related("payouts__membership__user").order_by("number"))
    from .services import draw_round_numbers  # services imports this module

    draw_numbers = draw_round_numbers(group, rounds)
    credit = Decimal(m.prepaid_credit or 0)
    rows = []
    for r in rounds:
        c = contribs.get(r.id)
        # Other members stay private: "You" or "Member <place>".
        winners = ["You" if p.membership.user_id == m.user_id else f"Member {p.membership.position}" for p in r.payouts.all()]
        row = {
            "round": r.number, "round_id": r.id, "due_date": r.due_date.isoformat(), "draw_date": r.draw_date.isoformat(),
            "round_status": r.status, "amount": str(contribution), "late_fee": "0.00", "paid_at": None,
            "receipt": None, "you_won": bool(m.won_round_id and m.won_round_id == r.id), "winners": winners,
            "has_draw": draw_numbers is None or r.number in draw_numbers,
        }
        if c:
            fee = pending_fee(c, group, today)
            row.update(amount=str(c.amount), late_fee=str(fee.quantize(Decimal("0.01"))),
                       paid_at=c.paid_at.isoformat() if c.paid_at else None,
                       receipt=c.payment_record.reference if c.payment_record_id else None)
            if c.status == Contribution.Status.WAIVED:
                state = "credit"
            elif c.status == Contribution.Status.PAID:
                state = "paid" if _paid_on_time(c, group) else "paid_late"
            elif _is_overdue(c, group, today):
                state = "overdue"
            else:
                state = "due"
        elif not m.is_live:
            state = "not_billed"
        elif r.status == EqubRound.Status.UPCOMING:
            if credit >= contribution:
                state, credit = "prepaid", credit - contribution
            else:
                state = "upcoming"
        else:
            state = "not_billed"  # round ran before this member joined / while suspended
        row["state"] = state
        rows.append(row)
    return rows
