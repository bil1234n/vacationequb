"""
Staff operations on Equb places: draw holds, postponed draws, late-fee waivers, exit requests,
call-centre work, terminations, reminders and standing escalation.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify

from . import standing as standing_mod
from .models import (CallLog, Contribution, EqubGroup, EqubMembership, EqubRound, EqubSettings, Termination)
from .services import money

# --------------------------------------------------------------------------- draws
def set_draw_block(m: EqubMembership, staff, *, until: date, reason: str) -> EqubMembership:
    if until < timezone.localdate():
        raise BusinessRuleError("Choose a date from today onwards.")
    if not reason.strip():
        raise BusinessRuleError("Give a reason – it is shown on the draw record.")
    m.draw_blocked_until = until
    m.draw_block_reason = reason.strip()[:255]
    m.save(update_fields=["draw_blocked_until", "draw_block_reason", "updated_at"])
    return m


def clear_draw_block(m: EqubMembership) -> EqubMembership:
    m.draw_blocked_until = None
    m.draw_block_reason = ""
    m.save(update_fields=["draw_blocked_until", "draw_block_reason", "updated_at"])
    return m


@transaction.atomic
def postpone_draw(rnd: EqubRound, staff, *, new_date: date, reason: str, notify_members: bool = True) -> EqubRound:
    rnd = EqubRound.objects.select_for_update().select_related("group").get(pk=rnd.pk)
    if rnd.status == EqubRound.Status.DRAWN:
        raise BusinessRuleError("This round has already been drawn.")
    if new_date < timezone.localdate():
        raise BusinessRuleError("The new draw date can't be in the past.")
    if new_date < rnd.due_date:
        raise BusinessRuleError("The draw can't be before the round's due date.")
    old = rnd.draw_date
    rnd.draw_date = new_date
    rnd.dates_fixed = True
    rnd.draw_note = (reason.strip() or f"Moved from {old:%d %b}")[:255]
    rnd.save(update_fields=["draw_date", "draw_note", "dates_fixed", "updated_at"])
    if notify_members and rnd.status == EqubRound.Status.COLLECTING:
        for m in rnd.group.memberships.filter(status__in=EqubMembership.LIVE_STATUSES).select_related("user"):
            notify(m.user, Notification.Kind.EQUB_REMINDER, f"{rnd.group.name}: round {rnd.number} draw moved",
                   f"The draw is now on {new_date:%d %b %Y}" + (f" – {reason.strip()}" if reason.strip() else "") + ".",
                   {"equb": rnd.group.slug, "membership": m.id})
    return rnd


# --------------------------------------------------------------------------- fees
@transaction.atomic
def waive_late_fee(c: Contribution, staff, *, reason: str = "") -> Contribution:
    c = Contribution.objects.select_for_update().select_related("membership__user", "round__group").get(pk=c.pk)
    if c.is_settled:
        raise BusinessRuleError("This contribution is already paid.")
    if not c.late_fee and c.late_fee_waived:
        raise BusinessRuleError("The late fee has already been waived.")
    c.late_fee = Decimal("0")
    c.late_fee_waived = True
    c.note = (f"Late fee waived by {staff.full_name}" + (f": {reason.strip()}" if reason.strip() else ""))[:160]
    c.save(update_fields=["late_fee", "late_fee_waived", "note", "updated_at"])
    return c


# --------------------------------------------------------------------------- exit requests
def exit_info(m: EqubMembership, st: standing_mod.Standing | None = None) -> dict:
    """Can this member leave, ask to leave, or neither – and why."""
    g = m.group
    base = {"can_leave": False, "can_request_exit": False, "exit_requested_at": m.exit_requested_at,
            "exit_request_reason": m.exit_request_reason, "reason": None}
    if not m.is_live:
        return {**base, "reason": "This place is no longer active."}
    if g.status in (EqubGroup.Status.DRAFT, EqubGroup.Status.OPEN):
        return {**base, "can_leave": True,
                "reason": "The Equb hasn't started yet, so you can leave freely – nothing has been paid."}
    st = st or standing_mod.standing_for(m)
    if m.has_won:
        if st.remaining_amount > 0:
            return {**base, "reason": f"You have received your payout, so this place can't be given up. "
                                      f"{money(st.remaining_amount)} ETB is still to be paid over the remaining rounds."}
        return {**base, "reason": "You have received your payout and your contributions are complete."}
    if g.status != EqubGroup.Status.ACTIVE:
        return {**base, "reason": "This Equb has finished."}
    if m.exit_requested_at:
        return {**base, "reason": "Your request to leave is with our team. We'll contact you about it."}
    return {**base, "can_request_exit": True,
            "reason": "This Equb is running, so leaving needs approval. Send a request and our team will "
                      "contact you about the contributions you have paid."}


def request_exit(user, m: EqubMembership, reason: str) -> EqubMembership:
    if m.user_id != user.id:
        raise BusinessRuleError("This isn't your Equb place.")
    info = exit_info(m)
    if not info["can_request_exit"]:
        raise BusinessRuleError(info["reason"] or "You can't ask to leave this Equb.")
    if len(reason.strip()) < 5:
        raise BusinessRuleError("Please tell us briefly why you want to leave.")
    m.exit_requested_at = timezone.now()
    m.exit_request_reason = reason.strip()[:500]
    m.save(update_fields=["exit_requested_at", "exit_request_reason", "updated_at"])
    notify(user, Notification.Kind.EQUB_REMINDER, "Exit request received",
           f"We received your request to leave {m.group.name}. Our team will contact you within 2 working days.",
           {"equb": m.group.slug, "membership": m.id})
    return m


def cancel_exit_request(user, m: EqubMembership) -> EqubMembership:
    if m.user_id != user.id:
        raise BusinessRuleError("This isn't your Equb place.")
    if not m.exit_requested_at:
        raise BusinessRuleError("There is no exit request to cancel.")
    m.exit_requested_at = None
    m.exit_request_reason = ""
    m.save(update_fields=["exit_requested_at", "exit_request_reason", "updated_at"])
    return m


def decline_exit_request(m: EqubMembership, staff, *, note: str = "") -> EqubMembership:
    if not m.exit_requested_at:
        raise BusinessRuleError("There is no exit request on this place.")
    m.exit_requested_at = None
    m.exit_request_reason = ""
    m.save(update_fields=["exit_requested_at", "exit_request_reason", "updated_at"])
    notify(m.user, Notification.Kind.EQUB_REMINDER, "About your exit request",
           f"Your place in {m.group.name} stays active." + (f" {note.strip()}" if note.strip() else
                                                           " Please contact us if you have questions."),
           {"equb": m.group.slug, "membership": m.id})
    return m


# --------------------------------------------------------------------------- call centre
def log_call(m: EqubMembership, staff, *, outcome: str, promised_date: date | None = None, feedback: str = "") -> CallLog:
    if outcome not in CallLog.Outcome.values:
        raise BusinessRuleError("Choose the call outcome.")
    if outcome == CallLog.Outcome.PROMISED and not promised_date:
        raise BusinessRuleError("Enter the date the member promised to pay.")
    st = standing_mod.standing_for(m)
    log = CallLog.objects.create(membership=m, called_by=staff, outcome=outcome, promised_date=promised_date,
                                 feedback=feedback.strip(), arrears_at_call=st.arrears, days_overdue_at_call=st.days_overdue)
    m.called_at, m.called_by = timezone.now(), staff
    m.save(update_fields=["called_at", "called_by", "updated_at"])
    return log


def set_called(m: EqubMembership, staff, called: bool) -> EqubMembership:
    m.called_at = timezone.now() if called else None
    m.called_by = staff if called else None
    m.save(update_fields=["called_at", "called_by", "updated_at"])
    return m


def latest_calls(membership_ids) -> dict[int, CallLog]:
    out: dict[int, CallLog] = {}
    for log in CallLog.objects.filter(membership_id__in=list(membership_ids)).select_related("called_by").order_by("-created_at"):
        out.setdefault(log.membership_id, log)
    return out


# --------------------------------------------------------------------------- terminations
def request_termination(m: EqubMembership, staff, *, note: str = "") -> EqubMembership:
    if not m.is_live:
        raise BusinessRuleError("This place is not active.")
    m.termination_requested_at = timezone.now()
    m.termination_note = (note.strip() or f"Sent by {staff.full_name}")[:255]
    m.save(update_fields=["termination_requested_at", "termination_note", "updated_at"])
    return m


def cancel_termination_request(m: EqubMembership) -> EqubMembership:
    m.termination_requested_at = None
    m.termination_note = ""
    m.save(update_fields=["termination_requested_at", "termination_note", "updated_at"])
    return m


@transaction.atomic
def terminate(m: EqubMembership, staff, *, reason: str, note: str = "", notify_member: bool = True) -> Termination:
    m = EqubMembership.objects.select_for_update().select_related("group", "user").get(pk=m.pk)
    if not m.is_live:
        raise BusinessRuleError("This place is already closed.")
    if reason not in Termination.Reason.values:
        raise BusinessRuleError("Choose a reason.")
    st = standing_mod.standing_for(m)
    t = Termination.objects.create(
        membership=m, terminated_by=staff, reason=reason, note=note.strip(), arrears_at_termination=st.arrears,
        paid_at_termination=m.total_paid, missed_rounds=st.missed_rounds, days_overdue=st.days_overdue,
        had_won=m.has_won, member_notified=notify_member)
    m.status = EqubMembership.Status.DEFAULTED
    m.ended_at = timezone.now()
    m.termination_requested_at = None
    m.exit_requested_at = None
    m.save(update_fields=["status", "ended_at", "termination_requested_at", "exit_requested_at", "updated_at"])
    # Out before this round fell due: drop its unpaid slip so it isn't counted as owed.
    today = timezone.localdate()
    for c in Contribution.objects.select_related("round").filter(
            membership=m, status=Contribution.Status.PENDING, round__status=EqubRound.Status.COLLECTING):
        if today <= c.round.due_date + timedelta(days=m.group.grace_days):
            rnd = c.round
            c.delete()
            EqubRound.objects.filter(pk=rnd.pk).update(expected_amount=m.group.contribution_amount * rnd.contributions.count())
    if notify_member:
        why = {"NON_PAYMENT": "because contributions were not paid", "MEMBER_REQUEST": "as you requested",
               "UNREACHABLE": "because we could not reach you"}.get(reason, "")
        notify(m.user, Notification.Kind.EQUB_REMINDER, f"Your place in {m.group.name} was cancelled",
               f"Your Equb place has been cancelled {why}".strip() + ". Please contact Vacation Equb support "
               "about any contributions you have paid or still owe.", {"equb": m.group.slug, "membership": m.id})
    return t


@transaction.atomic
def reinstate(t: Termination, staff, *, notify_member: bool = True) -> EqubMembership:
    t = Termination.objects.select_for_update().select_related("membership__group", "membership__user").get(pk=t.pk)
    if t.reinstated_at:
        raise BusinessRuleError("Already reinstated.")
    m = t.membership
    if m.group.status != EqubGroup.Status.ACTIVE:
        raise BusinessRuleError("The Equb is no longer running, so the place can't be reinstated.")
    m.status = EqubMembership.Status.WON if m.has_won else EqubMembership.Status.ACTIVE
    m.ended_at = None
    m.standing_notified = ""
    m.save(update_fields=["status", "ended_at", "standing_notified", "updated_at"])
    t.reinstated_at, t.reinstated_by = timezone.now(), staff
    t.save(update_fields=["reinstated_at", "reinstated_by", "updated_at"])
    current = m.group.rounds.filter(status=EqubRound.Status.COLLECTING).first()
    if current:
        Contribution.objects.get_or_create(round=current, membership=m, defaults={"amount": m.group.contribution_amount})
        EqubRound.objects.filter(pk=current.pk).update(
            expected_amount=m.group.contribution_amount * current.contributions.count())
    if notify_member:
        notify(m.user, Notification.Kind.EQUB_REMINDER, f"Welcome back to {m.group.name}",
               "Your Equb place has been reinstated. Check the app for what is due next.",
               {"equb": m.group.slug, "membership": m.id})
    return m


# --------------------------------------------------------------------------- reminders & escalation
def send_due_reminders(rules: EqubSettings | None = None, today: date | None = None) -> int:
    rules = rules or EqubSettings.load()
    today = today or timezone.localdate()
    window_end = today + timedelta(days=rules.reminder_days_before)
    n = 0
    qs = (Contribution.objects.filter(status=Contribution.Status.PENDING, reminded_at__isnull=True,
                                      round__status=EqubRound.Status.COLLECTING, round__due_date__gte=today,
                                      round__due_date__lte=window_end,
                                      membership__status__in=EqubMembership.LIVE_STATUSES)
          .select_related("round__group", "membership__user"))
    for c in qs:
        days = (c.round.due_date - today).days
        when = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        g = c.round.group
        notify(c.membership.user, Notification.Kind.EQUB_REMINDER, f"{g.name}: contribution due {when}",
               f"Round {c.round.number} – {money(c.amount_due)} ETB is due on {c.round.due_date:%a %d %b}. "
               f"Pay in the app to stay in the draw on {c.round.draw_date:%d %b}.",
               {"equb": g.slug, "membership": c.membership_id, "round": c.round.number})
        Contribution.objects.filter(pk=c.pk).update(reminded_at=timezone.now())
        n += 1
    return n


_LEVEL = {"": 0, standing_mod.WARNED: 1, standing_mod.NEVER_PAID: 2, standing_mod.BLOCKED: 2, standing_mod.SUSPENDED: 3}


def _escalation_message(st: standing_mod.Standing, g: EqubGroup) -> tuple[str, str]:
    rounds = f"{st.missed_rounds} unpaid round{'s' if st.missed_rounds != 1 else ''}"
    owed = f"{money(st.arrears)} ETB"
    if st.status == standing_mod.NEVER_PAID:
        return (f"{g.name}: first contribution missing",
                f"We haven't received your first contribution ({owed}). Pay now to take part in the draws.")
    if st.status == standing_mod.SUSPENDED:
        return (f"{g.name}: your place is at risk",
                f"You have {rounds} ({owed}). Suspended places can be cancelled – please pay or contact us today.")
    if st.status == standing_mod.BLOCKED:
        return (f"{g.name}: you're out of the next draw",
                f"You have {rounds} ({owed}). You'll be back in the draw as soon as you catch up.")
    return (f"{g.name}: payment overdue",
            f"You have {rounds} ({owed}). Pay now to keep your place in the draw.")


def escalate_standings(rules: EqubSettings | None = None, today: date | None = None) -> dict:
    """Tell members once each time their standing gets worse; flag suspended places for termination."""
    rules = rules or EqubSettings.load()
    today = today or timezone.localdate()
    members = list(standing_mod.live_memberships().filter(group__status=EqubGroup.Status.ACTIVE))
    standings = standing_mod.standings_for(members, rules, today)
    out = {"notified": 0, "flagged": 0}
    for m in members:
        st = standings[m.id]
        level = st.status if st.status in _LEVEL else ""
        fields = {}
        if level != m.standing_notified:
            if level and rules.send_overdue_notices and _LEVEL[level] > _LEVEL.get(m.standing_notified, 0):
                title, body = _escalation_message(st, m.group)
                notify(m.user, Notification.Kind.EQUB_REMINDER, title, body, {"equb": m.group.slug, "membership": m.id})
                out["notified"] += 1
            fields["standing_notified"] = level
        if st.missed_rounds >= rules.suspend_at and rules.auto_flag_for_termination and not m.termination_requested_at:
            fields["termination_requested_at"] = timezone.now()
            fields["termination_note"] = f"Automatically flagged – {st.missed_rounds} rounds behind ({money(st.arrears)} ETB)"
            out["flagged"] += 1
        if fields:
            EqubMembership.objects.filter(pk=m.pk).update(**fields)
    return out


# --------------------------------------------------------------------------- re-joining after removal
def previous_place(user, group: EqubGroup) -> EqubMembership | None:
    """The user's own (not dependant) place in ``group`` if it was closed – terminated, or left after the start."""
    if not getattr(user, "is_authenticated", False):
        return None
    m = group.memberships.filter(user=user, dependent_name="").first()
    if m is None:
        return None
    if m.status == EqubMembership.Status.DEFAULTED:
        return m
    if m.status == EqubMembership.Status.LEFT and (m.exit_requested_at or m.terminations.exists()
                                                   or group.status != EqubGroup.Status.OPEN):
        return m
    return None


