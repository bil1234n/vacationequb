"""
Community Finance & Impact services.

Crowdfunding: a donor gives ``amount`` (100% to the cause) and pays ``amount + fee`` – the platform fee is a separate,
explicit add-on. Donations never earn entries or prizes.

Giveaways: free entries, earned only by referrals – every ``referrals_per_entry`` friends (10) who register on EthioVacation
with your code during the giveaway = 1 entry. Entries are issued as contiguous number ranges under a row lock and
the winners are picked with the commit/reveal draw in ``apps.core.fairness`` (anyone can verify it afterwards).
"""
from __future__ import annotations

import bisect
import hashlib
import logging
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.core import fairness
from apps.core.exceptions import BusinessRuleError
from apps.core.utils import generate_code
from apps.notifications.models import Notification
from apps.notifications.services import notify
from apps.payments.models import PaymentTransaction
from apps.payments.services import initiate_payment

from .models import (CrowdfundingCampaign, Disbursement, Donation, Giveaway, GiveawayEntry, GiveawayParticipant,
                     GiveawayWinner)

log = logging.getLogger(__name__)
CENT = Decimal("0.01")


def money(v) -> str:
    v = Decimal(v or 0).quantize(CENT)
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"


def mask(name: str) -> str:
    parts = (name or "EthioVacation user").split()
    return parts[0] + (f" {parts[1][0]}." if len(parts) > 1 else "")


# =========================================================================== crowdfunding
def quote(campaign: CrowdfundingCampaign, amount) -> dict:
    """The checkout breakdown: gift (to the cause) + platform fee (to EthioVacation) = total."""
    amount = Decimal(amount).quantize(CENT)
    fee = campaign.fee_for(amount)
    return {"amount": amount, "fee_percent": campaign.platform_fee_percent, "fee": fee, "total": amount + fee}


def start_donation(user, campaign: CrowdfundingCampaign, amount, *, kind: str = Donation.Kind.DONATION, provider=None,
                   payer_phone: str = "", is_anonymous: bool = False, message: str = "") -> tuple[Donation, PaymentTransaction]:
    amount = Decimal(amount)
    if not campaign.is_live:
        raise BusinessRuleError("This campaign is not accepting donations right now.")
    if amount != amount.to_integral_value() or amount < campaign.min_donation:
        raise BusinessRuleError(f"Give a whole amount of at least {campaign.min_donation} ETB.")
    if amount > 1_000_000:
        raise BusinessRuleError("For gifts over 1,000,000 ETB please contact our team.")
    q = quote(campaign, amount)
    donation = Donation.objects.create(campaign=campaign, user=user, kind=kind, amount=q["amount"], fee_amount=q["fee"],
                                       total_amount=q["total"], is_anonymous=is_anonymous, message=(message or "")[:200])
    txn = initiate_payment(user=user, target=donation, amount=q["total"], purpose=PaymentTransaction.Purpose.DONATION,
                           provider=provider, payer_phone=payer_phone)
    return donation, txn


def on_donation_paid(txn):
    with transaction.atomic():
        d = Donation.objects.select_for_update().select_related("campaign").get(pk=txn.object_id)
        if d.status == Donation.Status.PAID:
            return
        first_from_user = not Donation.objects.filter(campaign=d.campaign, user=d.user, status=Donation.Status.PAID).exists()
        d.status, d.paid_at = Donation.Status.PAID, timezone.now()
        d.save(update_fields=["status", "paid_at", "updated_at"])
        CrowdfundingCampaign.objects.filter(pk=d.campaign_id).update(
            raised_amount=F("raised_amount") + d.amount, fees_collected=F("fees_collected") + d.fee_amount,
            donations_count=F("donations_count") + 1, donors_count=F("donors_count") + (1 if first_from_user else 0))
        c = CrowdfundingCampaign.objects.get(pk=d.campaign_id)
        reached = c.status == CrowdfundingCampaign.Status.LIVE and c.raised_amount >= c.goal_amount
        if reached:
            CrowdfundingCampaign.objects.filter(pk=c.pk).update(status=CrowdfundingCampaign.Status.FUNDED)
    notify(d.user, Notification.Kind.DONATION, "JazakAllahu khayran 🤲",
           f"Your {d.get_kind_display().split(' (')[0]} of {money(d.amount)} ETB to “{c.title}” was received. "
           f"{c.progress_percent:.0f}% of the goal is now raised.", {"campaign": c.slug, "donation": d.id})
    if c.beneficiary_id and c.beneficiary_id != d.user_id:
        notify(c.beneficiary, Notification.Kind.DONATION, "Someone gave to your journey 🤲",
               f"{'An anonymous donor' if d.is_anonymous else mask(d.user.full_name)} gave {money(d.amount)} ETB to "
               f"“{c.title}”.", {"campaign": c.slug})
    if reached:
        for uid in Donation.objects.filter(campaign=c, status=Donation.Status.PAID).values_list("user_id", flat=True).distinct():
            notify(get_user_model().objects.get(pk=uid), Notification.Kind.DONATION, "Goal reached 🎉",
                   f"“{c.title}” reached its goal of {money(c.goal_amount)} ETB. Thank you for your support.", {"campaign": c.slug})


