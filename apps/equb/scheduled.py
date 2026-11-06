"""
Calendar pay – members choose the exact day and time they want to pay.

At that time the Chapa checkout for the chosen rounds is created and sent to the member's phone
(push + in-app notification with the payment link). They confirm it on Chapa's page with their
telebirr / CBE Birr / bank OTP. Chapa can't take money without the member confirming, so a scheduled
payment is a bill that arrives exactly when the member asked for it.
"""
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.payments.models import PaymentTransaction

from . import services
from .models import EqubGroup, EqubMembership, EqubRound, ScheduledPayment

MAX_OPEN_PER_PLACE = 12
MIN_LEAD = timedelta(minutes=2)
MAX_AHEAD_DAYS = 400


def _provider() -> str:
    """Scheduled bills always go through Chapa (the sandbox only while Chapa isn't configured in development)."""
    from django.conf import settings

    chapa = (getattr(settings, "PAYMENT_GATEWAYS", {}) or {}).get("chapa", {}) or {}
    if chapa.get("SECRET_KEY"):
        return "chapa"
    return "mock" if settings.VACATION.get("ENABLE_MOCK_GATEWAY") else "chapa"


def refresh_status(sp: ScheduledPayment) -> ScheduledPayment:
    """A sent bill becomes PAID once its Chapa payment succeeded."""
    if sp.status == ScheduledPayment.Status.SENT and sp.transaction_id:
        txn = sp.transaction
        if txn.status == PaymentTransaction.Status.SUCCESS:
            sp.status = ScheduledPayment.Status.PAID
            sp.save(update_fields=["status", "updated_at"])
        elif txn.is_final:
            sp.status = ScheduledPayment.Status.CANCELLED if txn.status == PaymentTransaction.Status.CANCELLED \
                else ScheduledPayment.Status.FAILED
            sp.note = "The bill was cancelled before it was paid." if sp.status == ScheduledPayment.Status.CANCELLED \
                else "The bill was not paid."
            sp.save(update_fields=["status", "note", "updated_at"])
    return sp


def create(user, m: EqubMembership, when: datetime, rounds: int = 1, repeat: str = "ONCE") -> ScheduledPayment:
    if m.user_id != user.id:
        raise BusinessRuleError("You can only schedule payments for your own Equb place.")
    if not m.is_live:
        raise BusinessRuleError("This place is no longer active.")
    group = m.group
    if group.status not in (EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE):
        raise BusinessRuleError("This Equb is not taking payments.")
    if timezone.is_naive(when):
        when = timezone.make_aware(when)
    now = timezone.now()
    if when < now + MIN_LEAD:
        raise BusinessRuleError("Choose a time at least a few minutes from now.")
    if when > now + timedelta(days=MAX_AHEAD_DAYS):
        raise BusinessRuleError("Choose a date within the next year.")
    if group.status == EqubGroup.Status.OPEN and timezone.localtime(when).date() < group.start_date:
        raise BusinessRuleError(f"Payments open when the Equb starts on {group.start_date:%d %b %Y}. "
                                "Choose that day or later.")
    if repeat not in ScheduledPayment.Repeat.values:
        repeat = ScheduledPayment.Repeat.ONCE
    if m.scheduled_payments.filter(status__in=ScheduledPayment.OPEN_STATUSES).count() >= MAX_OPEN_PER_PLACE:
        raise BusinessRuleError(f"You already have {MAX_OPEN_PER_PLACE} scheduled payments. Cancel one first.")
    if m.scheduled_payments.filter(status=ScheduledPayment.Status.SCHEDULED,
                                   scheduled_for__range=(when - timedelta(minutes=10), when + timedelta(minutes=10))).exists():
        raise BusinessRuleError("You already have a payment scheduled at that time.")
    rounds = max(1, min(int(rounds or 1), 60))
    sp = ScheduledPayment.objects.create(membership=m, user=user, scheduled_for=when, rounds=rounds, repeat=repeat)
    notify(user, Notification.Kind.EQUB_REMINDER, "Payment scheduled",
           f"We'll send your {group.name} payment for {rounds} round{'s' if rounds > 1 else ''} on "
           f"{timezone.localtime(when):%a %d %b at %H:%M}. You confirm it in Chapa.",
           {"equb": group.slug, "membership": m.id, "scheduled_payment": sp.id})
    return sp


def cancel(user, sp: ScheduledPayment) -> ScheduledPayment:
    if sp.user_id != user.id:
        raise BusinessRuleError("This isn't your scheduled payment.")
    refresh_status(sp)
    if sp.status not in ScheduledPayment.OPEN_STATUSES:
        raise BusinessRuleError("This payment can no longer be cancelled.")
    sp.status = ScheduledPayment.Status.CANCELLED
    sp.save(update_fields=["status", "updated_at"])
    return sp


def _next_due_after(m: EqubMembership, after: datetime):
    day = timezone.localtime(after).date()
    rnd = (m.group.rounds.filter(due_date__gt=day).exclude(status=EqubRound.Status.DRAWN)
           .order_by("due_date").first())
    return rnd.due_date if rnd else None