def rejoin_info(user, group: EqubGroup) -> dict | None:
    from .models import JoinRequest

    m = previous_place(user, group)
    if m is None:
        return None
    req = JoinRequest.objects.filter(group=group, user=user).order_by("-created_at").first()
    return {"needs_approval": True, "previous_position": m.position,
            "request": {"id": req.id, "status": req.status, "status_display": req.get_status_display(),
                        "created_at": req.created_at.isoformat(), "staff_note": req.staff_note} if req else None,
            "can_request": (group.status in (EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE)
                            and not (req and req.status == JoinRequest.Status.PENDING))}


@transaction.atomic
def request_rejoin(user, group: EqubGroup, message: str = ""):
    from .models import JoinRequest

    m = previous_place(user, group)
    if m is None:
        raise BusinessRuleError("You can join this Equb directly.")
    if group.status not in (EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE):
        raise BusinessRuleError("This Equb is not accepting members.")
    if JoinRequest.objects.filter(group=group, user=user, status=JoinRequest.Status.PENDING).exists():
        raise BusinessRuleError("Your request is already waiting for approval.")
    req = JoinRequest.objects.create(group=group, user=user, membership=m, message=(message or "").strip()[:500])
    notify(user, Notification.Kind.EQUB_REMINDER, "Request to rejoin sent",
           f"We received your request to join {group.name} again. You'll get a message when our team decides.",
           {"equb": group.slug, "join_request": req.id})
    return req


