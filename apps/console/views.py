"""
Rihla staff console — a server-rendered web dashboard for operations staff.

Every page is staff-only. Charts are rendered client-side with Chart.js from a
single JSON config emitted per page (see ``static/console/console.js``).
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout, update_session_auth_hash
from django.contrib.auth.forms import PasswordChangeForm
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.db.models.functions import TruncDate
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User, normalize_phone
from apps.core.exceptions import BusinessRuleError
from apps.equb.models import Contribution, EqubGroup, EqubMembership, EqubRound
from apps.luckydraw.models import CrowdfundingCampaign, Donation, Giveaway, GiveawayEntry, GiveawayWinner
from apps.payments.models import PaymentTransaction
from apps.travel.models import Booking, Flight, Seat, Ticket, TravelPackage
from apps.travel.services.pricing import current_fare
from . import forms as f

PAID = PaymentTransaction.Status.SUCCESS
BOOKING_STATUS_ORDER = [("CONFIRMED", "Confirmed"), ("PENDING_PAYMENT", "Awaiting payment"), ("CANCELLED", "Cancelled"),
                        ("REFUNDED", "Refunded"), ("EXPIRED", "Expired (unpaid)")]
BOOKING_STATUS_COLORS = {"CONFIRMED": "#0F382B", "PENDING_PAYMENT": "#C9A227", "CANCELLED": "#C0392B",
                         "REFUNDED": "#E8A598", "EXPIRED": "#B7C4BE"}
PURPOSE_LABELS = dict(PaymentTransaction.Purpose.choices)
PER_PAGE = 25


# --------------------------------------------------------------------------- helpers
def staff_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('console:login')}?next={request.get_full_path()}")
        if not request.user.is_staff:
            logout(request)
            messages.error(request, "The console is for Rihla staff accounts only.")
            return redirect("console:login")
        return view(request, *args, **kwargs)
    return wrapper


def days_back(n: int):
    today = timezone.localdate()
    return [today - timedelta(days=i) for i in range(n - 1, -1, -1)]


def series(qs, date_field: str, value, days: list, extra_filter: dict | None = None):
    """Aggregate ``value`` per day over ``days`` → list aligned with days."""
    start = days[0]
    rows = (qs.filter(**{f"{date_field}__date__gte": start}, **(extra_filter or {}))
            .annotate(d=TruncDate(date_field)).values("d").annotate(v=value))
    by_day = {r["d"]: r["v"] or 0 for r in rows}
    return [float(by_day.get(d, 0)) for d in days]


def labels_for(days):
    return [d.strftime("%d %b") for d in days]


def page(request, qs):
    return Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))


def pct(a, b):
    return round(100 * float(a) / float(b), 1) if b else 0.0


def growth(cur, prev):
    if not prev:
        return None
    return round(100 * (float(cur) - float(prev)) / float(prev), 1)


# --------------------------------------------------------------------------- auth
def login_view(request):
    if request.user.is_authenticated and request.user.is_staff:
        return redirect("console:dashboard")
    error = None
    if request.method == "POST":
        phone = normalize_phone(request.POST.get("phone", ""))
        user = authenticate(request, username=phone, password=request.POST.get("password", ""))
        if user is None:
            error = "Incorrect phone number or password."
        elif not user.is_staff:
            error = "This account does not have staff access."
        else:
            login(request, user)
            nxt = request.GET.get("next", "")
            return redirect(nxt if nxt.startswith("/console/") else "console:dashboard")
    return render(request, "console/login.html", {"error": error, "phone": request.POST.get("phone", "")})


@require_POST
def logout_view(request):
    logout(request)
    messages.success(request, "You have been signed out.")
    return redirect("console:login")


@staff_required
def profile(request):
    user = request.user
    pw_form = PasswordChangeForm(user)
    if request.method == "POST":
        if request.POST.get("form") == "profile":
            user.full_name = request.POST.get("full_name", user.full_name).strip() or user.full_name
            user.email = request.POST.get("email", "").strip()
            user.save(update_fields=["full_name", "email"])
            messages.success(request, "Profile updated.")
            return redirect("console:profile")
        pw_form = PasswordChangeForm(user, request.POST)
        if pw_form.is_valid():
            pw_form.save()
            update_session_auth_hash(request, pw_form.user)
            messages.success(request, "Password changed.")
            return redirect("console:profile")
    activity = {
        "draws_run": EqubRound.objects.filter(draw__executed_by=user).count(),
        "last_login": user.last_login,
        "joined": user.date_joined,
    }
    return render(request, "console/profile.html", {"pw_form": pw_form, "activity": activity, "nav": "profile"})


# --------------------------------------------------------------------------- dashboard
@staff_required
def dashboard(request):
    period = int(request.GET.get("days", 30)) if request.GET.get("days", "30").isdigit() else 30
    period = period if period in (7, 30, 90) else 30
    days = days_back(period)
    since = timezone.now() - timedelta(days=period)
    prev_since = since - timedelta(days=period)
    # Money kept: successful payments minus anything refunded since (fully refunded payments net to zero).
    # Crowdfunding gifts belong to the people they help, so they are not company revenue.
    paid = PaymentTransaction.objects.filter(status__in=[PAID, PaymentTransaction.Status.REFUNDED]).exclude(
        purpose__in=["DONATION", "MICRO_DONATION"])
    net = Sum(F("amount") - F("refunded_amount"))

    revenue = paid.filter(completed_at__gte=since).aggregate(s=net)["s"] or 0
    revenue_prev = paid.filter(completed_at__gte=prev_since, completed_at__lt=since).aggregate(s=net)["s"] or 0
    confirmed = Booking.objects.filter(status="CONFIRMED", confirmed_at__gte=since).count()
    confirmed_prev = Booking.objects.filter(status="CONFIRMED", confirmed_at__gte=prev_since, confirmed_at__lt=since).count()
    customers = User.objects.filter(is_staff=False)
    total_customers = customers.count()
    new_customers = customers.filter(date_joined__gte=since).count()
    equb_saved = Contribution.objects.filter(status="PAID", paid_at__gte=since).aggregate(s=Sum("amount"))["s"] or 0
    gifts = Donation.objects.filter(status="PAID", paid_at__gte=since).aggregate(s=Sum("amount"), f=Sum("fee_amount"))
    crowd_raised, crowd_fees = gifts["s"] or 0, gifts["f"] or 0
    rate = Decimal(str(settings.RIHLA["COMPANY_PROFIT_PERCENT"]))
    profit = (Decimal(revenue) * rate / 100).quantize(Decimal("0.01"))
    profit_prev = Decimal(revenue_prev) * rate / 100

    kpis = [
        {"label": "Revenue (net of refunds)", "value": revenue, "money": True, "growth": growth(revenue, revenue_prev), "icon": "cash", "dark": True},
        {"label": "Confirmed bookings", "value": confirmed, "growth": growth(confirmed, confirmed_prev), "icon": "plane"},
        {"label": "Customers", "value": f"{total_customers:,}", "sub": f"+{new_customers} joined in the last {period} days", "icon": "users"},
        {"label": f"Company profit ({rate.normalize()}%)", "value": profit, "money": True, "growth": growth(profit, profit_prev),
         "sub": f"{rate.normalize()}% of {revenue:,.0f} ETB revenue", "icon": "check", "gold": True},
        {"label": "Equb savings", "value": equb_saved, "money": True, "sub": f"{EqubMembership.objects.exclude(status='LEFT').count()} active memberships", "icon": "people"},
        {"label": "Crowdfunding raised", "value": crowd_raised, "money": True, "sub": f"+{crowd_fees:,.0f} ETB platform fees · {GiveawayEntry.objects.filter(created_at__gte=since).aggregate(s=Sum('count'))['s'] or 0} free giveaway entries", "icon": "gift"},
        {"label": "Seats sold (upcoming)", "value": Seat.objects.filter(status="BOOKED", flight__departure_time__gt=timezone.now()).count(),
         "sub": f"{Flight.objects.filter(departure_time__gt=timezone.now()).count()} scheduled flights", "icon": "seat"},
        {"label": "Tickets issued", "value": Ticket.objects.filter(created_at__gte=since).count(), "sub": "e-tickets with signed QR", "icon": "ticket"},
    ]

    rev_by_purpose = [
        {"label": PURPOSE_LABELS[p].split(" /")[0].split(" (")[0], "data": series(paid, "completed_at", net, days, {"purpose": p})}
        for p in ("BOOKING", "EQUB_CONTRIBUTION")
    ]
    status_counts = dict(Booking.objects.values_list("status").annotate(c=Count("id")))
    provider_rows = paid.filter(completed_at__gte=since).values("provider").annotate(s=net).order_by("-s")
    top_dest = (Booking.objects.filter(status="CONFIRMED", flight__isnull=False)
                .values("flight__destination__city").annotate(c=Count("id")).order_by("-c")[:6])
    upcoming = (Flight.objects.filter(departure_time__gt=timezone.now()).select_related("origin", "destination")
                .annotate(total=Count("seats"), booked=Count("seats", filter=Q(seats__status="BOOKED")))
                .filter(booked__gt=0).order_by("-booked", "departure_time")[:8])
    signups = series(User.objects.all(), "date_joined", Count("id"), days)
    entries = series(Donation.objects.filter(status="PAID"), "paid_at", Sum("amount"), days)

    charts = {
        "revenue": {"type": "bar", "stacked": True, "money": True, "labels": labels_for(days), "datasets": rev_by_purpose},
        "booking_status": {"type": "doughnut", "labels": [label for key, label in BOOKING_STATUS_ORDER if key in status_counts],
                           "datasets": [{"label": "Bookings",
                                         "data": [status_counts[key] for key, _ in BOOKING_STATUS_ORDER if key in status_counts],
                                         "colors": [BOOKING_STATUS_COLORS[key] for key, _ in BOOKING_STATUS_ORDER
                                                    if key in status_counts]}]},
        "providers": {"type": "doughnut", "money": True, "labels": [r["provider"].title() for r in provider_rows],
                      "datasets": [{"label": "Collected", "data": [float(r["s"]) for r in provider_rows]}]},
        "destinations": {"type": "bar", "horizontal": True, "labels": [r["flight__destination__city"] for r in top_dest],
                         "datasets": [{"label": "Confirmed bookings", "data": [r["c"] for r in top_dest]}]},
        "load": {"type": "bar", "suffix": "%", "labels": [f"{f.flight_number} {f.departure_time:%d/%m}" for f in upcoming],
                 "datasets": [{"label": "Load factor", "data": [pct(f.booked, f.total) for f in upcoming], "color": "#3E8E6C"}]},
        "signups": {"type": "line", "labels": labels_for(days), "datasets": [{"label": "New customers", "data": signups}]},
        "entries": {"type": "bar", "money": True, "labels": labels_for(days), "datasets": [{"label": "Crowdfunding gifts", "data": entries, "color": "#C9A227"}]},
    }
    recent_bookings = Booking.objects.select_related("user", "flight__origin", "flight__destination", "package")[:8]
    recent_payments = PaymentTransaction.objects.select_related("user")[:8]
    return render(request, "console/dashboard.html", {
        "nav": "dashboard", "kpis": kpis, "charts": charts, "period": period,
        "recent_bookings": recent_bookings, "recent_payments": recent_payments,
        "live_campaigns": CrowdfundingCampaign.objects.live()[:4], "live_giveaways": Giveaway.objects.live()[:4],
        "is_empty": not (Flight.objects.exists() or TravelPackage.objects.exists() or EqubGroup.objects.exists()
                         or CrowdfundingCampaign.objects.exists() or Giveaway.objects.exists()), "collecting_rounds": EqubRound.objects.filter(status="COLLECTING").select_related("group")[:5],
        "equb": _equb_snapshot(), "attention": _attention(),
    })


def _attention() -> dict:
    """Today's work queues across travel and Equb – the strip at the top of the dashboard."""
    from apps.equb.models import EqubMembership as M
    from apps.travel.services import operations as travel_ops

    departures = list(travel_ops.desk_departures(24))
    return {
        "departures": len([f for f in departures if f.tickets]),
        "passengers": sum(f.tickets for f in departures), "checked_in": sum(f.checked for f in departures),
        "holds": Booking.objects.filter(status="PENDING_PAYMENT", expires_at__gt=timezone.now()).count(),
        "refunds": travel_ops.refunds_due().count() + travel_ops.paid_after_expiry().count(),
        "exits": M.objects.filter(exit_requested_at__isnull=False, status__in=M.LIVE_STATUSES).count(),
        "terminations": M.objects.filter(termination_requested_at__isnull=False, status__in=M.LIVE_STATUSES).count(),
    }