def on_donation_failed(txn):
    Donation.objects.filter(pk=txn.object_id, status=Donation.Status.PENDING).update(status=Donation.Status.FAILED)


@transaction.atomic
def record_disbursement(campaign: CrowdfundingCampaign, *, amount, paid_to: str, method: str = "Bank transfer",
                        reference: str = "", note: str = "", paid_on=None, by=None) -> Disbursement:
    c = CrowdfundingCampaign.objects.select_for_update().get(pk=campaign.pk)
    amount = Decimal(amount).quantize(CENT)
    if amount <= 0:
        raise BusinessRuleError("Enter the amount paid out.")
    if amount > c.available_to_disburse:
        raise BusinessRuleError(f"Only {money(c.available_to_disburse)} ETB is available to pay out.")
    d = Disbursement.objects.create(campaign=c, amount=amount, paid_to=(paid_to or c.beneficiary_name)[:150],
                                    method=method[:40], reference=reference[:80], note=note[:255],
                                    paid_on=paid_on or timezone.localdate(), recorded_by=by)
    CrowdfundingCampaign.objects.filter(pk=c.pk).update(disbursed_amount=F("disbursed_amount") + amount)
    if c.beneficiary_id:
        notify(c.beneficiary, Notification.Kind.DONATION, "Funds sent to you",
               f"{money(amount)} ETB from “{c.title}” was paid to {d.paid_to} ({d.method}).", {"campaign": c.slug})
    return d


def recent_donors(campaign, limit: int = 20) -> list[dict]:
    qs = (Donation.objects.filter(campaign=campaign, status=Donation.Status.PAID).select_related("user")
          .order_by("-paid_at")[:limit])
    return [{"name": "Anonymous" if d.is_anonymous else mask(d.user.full_name), "amount": str(d.amount),
             "kind": d.kind, "message": d.message, "paid_at": d.paid_at} for d in qs]


# =========================================================================== giveaways
def referrals_in_window(giveaway: Giveaway, user) -> int:
    """Friends who registered with ``user``'s code while the giveaway was running."""
    return get_user_model().objects.filter(referred_by=user, is_active=True, date_joined__gte=giveaway.starts_at,
                                           date_joined__lt=giveaway.ends_at).count()


def user_entries(giveaway, user) -> int:
    return GiveawayEntry.objects.filter(giveaway=giveaway, user=user).aggregate(s=Sum("count"))["s"] or 0


def _sync_participant(giveaway_id: int, user, notify_user: bool = True) -> GiveawayParticipant | None:
    """Recount ``user``'s referrals in a giveaway and issue any free entries they have earned."""
    g = Giveaway.objects.select_for_update().get(pk=giveaway_id)
    refs = referrals_in_window(g, user)
    p, created = GiveawayParticipant.objects.get_or_create(giveaway=g, user=user)
    if refs == p.referrals and not created:
        return p
    Giveaway.objects.filter(pk=g.pk).update(
        referrals_count=F("referrals_count") + (refs - p.referrals),
        participants_count=F("participants_count") + (1 if (p.referrals == 0 and refs > 0) else 0))
    due = refs // g.referrals_per_entry
    if g.max_entries_per_user:
        due = min(due, g.max_entries_per_user)
    new = due - p.entries
    block = None
    if new > 0 and g.is_live:
        first = g.entries_issued + 1
        block = GiveawayEntry.objects.create(giveaway=g, user=user, first_ticket=first, last_ticket=first + new - 1, count=new)
        Giveaway.objects.filter(pk=g.pk).update(entries_issued=F("entries_issued") + new)
        p.entries = due
    p.referrals = refs
    p.save(update_fields=["referrals", "entries", "updated_at"])
    if notify_user and g.is_live:
        if block:
            notify(user, Notification.Kind.LUCKY_ENTRY, "🎁 Free entry unlocked!",
                   f"{refs} friends joined with your code – you now have {p.entries} free entr{'y' if p.entries == 1 else 'ies'} "
                   f"in “{g.title}” (#{block.first_ticket}{'–#' + str(block.last_ticket) if block.count > 1 else ''}).",
                   {"giveaway": g.slug})
        else:
            left = g.referrals_per_entry - refs % g.referrals_per_entry
            notify(user, Notification.Kind.PROMO, "Your friend joined EthioVacation 🎉",
                   f"{refs % g.referrals_per_entry}/{g.referrals_per_entry} friends for your next free entry in “{g.title}” – "
                   f"{left} more to go.", {"giveaway": g.slug})
    return p


