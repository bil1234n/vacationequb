"""Community Finance & Impact: Crowdfunding Campaigns (Sadaqah / Hibah) and Community Giveaways (free, sponsored)."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.exceptions import BusinessRuleError
from apps.luckydraw import services as cf
from apps.luckydraw.models import (CrowdfundingCampaign, Donation, Giveaway, GiveawayEntry, GiveawayParticipant,
                                   GiveawayWinner)

from . import forms as f
from .views import days_back, labels_for, series, staff_required


def _render_form(request, **kw):
    from .manage_views import _render
    return _render(request, **kw)


# --------------------------------------------------------------------------- crowdfunding
@staff_required
def community(request):
    """Crowdfunding Campaigns tab."""
    status, q = request.GET.get("status", ""), request.GET.get("q", "").strip()
    qs = CrowdfundingCampaign.objects.all()
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(beneficiary_name__icontains=q))
    paid = Donation.objects.filter(status="PAID")
    totals = CrowdfundingCampaign.objects.aggregate(raised=Sum("raised_amount"), fees=Sum("fees_collected"),
                                                   disbursed=Sum("disbursed_amount"), goal=Sum("goal_amount"))
    days = days_back(30)
    charts = {"daily": {"type": "bar", "money": True, "labels": labels_for(days), "datasets": [
        {"label": "Gifts to campaigns", "data": series(paid, "paid_at", Sum("amount"), days), "color": "#0F382B"},
        {"label": "Platform fees", "data": series(paid, "paid_at", Sum("fee_amount"), days), "color": "#C9A227"}],
        "stacked": True},
        "kinds": {"type": "doughnut", "money": True, "labels": ["Sadaqah", "Hibah"], "datasets": [
            {"label": "Given", "data": [float(paid.filter(kind=k).aggregate(s=Sum("amount"))["s"] or 0) for k in ("SADAQAH", "HIBAH")]}]}}
    kpi = {"raised": totals["raised"] or 0, "fees": totals["fees"] or 0, "disbursed": totals["disbursed"] or 0,
           "to_pay": (totals["raised"] or 0) - (totals["disbursed"] or 0),
           "donors": paid.values("user").distinct().count(), "gifts": paid.count(),
           "live": CrowdfundingCampaign.objects.live().count(), "funded": CrowdfundingCampaign.objects.filter(status="FUNDED").count()}
    return render(request, "console/community.html", {
        "nav": "community", "tab": "campaigns", "camps": list(qs), "kpi": kpi, "charts": charts, "status": status, "q": q,
        "statuses": CrowdfundingCampaign.Status.choices})


@staff_required
def campaign_detail(request, slug):
    c = get_object_or_404(CrowdfundingCampaign, slug=slug)
    dform = f.DisbursementForm(request.POST if request.POST.get("action") == "disburse" else None,
                               initial={"paid_to": c.beneficiary_name, "paid_on": timezone.localdate(),
                                        "amount": c.available_to_disburse or None})
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "disburse" and dform.is_valid():
                d = dform.cleaned_data
                cf.record_disbursement(c, amount=d["amount"], paid_to=d["paid_to"], method=d["method"],
                                              reference=d["reference"], note=d["note"], paid_on=d["paid_on"], by=request.user)
                messages.success(request, f"{d['amount']:,.2f} ETB paid out to {d['paid_to']} was recorded.")
                return redirect("console:campaign", slug=slug)
            if action == "disburse":
                messages.error(request, "Check the payout details: " + "; ".join(
                    f"{dform.fields[k].label}: {v[0]}" for k, v in dform.errors.items() if k in dform.fields))
            elif action in ("close", "reopen", "go_live"):
                c.status = {"close": "CLOSED", "reopen": "LIVE", "go_live": "LIVE"}[action]
                c.save(update_fields=["status", "updated_at"])
                messages.success(request, {"close": "Campaign closed – no more donations.", "reopen": "Campaign reopened.",
                                           "go_live": "Campaign is live."}[action])
                return redirect("console:campaign", slug=slug)
        except BusinessRuleError as exc:
            messages.error(request, str(exc.detail))
            return redirect("console:campaign", slug=slug)
    start = timezone.localtime(c.starts_at).date()
    n = max((timezone.localdate() - start).days + 1, 1)
    days = [start + timedelta(days=i) for i in range(min(n, 60))]
    paid = c.donations.filter(status="PAID")
    charts = {"daily": {"type": "bar", "money": True, "labels": labels_for(days), "datasets": [
        {"label": "Gifts", "data": series(paid, "paid_at", Sum("amount"), days), "color": "#0F382B"}]}}
    return render(request, "console/campaign_detail.html", {
        "nav": "community", "c": c, "charts": charts, "dform": dform,
        "donations": c.donations.select_related("user").exclude(status="FAILED").order_by("-created_at")[:40],
        "disbursements": c.disbursements.select_related("recorded_by"),
        "kinds": {k: paid.filter(kind=k).aggregate(s=Sum("amount"), n=Count("id")) for k in ("SADAQAH", "HIBAH")},
        "avg": (paid.aggregate(s=Sum("amount"))["s"] or 0) / max(paid.count(), 1),
    })


@staff_required
def campaign_form(request, slug=None):
    obj = get_object_or_404(CrowdfundingCampaign, slug=slug) if slug else None
    form = f.CrowdfundingForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        c = form.save(commit=False)
        if not obj:
            c.created_by = request.user
        c.save()
        messages.success(request, f"Crowdfunding campaign “{c.title}” {'updated' if obj else 'created'}.")
        return redirect("console:campaign", slug=c.slug)
    return _render_form(request, nav="community", title=f"Edit {obj.title}" if obj else "New Crowdfunding Campaign",
                        crumbs="Community Finance & Impact", form=form, obj=obj,
                        cancel=reverse("console:campaign", args=[obj.slug]) if obj else reverse("console:community"),
                        delete_url=reverse("console:campaign_delete", args=[obj.slug]) if obj and not obj.donations.exists() else None)


@staff_required
@require_POST
def campaign_delete(request, slug):
    obj = get_object_or_404(CrowdfundingCampaign, slug=slug)
    if obj.donations.exists():
        messages.error(request, "People have given to this campaign; close or cancel it instead.")
        return redirect("console:campaign", slug=slug)
    obj.delete()
    messages.success(request, "Campaign deleted.")
    return redirect("console:community")


# --------------------------------------------------------------------------- giveaways
@staff_required
def giveaways(request):
    status = request.GET.get("status", "")
    qs = Giveaway.objects.all()
    if status:
        qs = qs.filter(status=status)
    days = days_back(30)
    charts = {"daily": {"type": "bar", "labels": labels_for(days), "datasets": [
        {"label": "Free entries earned", "data": series(GiveawayEntry.objects.all(), "created_at", Sum("count"), days), "color": "#C9A227"}]}}
    agg = Giveaway.objects.aggregate(entries=Sum("entries_issued"), refs=Sum("referrals_count"),
                                     value=Sum("prize_value"))
    kpi = {"live": Giveaway.objects.live().count(), "entries": agg["entries"] or 0, "referrals": agg["refs"] or 0,
           "participants": GiveawayParticipant.objects.filter(referrals__gt=0).values("user").distinct().count(),
           "winners": GiveawayWinner.objects.count(),
           "sponsored": sum((g.prize_value * g.number_of_winners for g in Giveaway.objects.exclude(status="CANCELLED")), Decimal("0"))}
    return render(request, "console/community.html", {
        "nav": "giveaways", "tab": "giveaways", "gives": list(qs), "kpi": kpi, "charts": charts, "status": status,
        "statuses": Giveaway.Status.choices})


@staff_required
def giveaway_detail(request, slug):
    g = get_object_or_404(Giveaway, slug=slug)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "draw":
                if g.status == Giveaway.Status.LIVE:
                    g.status = Giveaway.Status.CLOSED
                    g.save(update_fields=["status"])
                winners = cf.close_and_draw(g)
                messages.success(request, f"{len(winners)} winner(s) drawn at random and notified.")
            elif action == "recount":
                n = cf.recount(g)
                messages.success(request, f"Referrals recounted for {n} inviter(s).")
            elif action == "close":
                g.status = Giveaway.Status.CLOSED
                g.save(update_fields=["status"])
                messages.success(request, "Referrals closed. Run the draw when you're ready.")
        except BusinessRuleError as exc:
            messages.error(request, str(exc.detail))
        return redirect("console:giveaway", slug=slug)
    start = timezone.localtime(g.starts_at).date()
    end = min(timezone.localdate(), timezone.localtime(g.ends_at).date())
    n = max((end - start).days + 1, 1)
    days = [start + timedelta(days=i) for i in range(min(n, 60))]
    charts = {"daily": {"type": "bar", "labels": labels_for(days), "datasets": [
        {"label": "Free entries earned", "data": series(GiveawayEntry.objects.filter(giveaway=g), "created_at", Sum("count"), days),
         "color": "#C9A227"}]}}
    return render(request, "console/giveaway_detail.html", {
        "nav": "giveaways", "g": g, "charts": charts,
        "participants": g.participants.filter(referrals__gt=0).select_related("user").order_by("-entries", "-referrals")[:50],
        "close_to": g.participants.filter(entries=0, referrals__gt=0).count(),
        "winners": g.winners.select_related("user", "claimed_booking"), "verification": cf.verify(g),
    })


@staff_required
def giveaway_form(request, slug=None):
    obj = get_object_or_404(Giveaway, slug=slug) if slug else None
    form = f.GiveawayForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        g = form.save()
        if g.status == Giveaway.Status.LIVE:
            cf.recount(g)
        messages.success(request, f"Giveaway “{g.title}” {'updated' if obj else 'created'}.")
        return redirect("console:giveaway", slug=g.slug)
    return _render_form(request, nav="giveaways", title=f"Edit {obj.title}" if obj else "New Community Giveaway",
                        crumbs="Community Finance & Impact", form=form, obj=obj,
                        cancel=reverse("console:giveaway", args=[obj.slug]) if obj else reverse("console:giveaways"),
                        delete_url=reverse("console:giveaway_delete", args=[obj.slug]) if obj and not obj.entries_issued else None)


@staff_required
@require_POST
def giveaway_delete(request, slug):
    obj = get_object_or_404(Giveaway, slug=slug)
    if obj.entries_issued:
        messages.error(request, "People have earned entries; cancel the giveaway instead.")
        return redirect("console:giveaway", slug=slug)
    obj.delete()
    messages.success(request, "Giveaway deleted.")
    return redirect("console:giveaways")


@staff_required
def old_lucky_redirect(request, slug=None):
    return redirect("console:community")