def _with_standing(memberships):
    from apps.equb.standing import standings_for

    memberships = list(memberships)
    standings = standings_for(memberships)
    for m in memberships:
        m.standing = standings[m.id]
    return memberships


def _equb_snapshot() -> dict:
    from apps.equb import reports as equb_reports

    members, standings = equb_reports.live_standings()
    return equb_reports.receivables_summary(members, standings)


# --------------------------------------------------------------------------- flights & packages
@staff_required
def flights(request):
    qs = (Flight.objects.select_related("origin", "destination", "aircraft")
          .annotate(total=Count("seats", distinct=True), booked=Count("seats", filter=Q(seats__status="BOOKED"), distinct=True),
                    held=Count("seats", filter=Q(seats__status="LOCKED"), distinct=True)))
    dest, when = request.GET.get("destination", ""), request.GET.get("when", "upcoming")
    if dest:
        qs = qs.filter(destination__iata=dest)
    qs = qs.filter(departure_time__gt=timezone.now()) if when == "upcoming" else qs.filter(departure_time__lte=timezone.now())
    qs = qs.order_by("departure_time" if when == "upcoming" else "-departure_time")
    pg = page(request, qs)
    for f in pg:
        f.load = pct(f.booked, f.total)
    dests = Flight.objects.values_list("destination__iata", "destination__city").distinct().order_by("destination__city")
    route_rev = (Booking.objects.filter(status="CONFIRMED", flight__isnull=False)
                 .values("flight__destination__iata").annotate(s=Sum("total_amount")).order_by("-s")[:8])
    charts = {"routes": {"type": "bar", "money": True, "labels": [r["flight__destination__iata"] for r in route_rev],
                         "datasets": [{"label": "Revenue", "data": [float(r["s"]) for r in route_rev]}]}}
    return render(request, "console/flights.html", {"nav": "flights", "page": pg, "dests": dests, "dest": dest, "when": when, "charts": charts})