def _repeat(sp: ScheduledPayment):
    if sp.repeat != ScheduledPayment.Repeat.EVERY_DUE:
        return
    nxt = _next_due_after(sp.membership, sp.scheduled_for)
    if not nxt:
        return
    local = timezone.localtime(sp.scheduled_for)
    when = timezone.make_aware(datetime.combine(nxt, local.time().replace(second=0, microsecond=0)))
    if not sp.membership.scheduled_payments.filter(status=ScheduledPayment.Status.SCHEDULED,
                                                   scheduled_for=when).exists():
        ScheduledPayment.objects.create(membership=sp.membership, user=sp.user, scheduled_for=when, rounds=sp.rounds,
                                        repeat=sp.repeat)


def send(sp: ScheduledPayment) -> ScheduledPayment:
    """Create the Chapa checkout now and send it to the member."""
    with transaction.atomic():
        sp = ScheduledPayment.objects.select_for_update().select_related("membership__group", "user").get(pk=sp.pk)
        if sp.status != ScheduledPayment.Status.SCHEDULED:
            return sp
        m, group = sp.membership, sp.membership.group
        plan = services.payment_plan(m)
        if not plan["payable"]:
            sp.status = ScheduledPayment.Status.SKIPPED if m.is_live and group.status == EqubGroup.Status.ACTIVE \
                else ScheduledPayment.Status.FAILED
            sp.note = (plan["reason"] or "Nothing was due – you are paid up.")[:255]
            sp.sent_at = timezone.now()
            sp.save(update_fields=["status", "note", "sent_at", "updated_at"])
            failed = sp.status == ScheduledPayment.Status.FAILED
        else:
            options = plan["options"]
            option = (next((o for o in options if o["rounds"] == sp.rounds), None)
                      or next((o for o in reversed(options) if o["rounds"] <= sp.rounds), None) or options[0])
            try:
                txn = services.start_member_payment(sp.user, m, option["rounds"], _provider())
            except BusinessRuleError as exc:
                sp.status, sp.note, sp.sent_at = ScheduledPayment.Status.FAILED, str(exc.detail)[:255], timezone.now()
                sp.save(update_fields=["status", "note", "sent_at", "updated_at"])
                failed = True
            else:
                sp.status, sp.transaction, sp.amount, sp.sent_at = ScheduledPayment.Status.SENT, txn, txn.amount, timezone.now()
                sp.note = (f"Rounds {option['from_round']}–{option['to_round']}" if option["from_round"] != option["to_round"]
                           else f"Round {option['from_round']}")
                sp.save(update_fields=["status", "transaction", "amount", "sent_at", "note", "updated_at"])
                failed = False
        _repeat(sp)
    if sp.status == ScheduledPayment.Status.SENT:
        notify(sp.user, Notification.Kind.PAYMENT, f"Time to pay {services.money(sp.amount)} ETB",
               f"Your scheduled {group.name} payment ({sp.note}) is ready. Tap to pay with Chapa – "
               "confirm with your telebirr, CBE Birr or bank OTP.",
               {"equb": group.slug, "membership": m.id, "scheduled_payment": sp.id,
                "transaction": str(sp.transaction.reference), "checkout_url": sp.transaction.checkout_url})
    elif failed:
        notify(sp.user, Notification.Kind.EQUB_REMINDER, "Scheduled payment not sent",
               f"{group.name}: {sp.note}", {"equb": group.slug, "membership": m.id, "scheduled_payment": sp.id})
    return sp


def dispatch_due(now: datetime | None = None, membership: EqubMembership | None = None) -> int:
    """Send every scheduled payment whose time has come (run by the scheduler, and when the member opens the app)."""
    now = now or timezone.now()
    qs = ScheduledPayment.objects.filter(status=ScheduledPayment.Status.SCHEDULED, scheduled_for__lte=now)
    if membership is not None:
        qs = qs.filter(membership=membership)
    sent = 0
    for sp in list(qs.order_by("scheduled_for")[:200]):
        if send(sp).status == ScheduledPayment.Status.SENT:
            sent += 1
    return sent


def serialize(sp: ScheduledPayment) -> dict:
    refresh_status(sp)
    txn = sp.transaction
    return {
        "id": sp.id, "membership_id": sp.membership_id, "scheduled_for": sp.scheduled_for.isoformat(),
        "rounds": sp.rounds, "repeat": sp.repeat, "repeat_display": sp.get_repeat_display(),
        "status": sp.status, "status_display": sp.get_status_display(),
        "amount": str(sp.amount) if sp.amount is not None else None, "note": sp.note,
        "sent_at": sp.sent_at.isoformat() if sp.sent_at else None,
        "transaction": {"reference": str(txn.reference), "checkout_url": txn.checkout_url, "provider": txn.provider,
                        "status": txn.status} if txn else None,
        "can_cancel": sp.status in ScheduledPayment.OPEN_STATUSES,
    }