@transaction.atomic
def decide_rejoin(req, staff, *, approve: bool, note: str = "") -> EqubMembership | None:
    from . import services
    from .models import JoinRequest

    req = JoinRequest.objects.select_for_update().select_related("group", "user").get(pk=req.pk)
    if req.status != JoinRequest.Status.PENDING:
        raise BusinessRuleError("This request was already handled.")
    group = req.group
    req.decided_by, req.decided_at, req.staff_note = staff, timezone.now(), (note or "").strip()[:255]
    if not approve:
        req.status = JoinRequest.Status.REJECTED
        req.save(update_fields=["status", "decided_by", "decided_at", "staff_note", "updated_at"])
        notify(req.user, Notification.Kind.EQUB_REMINDER, f"Request to rejoin {group.name}",
               "Your request to join again was not approved." + (f" {req.staff_note}" if req.staff_note else ""),
               {"equb": group.slug, "join_request": req.id})
        return None
    if group.status not in (EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE):
        raise BusinessRuleError("This Equb is not accepting members any more.")
    if group.members_count >= group.max_members:
        raise BusinessRuleError("This Equb is full.")
    m = group.memberships.select_for_update().filter(user=req.user, dependent_name="").first()
    if m is None or m.is_live:
        raise BusinessRuleError("This person already has a place in the Equb.")
    open_termination = m.terminations.filter(reinstated_at__isnull=True).order_by("-created_at").first()
    if m.status == EqubMembership.Status.DEFAULTED and open_termination and group.status == EqubGroup.Status.ACTIVE:
        m = reinstate(open_termination, staff, notify_member=False)
    else:
        m.status = EqubMembership.Status.WON if m.has_won else EqubMembership.Status.ACTIVE
        m.ended_at = None
        m.exit_requested_at = None
        m.standing_notified = ""
        m.save(update_fields=["status", "ended_at", "exit_requested_at", "standing_notified", "updated_at"])
        if open_termination:
            open_termination.reinstated_at, open_termination.reinstated_by = timezone.now(), staff
            open_termination.save(update_fields=["reinstated_at", "reinstated_by", "updated_at"])
        services.sync_running_group(group)
    req.status, req.membership = JoinRequest.Status.APPROVED, m
    req.save(update_fields=["status", "membership", "decided_by", "decided_at", "staff_note", "updated_at"])
    notify(req.user, Notification.Kind.EQUB_REMINDER, f"Welcome back to {group.name}",
           f"Your request was approved – you hold place #{m.position} again."
           + (" You can pay now." if group.status == EqubGroup.Status.ACTIVE else ""),
           {"equb": group.slug, "membership": m.id})
    return m