@staff_required
def flight_detail(request, pk):
    f = get_object_or_404(Flight.objects.select_related("origin", "destination", "aircraft"), pk=pk)
    seats = list(f.seats.order_by("row", "letter"))
    cabins = []
    for block in f.aircraft.layout:
        groups = block["letters"].split("|")
        rows = []
        by_rl = {(s.row, s.letter): s for s in seats if s.cabin == block["cabin"]}
        for r in range(block["rows"][0], block["rows"][1] + 1):
            cells = []
            for gi, g in enumerate(groups):
                if gi:
                    cells.append(None)
                cells += [by_rl.get((r, letter)) for letter in g]
            rows.append({"row": r, "cells": cells})
        cabins.append({"cabin": block["cabin"], "rows": rows})
    fares = list(f.fares.all())
    for fc in fares:
        fc.now_price = current_fare(fc)
        fc.load = pct(fc.seats_sold, fc.seats_allocated)
    counts = {s: sum(1 for x in seats if x.status == s) for s in ("AVAILABLE", "BOOKED", "LOCKED", "BLOCKED")}
    by_pos = {}
    for s in seats:
        if s.status == "BOOKED":
            by_pos[s.get_position_display()] = by_pos.get(s.get_position_display(), 0) + 1
    bookings_qs = f.bookings.select_related("user", "fare_class").annotate(pax=Count("passengers")).order_by("-created_at")[:30]
    revenue = f.bookings.filter(status="CONFIRMED").aggregate(s=Sum("total_amount"))["s"] or 0
    charts = {
        "seats": {"type": "doughnut", "labels": ["Available", "Booked", "Held", "Blocked"],
                  "datasets": [{"label": "Seats", "data": list(counts.values()), "colors": ["#B8D8C8", "#0F382B", "#C9A227", "#5B6F66"]}]},
        "fares": {"type": "bar", "stacked": True, "labels": [fc.name for fc in fares], "datasets": [
            {"label": "Sold", "data": [fc.seats_sold for fc in fares]},
            {"label": "Remaining", "data": [fc.seats_left for fc in fares], "color": "#B8D8C8"}]},
        "positions": {"type": "doughnut", "labels": list(by_pos), "datasets": [{"label": "Booked seats", "data": list(by_pos.values())}]},
    }
    return render(request, "console/flight_detail.html", {
        "nav": "flights", "f": f, "cabins": cabins, "fares": fares, "counts": counts, "charts": charts,
        "bookings": bookings_qs, "revenue": revenue, "load": pct(counts["BOOKED"], len(seats)),
    })