def on_referral_signup(*, referrer, new_user):
    """A friend registered with ``referrer``'s code: update their progress in every live giveaway."""
    live = list(Giveaway.objects.live().values_list("pk", flat=True))
    if not live:
        notify(referrer, Notification.Kind.PROMO, "Your friend joined EthioVacation 🎉",
               f"{new_user.first_name} signed up with your code. Invite friends during a giveaway to earn free entries.", {})
        return
    for pk in live:
        with transaction.atomic():
            _sync_participant(pk, referrer)


def recount(giveaway: Giveaway) -> int:
    """Staff: recount everyone's referrals in the window (e.g. after editing the dates). Returns participants."""
    referrers = (get_user_model().objects.filter(referrals__date_joined__gte=giveaway.starts_at,
                                                 referrals__date_joined__lt=giveaway.ends_at,
                                                 referrals__is_active=True).distinct())
    n = 0
    for u in referrers:
        with transaction.atomic():
            _sync_participant(giveaway.pk, u, notify_user=False)
        n += 1
    return n


def progress(giveaway: Giveaway, user) -> dict:
    p = GiveawayParticipant.objects.filter(giveaway=giveaway, user=user).first()
    refs = p.referrals if p else referrals_in_window(giveaway, user)
    entries = p.entries if p else 0
    n = giveaway.referrals_per_entry
    capped = bool(giveaway.max_entries_per_user and entries >= giveaway.max_entries_per_user)
    return {"referrals": refs, "entries": entries, "referrals_per_entry": n, "towards_next": refs % n,
            "needed_for_next": 0 if capped else n - refs % n, "max_entries": giveaway.max_entries_per_user,
            "capped": capped, "referral_code": user.referral_code,
            "tickets": [{"first": e.first_ticket, "last": e.last_ticket}
                        for e in GiveawayEntry.objects.filter(giveaway=giveaway, user=user)]}


def _snapshot(g) -> tuple[list[GiveawayEntry], str]:
    blocks = list(GiveawayEntry.objects.filter(giveaway=g).order_by("first_ticket"))
    digest = hashlib.sha256(";".join(f"{b.first_ticket}-{b.last_ticket}:{b.user_id}" for b in blocks).encode()).hexdigest()
    return blocks, f"giveaway:{g.id}|entries:{g.entries_issued}|ledger:{digest}"


def select_winning_tickets(seed, public_input, blocks, total, k) -> list[tuple[int, GiveawayEntry]]:
    """Uniform pick over entry numbers; one prize per person."""
    starts = [b.first_ticket for b in blocks]
    picked, users, counter = [], set(), 0
    k = min(k, len({b.user_id for b in blocks}))
    while len(picked) < k:
        ticket = fairness.pick_index(seed, public_input, total, counter) + 1
        counter += 1
        block = blocks[bisect.bisect_right(starts, ticket) - 1]
        if block.user_id in users:
            continue
        picked.append((ticket, block))
        users.add(block.user_id)
    return picked


@transaction.atomic
def close_and_draw(giveaway: Giveaway) -> list[GiveawayWinner]:
    g = Giveaway.objects.select_for_update().get(pk=giveaway.pk)
    if g.status in (Giveaway.Status.DRAWN, Giveaway.Status.CANCELLED):
        raise BusinessRuleError("This giveaway has already been drawn or was cancelled.")
    if g.status == Giveaway.Status.LIVE and timezone.now() < g.ends_at:
        raise BusinessRuleError("Referrals are still open; close the giveaway before drawing.")
    if g.entries_issued == 0:
        raise BusinessRuleError(f"Nobody has earned an entry yet ({g.referrals_per_entry} referrals = 1 entry).")
    blocks, public_input = _snapshot(g)
    picks = select_winning_tickets(g.server_seed, public_input, blocks, g.entries_issued, g.number_of_winners)
    winners = [GiveawayWinner.objects.create(giveaway=g, user=b.user, entry=b, ticket_number=t, rank=rank,
                                             prize_value=g.prize_value, voucher_code="GV" + generate_code(8))
               for rank, (t, b) in enumerate(picks, start=1)]
    g.status, g.public_input, g.drawn_at = Giveaway.Status.DRAWN, public_input, timezone.now()
    g.save()
    for w in winners:
        notify(w.user, Notification.Kind.LUCKY_WIN, "🏆 You won the giveaway!",
               f"Entry #{w.ticket_number} won “{g.prize_title}” in {g.title}, sponsored by {g.sponsor_name}. Tap to claim it.",
               {"giveaway": g.slug, "voucher": w.voucher_code})
    winner_ids = {w.user_id for w in winners}
    for uid in GiveawayEntry.objects.filter(giveaway=g).exclude(user_id__in=winner_ids).values_list("user_id", flat=True).distinct():
        notify(get_user_model().objects.get(pk=uid), Notification.Kind.LUCKY_ENTRY, "Giveaway results are in",
               f"The winners of {g.title} were announced. Keep inviting friends – new giveaways are coming!", {"giveaway": g.slug})
    return winners