# --------------------------------------------------------------------------- round planner
@transaction.atomic
def plan_rounds(group: EqubGroup, staff, changes: dict[int, tuple[date, date]], *, shift_following: bool = False,
                notify_members: bool = False) -> list[int]:
    """
    Set the exact due date and draw date of rounds that haven't been drawn yet.
    ``changes`` maps round number → (due date, draw date). With ``shift_following`` every later round that
    wasn't edited moves by the same number of days as the last edited one. Returns the round numbers that changed.
    """
    rounds = {r.number: r for r in group.rounds.select_for_update().order_by("number")}
    today = timezone.localdate()
    changed: list[int] = []
    due_shift = draw_shift = None
    for n in sorted(changes):
        r = rounds.get(n)
        due, draw = changes[n]
        if r is None or r.status == EqubRound.Status.DRAWN or (r.due_date, r.draw_date) == (due, draw):
            continue
        if draw < due:
            raise BusinessRuleError(f"Round {n}: the draw can't be before the due date.")
        if draw < today:
            raise BusinessRuleError(f"Round {n}: the draw date can't be in the past.")
        due_shift, draw_shift = due - r.due_date, draw - r.draw_date
        r.due_date, r.draw_date, r.dates_fixed = due, draw, True
        changed.append(n)
    if shift_following and changed and (due_shift or draw_shift):
        # later rounds move by the same days as the last edited one (only its draw date may have changed)
        last = changed[-1]
        for n, r in rounds.items():
            if n > last and n not in changed and r.status == EqubRound.Status.UPCOMING:
                r.due_date, r.draw_date, r.dates_fixed = r.due_date + due_shift, r.draw_date + draw_shift, True
                changed.append(n)
    # rounds must stay in order
    ordered = sorted(rounds.values(), key=lambda r: r.number)
    for a, b in zip(ordered, ordered[1:]):
        if b.due_date < a.due_date or b.draw_date < a.draw_date:
            raise BusinessRuleError(f"Round {b.number} can't be before round {a.number}. Check the dates.")
    for n in set(changed):
        rounds[n].save(update_fields=["due_date", "draw_date", "dates_fixed", "updated_at"])
    if changed and notify_members:
        lines = ", ".join(f"round {n}: due {rounds[n].due_date:%d %b}, draw {rounds[n].draw_date:%d %b}"
                          for n in sorted(set(changed))[:4])
        for m in group.memberships.filter(status__in=EqubMembership.LIVE_STATUSES).select_related("user"):
            notify(m.user, Notification.Kind.EQUB_REMINDER, f"{group.name}: new round dates",
                   f"Updated schedule – {lines}{'…' if len(set(changed)) > 4 else ''}.",
                   {"equb": group.slug, "membership": m.id})
    return sorted(set(changed))


@transaction.atomic
def reset_round_dates(group: EqubGroup) -> int:
    """Put every round not drawn yet back on the regular schedule (start date + frequency + grace days)."""
    from .services import due_date_for

    n = 0
    for r in group.rounds.select_for_update().exclude(status=EqubRound.Status.DRAWN):
        due = due_date_for(group, r.number)
        draw = max(due + timedelta(days=group.grace_days), timezone.localdate())
        if (r.due_date, r.draw_date, r.dates_fixed) != (due, draw, False):
            r.due_date, r.draw_date, r.dates_fixed = due, draw, False
            r.save(update_fields=["due_date", "draw_date", "dates_fixed", "updated_at"])
            n += 1
    return n