@staff_required
def packages(request):
    pkgs = list(TravelPackage.objects.annotate(
        revenue=Sum("bookings__total_amount", filter=Q(bookings__status="CONFIRMED")),
        confirmed=Count("bookings", filter=Q(bookings__status="CONFIRMED"))).order_by("departure_date"))
    for p in pkgs:
        p.load = pct(p.seats_sold, p.capacity)
    charts = {"occupancy": {"type": "bar", "stacked": True, "labels": [p.name[:28] for p in pkgs], "datasets": [
        {"label": "Places sold", "data": [p.seats_sold for p in pkgs]},
        {"label": "Places left", "data": [p.seats_left for p in pkgs], "color": "#B8D8C8"}]},
        "revenue": {"type": "doughnut", "money": True, "labels": [p.get_category_display() for p in pkgs],
                    "datasets": [{"label": "Revenue", "data": [float(p.revenue or 0) for p in pkgs]}]}}
    return render(request, "console/packages.html", {"nav": "packages", "pkgs": pkgs, "charts": charts})


@staff_required
def payments(request):
    qs = PaymentTransaction.objects.select_related("user")
    status, purpose, provider, q = (request.GET.get(k, "") for k in ("status", "purpose", "provider", "q"))
    if status:
        qs = qs.filter(status=status)
    if purpose:
        qs = qs.filter(purpose=purpose)
    if provider:
        qs = qs.filter(provider=provider)
    if q:
        qs = qs.filter(Q(user__phone__icontains=q) | Q(provider_reference__icontains=q) | Q(user__full_name__icontains=q))
    days = days_back(30)
    by_status = dict(PaymentTransaction.objects.values_list("status").annotate(c=Count("id")))
    charts = {
        "volume": {"type": "line", "money": True, "labels": labels_for(days), "datasets": [
            {"label": "Successful", "data": series(PaymentTransaction.objects.filter(status=PAID), "completed_at", Sum("amount"), days)},
            {"label": "Failed", "data": series(PaymentTransaction.objects.filter(status="FAILED"), "created_at", Sum("amount"), days), "color": "#C0392B", "fill": False}]},
        "status": {"type": "doughnut", "labels": [s.title() for s in by_status], "datasets": [{"label": "Transactions", "data": list(by_status.values())}]},
    }
    agg = qs.aggregate(n=Count("id"), s=Sum("amount", filter=Q(status=PAID)))
    return render(request, "console/payments.html", {
        "nav": "payments", "page": page(request, qs), "charts": charts, "agg": agg, "status": status, "purpose": purpose,
        "provider": provider, "q": q, "statuses": PaymentTransaction.Status.choices, "purposes": PaymentTransaction.Purpose.choices,
        "providers": PaymentTransaction.Provider.choices,
    })