def verify(g: Giveaway) -> dict:
    if g.status != Giveaway.Status.DRAWN:
        return {"drawn": False, "seed_commitment": g.seed_commitment, "entries_issued": g.entries_issued}
    blocks, public_input = _snapshot(g)
    recorded = list(g.winners.order_by("rank").values_list("ticket_number", flat=True))
    recomputed = [t for t, _ in select_winning_tickets(g.server_seed, public_input, blocks, g.entries_issued,
                                                       g.number_of_winners)] if blocks else []
    return {"drawn": True, "seed_commitment": g.seed_commitment, "server_seed": g.server_seed,
            "commitment_ok": fairness.commitment_for(g.server_seed) == g.seed_commitment,
            "public_input": public_input, "public_input_matches": public_input == g.public_input,
            "entries_issued": g.entries_issued, "recorded_tickets": recorded, "recomputed_tickets": recomputed,
            "matches": bool(blocks) and recorded == recomputed and public_input == g.public_input}


@transaction.atomic
def claim_prize(winner: GiveawayWinner, *, booking_reference: str | None = None) -> GiveawayWinner:
    from apps.equb.models import EqubMembership
    from apps.equb.services import apply_credit, join_group
    from apps.travel.models import Booking
    from apps.travel.services.bookings import book_with_credit

    winner = GiveawayWinner.objects.select_for_update().select_related("giveaway").get(pk=winner.pk)
    if winner.claimed_at:
        raise BusinessRuleError("This prize has already been claimed.")
    g = winner.giveaway
    if g.prize_type == Giveaway.PrizeType.EQUB_CONTRIBUTION:
        if not g.prize_equb_group:
            raise BusinessRuleError("No Equb is attached to this prize; contact support.")
        m = EqubMembership.objects.filter(group=g.prize_equb_group, user=winner.user, dependent_name="").exclude(status="LEFT").first()
        if not m:
            m = join_group(winner.user, g.prize_equb_group)
        EqubMembership.objects.filter(pk=m.pk).update(prepaid_credit=F("prepaid_credit") + winner.prize_value)
        apply_credit(m)
        winner.claimed_equb_membership = m
    else:
        if not booking_reference:
            raise BusinessRuleError("Create a booking first, then claim the prize against it.")
        booking = Booking.objects.filter(reference=booking_reference.upper(), user=winner.user).first()
        if not booking or booking.status != Booking.Status.PENDING_PAYMENT:
            raise BusinessRuleError("Booking not found or not awaiting payment.")
        if g.prize_type == Giveaway.PrizeType.TRAVEL_PACKAGE and g.prize_package_id and booking.package_id != g.prize_package_id:
            raise BusinessRuleError("This prize is valid only for its travel package.")
        book_with_credit(booking, winner.prize_value, Booking.Funding.LUCKY_DRAW, winner.voucher_code)
        winner.claimed_booking = booking
    winner.claimed_at = timezone.now()
    winner.save()
    return winner


def leaderboard(g: Giveaway, limit: int = 20) -> list[dict]:
    rows = GiveawayParticipant.objects.filter(giveaway=g, referrals__gt=0).select_related("user").order_by("-referrals")[:limit]
    return [{"rank": i, "name": mask(p.user.full_name), "referrals": p.referrals, "entries": p.entries}
            for i, p in enumerate(rows, 1)]


def run_schedule(now=None) -> dict:
    """Scheduler: open scheduled giveaways, close finished ones, draw those whose draw time has come."""
    now = now or timezone.now()
    Giveaway.objects.filter(status="SCHEDULED", starts_at__lte=now).update(status="LIVE")
    Giveaway.objects.filter(status="LIVE", ends_at__lte=now).update(status="CLOSED")
    CrowdfundingCampaign.objects.filter(status="LIVE", ends_at__isnull=False, ends_at__lte=now).update(status="CLOSED")
    drawn = 0
    for g in Giveaway.objects.filter(status="CLOSED", draw_at__lte=now, entries_issued__gt=0):
        try:
            close_and_draw(g)
            drawn += 1
        except BusinessRuleError as exc:
            log.warning("giveaway %s: %s", g, exc.detail)
    return {"giveaways_drawn": drawn}