# --------------------------------------------------------------------------- customers
@staff_required
def users(request):
    qs = User.objects.annotate(
        n_bookings=Count("bookings", filter=Q(bookings__status="CONFIRMED"), distinct=True),
        n_equbs=Count("equb_memberships", distinct=True),
        spent=Sum("payments__amount", filter=Q(payments__status=PAID)),
    ).order_by("-date_joined")
    q, role = request.GET.get("q", "").strip(), request.GET.get("role", "")
    if q:
        qs = qs.filter(Q(phone__icontains=q) | Q(full_name__icontains=q) | Q(email__icontains=q) | Q(referral_code__iexact=q))
    if role == "staff":
        qs = qs.filter(is_staff=True)
    elif role == "customers":
        qs = qs.filter(is_staff=False)
    return render(request, "console/users.html", {"nav": "users", "page": page(request, qs), "q": q, "role": role,
                                                   "total": User.objects.count()})


@staff_required
def user_edit(request, pk):
    u = get_object_or_404(User, pk=pk, is_staff=False)
    form = f.CustomerForm(request.POST or None, instance=u)
    if request.method == "POST" and form.is_valid():
        form.save()
        msg = f"{u.full_name}'s profile was saved."
        if form.cleaned_data.get("new_password"):
            msg += f" They can now sign in with {u.phone} and the new password."
        messages.success(request, msg)
        nxt = request.POST.get("next") or request.GET.get("next")
        return redirect(nxt if nxt and nxt.startswith("/console/") else reverse("console:user", args=[u.pk]))
    from .manage_views import _render
    return _render(request, nav="users", title=f"Edit {u.full_name}", crumbs="Customers", form=form,
                   cancel=request.GET.get("next") or reverse("console:user", args=[u.pk]), obj=u,
                   intro="" if u.has_usable_password() else
                   "This customer was registered at the desk and has no password yet – set one below so they can sign in.")


@staff_required
def user_detail(request, pk):
    u = get_object_or_404(User, pk=pk)
    spent = dict(PaymentTransaction.objects.filter(user=u, status=PAID).values_list("purpose").annotate(s=Sum("amount")))
    charts = {"spend": {"type": "doughnut", "money": True, "labels": [PURPOSE_LABELS[k].split(" (")[0] for k in spent],
                        "datasets": [{"label": "Paid", "data": [float(v) for v in spent.values()]}]}}
    return render(request, "console/user_detail.html", {
        "nav": "users", "u": u, "charts": charts, "total_spent": sum(spent.values(), Decimal("0")),
        "bookings": u.bookings.select_related("flight__origin", "flight__destination", "package")[:20],
        "memberships": _with_standing(u.equb_memberships.select_related("group", "won_round")),
        "entries": u.giveaway_progress.filter(referrals__gt=0).select_related("giveaway"),
        "donations": Donation.objects.filter(user=u, status="PAID").select_related("campaign")[:10],
        "wins": GiveawayWinner.objects.filter(user=u).select_related("giveaway"),
        "payments": PaymentTransaction.objects.filter(user=u)[:15],
        "referrals": u.referrals.all()[:10],
    })
