"""
Staff console – Equb operations: pools, rounds & draws, member places, collections (receivables),
call centre, terminations & exit requests, reports, payouts and the Equb rules.
"""
from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, F, Max, Min, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from apps.accounts.models import User
from apps.core.exceptions import BusinessRuleError
from apps.equb import operations as ops
from apps.equb import reports, services
from apps.equb import standing as st
from apps.equb import teams
from apps.equb.models import (CallLog, Contribution, EqubDraw, GroupEqub, EqubGroup, EqubMembership, EqubPackage, EqubPayment, EqubPayout, EqubRound,
                              EqubSettings, Termination)
from apps.equb.serializers import rules_summary
from apps.equb.views import _payment_history

from . import forms as f
from .views import days_back, labels_for, pct, series, staff_required

PER_PAGE = 25
STATUS_ORDER = {"ACTIVE": 0, "OPEN": 1, "DRAFT": 2, "COMPLETED": 3, "CANCELLED": 4}


# --------------------------------------------------------------------------- helpers
def _date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _next(request, default: str) -> str:
    nxt = request.POST.get("next") or request.GET.get("next") or ""
    return nxt if nxt.startswith("/console/") else default


def _do(request, fn, ok: str | None = None):
    """Run a service call, turning business-rule errors into a message instead of a 409 page."""
    try:
        result = fn()
        if ok:
            messages.success(request, ok)
        return result
    except BusinessRuleError as exc:
        messages.error(request, str(exc.detail))
    return None


def _csv(filename: str, header: list[str], rows) -> HttpResponse:
    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    resp.write("﻿")  # Excel opens UTF-8 (Amharic names) correctly with a BOM
    w = csv.writer(resp)
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return resp


def _pager(request, items, per_page=PER_PAGE):
    return Paginator(items, per_page).get_page(request.GET.get("page"))


def _pools():
    return EqubGroup.objects.exclude(status=EqubGroup.Status.DRAFT).order_by("name").only("id", "name")


# --------------------------------------------------------------------------- pools
@staff_required
def equbs(request):
    status, package = request.GET.get("status", ""), request.GET.get("package", "")
    q = request.GET.get("q", "").strip()
    qs = EqubGroup.objects.select_related("equb_package").annotate(
        members=Count("memberships", filter=~Q(memberships__status=EqubMembership.Status.LEFT)))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(join_code__iexact=q) | Q(equb_package__name__icontains=q))
    if status:
        qs = qs.filter(status=status)
    if package:
        qs = qs.filter(equb_package__slug=package)
    groups = sorted(qs, key=lambda g: (STATUS_ORDER.get(g.status, 9), g.start_date, g.name))
    collected = dict(Contribution.objects.filter(status__in=Contribution.SETTLED)
                     .values_list("round__group").annotate(s=Sum("amount")))
    members, standings = reports.live_standings()
    behind: dict[int, list] = defaultdict(lambda: [0, Decimal("0")])
    for m in members:
        s = standings[m.id]
        if s.arrears > 0:
            behind[m.group_id][0] += 1
            behind[m.group_id][1] += s.arrears
    for g in groups:
        g.fill = pct(g.members, g.max_members)
        g.collected = collected.get(g.id) or 0
        g.behind, g.arrears = behind[g.id] if g.id in behind else (0, 0)
    summary = reports.receivables_summary(members, standings)
    payouts = EqubPayout.objects.aggregate(n=Count("id"), s=Sum("amount"), fee=Sum("fee_amount"))
    by_dest = defaultdict(int)
    for g in groups:
        by_dest[g.equb_package.name if g.equb_package_id else "No package"] += g.members
    days = days_back(30)
    charts = {
        "members": {"type": "doughnut", "labels": list(by_dest), "datasets": [{"label": "Members", "data": list(by_dest.values())}]},
        "savings": {"type": "bar", "money": True, "labels": labels_for(days), "datasets": [
            {"label": "Contributions received", "data": series(Contribution.objects.filter(status__in=Contribution.SETTLED),
                                                               "paid_at", Sum("amount"), days)}]},
        "standing": {"type": "doughnut", "labels": [r["label"] for r in summary["by_status"]],
                     "datasets": [{"label": "Members", "data": [r["count"] for r in summary["by_status"]],
                                   "colors": [r["color"] for r in summary["by_status"]]}]},
    }
    counts = dict(EqubGroup.objects.values_list("status").annotate(n=Count("id")))
    return render(request, "console/equbs.html", {
        "nav": "equb", "groups": groups, "charts": charts, "summary": summary, "status": status, "counts": counts, "q": q,
        "package": package, "packages": EqubPackage.objects.order_by("name"),
        "statuses": EqubGroup.Status.choices, "payouts": payouts, "liability": reports.payout_liability(),
        "saved": sum(collected.values(), Decimal("0")),
    })


@staff_required
def equb_detail(request, slug):
    g = get_object_or_404(EqubGroup.objects.select_related("equb_package", "organizer"), slug=slug)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "start":
            _do(request, lambda: services.start_group(g), "Equb started — rounds scheduled and members notified.")
        elif action == "sweep":
            n = services.sweep_late_contributions()
            ops.escalate_standings()
            messages.info(request, f"{n} contribution(s) marked late; members who fell behind were notified.")
        elif action == "new_code":
            g.join_code = EqubGroup.new_join_code()
            g.save(update_fields=["join_code", "updated_at"])
            messages.success(request, f"New join code: {g.join_code}. The old code no longer works.")
        elif action == "cancel_pool":
            if g.status in (EqubGroup.Status.OPEN, EqubGroup.Status.DRAFT):
                g.status = EqubGroup.Status.CANCELLED
                g.save(update_fields=["status", "updated_at"])
                messages.success(request, "Pool cancelled. Members can no longer join.")
            else:
                messages.error(request, "Only pools that haven't started can be cancelled here.")
        return redirect(_next(request, reverse("console:equb", args=[slug])))

    tab = request.GET.get("tab", "overview")
    rounds = list(g.rounds.select_related("draw").prefetch_related("payouts__membership__user")
                  .annotate(paid=Count("contributions", filter=Q(contributions__status__in=Contribution.SETTLED)),
                            slips=Count("contributions")).order_by("number"))
    for r in rounds:
        r.progress = pct(r.collected_amount, r.expected_amount)
        r.drawn = hasattr(r, "draw") and r.draw.is_executed
        r.eligible = len(r.draw.eligible_memberships) if r.drawn else None
    current = next((r for r in rounds if r.status == EqubRound.Status.COLLECTING), None)
    upcoming = [r for r in rounds if r.status == EqubRound.Status.UPCOMING]
    timeline = [r for r in rounds if r.status != EqubRound.Status.UPCOMING] + upcoming[:3]
    members = list(g.memberships.exclude(status=EqubMembership.Status.LEFT).select_related("user", "won_round", "group")
                   .order_by("position"))
    standings = st.standings_for(members)
    behind = [m for m in members if standings[m.id].arrears > 0]
    ctx = {"nav": "equb", "g": g, "tab": tab, "rounds": rounds, "current": current, "members_count": len(members),
           "timeline": timeline, "more_upcoming": max(len(upcoming) - 3, 0),
           "behind_count": len(behind), "totals": services.group_totals(g), "fill": pct(g.members_count, g.max_members),
           "rules": rules_summary(g, EqubSettings.load()),
           "arrears": sum((standings[m.id].arrears for m in members), Decimal("0")),
           "credit": sum((standings[m.id].credit for m in members), Decimal("0"))}
    if tab == "members":
        filt = request.GET.get("standing", "")
        rows = [{"m": m, "s": standings[m.id]} for m in members]
        if filt == "behind":
            rows = [r for r in rows if r["s"].arrears > 0]
        elif filt == "won":
            rows = [r for r in rows if r["m"].has_won]
        elif filt == "waiting":
            rows = [r for r in rows if not r["m"].has_won and r["m"].is_live]
        elif filt == "ended":
            rows = [r for r in rows if not r["m"].is_live]
        ctx.update(rows=rows, filt=filt)
    elif tab == "payments":
        ctx["payments"] = _pager(request, EqubPayment.objects.filter(membership__group=g)
                                 .exclude(status=EqubPayment.Status.CANCELLED)
                                 .select_related("membership__user", "recorded_by").order_by("-created_at"))
        ctx["legacy"] = (Contribution.objects.filter(round__group=g, payment__isnull=False, payment_record__isnull=True)
                         .select_related("membership__user", "round", "payment").order_by("-paid_at")[:50])
    else:
        ledger = services.member_ledger(g)
        contrib_status = dict(Contribution.objects.filter(round__group=g).values_list("status").annotate(c=Count("id")))
        ctx.update(ledger=ledger, charts={
            "rounds": {"type": "bar", "money": True, "labels": [f"R{r.number}" for r in rounds], "datasets": [
                {"label": "Expected", "data": [float(r.expected_amount) for r in rounds], "color": "#B8D8C8"},
                {"label": "Collected", "data": [float(r.collected_amount) for r in rounds]}]},
            "status": {"type": "doughnut", "labels": [dict(Contribution.Status.choices).get(s, s) for s in contrib_status],
                       "datasets": [{"label": "Contributions", "data": list(contrib_status.values()),
                                     "colors": [{"PAID": "#0F382B", "WAIVED": "#C9A227", "LATE": "#C0392B",
                                                 "PENDING": "#B8D8C8"}.get(s, "#5B6F66") for s in contrib_status]}]},
        })
    return render(request, "console/equb_detail.html", ctx)


# --------------------------------------------------------------------------- rounds & draws
@staff_required
def equb_round(request, slug, number):
    g = get_object_or_404(EqubGroup, slug=slug)
    rnd = get_object_or_404(EqubRound.objects.select_related("draw", "group"), group=g, number=number)
    here = reverse("console:equb_round", args=[slug, number])
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "draw":
            p = request.POST
            if p.get("method") == "select":
                ids = [int(i) for i in p.getlist("winner") if i.isdigit()]
                if not ids:
                    messages.error(request, "Tick the member(s) who win this round.")
                    return redirect(here)
                payouts = _do(request, lambda: services.run_draw(
                    rnd, executed_by=request.user, winner_ids=ids, note=p.get("note", ""),
                    include_ineligible=bool(p.get("include_ineligible"))))
                if payouts:
                    messages.success(request, "Winner chosen: " + ", ".join(x.membership.user.full_name for x in payouts))
            else:
                payouts = _do(request, lambda: services.run_draw(rnd, executed_by=request.user))
                if payouts:
                    messages.success(request, "Winner drawn: " + ", ".join(x.membership.user.full_name for x in payouts))
        elif action == "postpone":
            new_date = _date(request.POST.get("draw_date"))
            if not new_date:
                messages.error(request, "Choose the new draw date.")
            else:
                _do(request, lambda: ops.postpone_draw(rnd, request.user, new_date=new_date,
                                                       reason=request.POST.get("reason", ""),
                                                       notify_members=bool(request.POST.get("notify"))),
                    f"Draw moved to {new_date:%d %b %Y}.")
        return redirect(here)
    contributions = list(rnd.contributions.select_related("membership__user", "payment_record").order_by("membership__position"))
    today = timezone.localdate()
    for c in contributions:
        c.overdue = c.status in Contribution.UNSETTLED and today > rnd.due_date + timedelta(days=g.grace_days)
        c.fee = st.pending_fee(c, g, today)
    ctx = {"nav": "equb_draws", "g": g, "r": rnd, "contributions": contributions,
           "paid": sum(1 for c in contributions if c.is_settled), "progress": pct(rnd.collected_amount, rnd.expected_amount),
           "drawn": hasattr(rnd, "draw") and rnd.draw.is_executed, "today": today}
    if ctx["drawn"]:
        ctx["in_draw"] = len(rnd.draw.eligible_memberships)
        ctx["snapshot"] = rnd.draw.pool_snapshot or {}
        ctx["verification"] = services.verify_draw(rnd)
        ctx["payouts"] = rnd.payouts.select_related("membership__user", "booking")
    elif hasattr(rnd, "draw"):
        ctx["pool"] = services.draw_pool(rnd)
        ctx["in_draw"] = ctx["pool"]["eligible_count"]
        ctx["can_draw"] = rnd.status == EqubRound.Status.COLLECTING and g.current_round == rnd.number
        ctx["selectable"] = services.selectable_members(ctx["pool"], include_ineligible=True)
        ctx["to_pick"] = min(g.winners_per_round, len(ctx["selectable"]))
    return render(request, "console/equb_round.html", ctx)


@staff_required
def equb_plan(request, slug):
    """
    Rounds & draw schedule: every round with its exact due date and draw date (editable until it is drawn),
    collection progress, the money for winners, and the actions for the round being collected now.
    """
    g = get_object_or_404(EqubGroup, slug=slug)
    here = reverse("console:equb_plan", args=[slug])
    rounds = list(g.rounds.select_related("draw").prefetch_related("payouts__membership__user").order_by("number"))
    if request.method == "POST":
        from .templatetags.console_tags import bust_equb_counts

        bust_equb_counts()
        a = request.POST.get("action")
        if a == "save":
            changes, bad = {}, []
            for r in rounds:
                if r.status == EqubRound.Status.DRAWN:
                    continue
                due, draw = _date(request.POST.get(f"due_{r.number}")), _date(request.POST.get(f"draw_{r.number}"))
                if not due or not draw:
                    bad.append(r.number)
                    continue
                changes[r.number] = (due, draw)
            if bad:
                messages.error(request, f"Enter both dates for round{'s' if len(bad) > 1 else ''} {', '.join(map(str, bad))}.")
            else:
                done = _do(request, lambda: ops.plan_rounds(g, request.user, changes,
                                                            shift_following=bool(request.POST.get("shift")),
                                                            notify_members=bool(request.POST.get("notify"))))
                if done is not None:
                    messages.success(request, f"Saved – round{'s' if len(done) != 1 else ''} {', '.join(map(str, done))} "
                                              f"{'have' if len(done) != 1 else 'has'} new dates." if done else "No dates changed.")
        elif a == "reset":
            n = _do(request, lambda: ops.reset_round_dates(g))
            if n is not None:
                messages.success(request, f"{n} round{'s' if n != 1 else ''} put back on the regular schedule.")
        elif a == "skip":
            cur = next((r for r in rounds if r.status == EqubRound.Status.COLLECTING), None)
            if cur:
                _do(request, lambda: services.close_round_without_winner(cur, request.user, request.POST.get("note", "")),
                    f"Round {cur.number} closed without a winner – the money stays for the next draw.")
        return redirect(here)
    today = timezone.localdate()
    current = next((r for r in rounds if r.status == EqubRound.Status.COLLECTING), None)
    paid = dict(Contribution.objects.filter(round__group=g, status__in=Contribution.SETTLED)
                .values_list("round__number").annotate(n=Count("id")))
    slips = dict(Contribution.objects.filter(round__group=g).values_list("round__number").annotate(n=Count("id")))
    draw_numbers = services.draw_round_numbers(g, rounds)
    for r in rounds:
        r.paid_n, r.slips_n = paid.get(r.number, 0), slips.get(r.number, 0)
        r.editable = r.status != EqubRound.Status.DRAWN
        r.has_draw = draw_numbers is None or r.number in draw_numbers
        r.days = (r.draw_date - today).days
        r.winners = [p.membership.user.full_name for p in r.payouts.all()]
    pool = services.draw_pool(current) if current else None
    return render(request, "console/equb_plan.html", {
        "nav": "equb", "g": g, "rounds": rounds, "current": current, "today": today,
        "funds": services.equb_funds(g, current) if g.status == EqubGroup.Status.ACTIVE else None,
        "in_draw": pool["eligible_count"] if pool else 0, "waiting": pool["waiting_count"] if pool else 0,
        "drawn": sum(1 for r in rounds if r.status == EqubRound.Status.DRAWN),
        "draws_total": g.rounds_drawn if draw_numbers is not None else len(rounds),
        "draws_done": sum(1 for r in rounds if r.status == EqubRound.Status.DRAWN
                          and getattr(getattr(r, "draw", None), "executed_at", None))})


# --------------------------------------------------------------------------- one member's place
@staff_required
def equb_member(request, pk):
    m = get_object_or_404(EqubMembership.objects.select_related("group", "user", "won_round", "called_by"), pk=pk)
    here = reverse("console:equb_member", args=[pk])
    if request.method == "POST":
        a = request.POST.get("action")
        p = request.POST
        if a == "record_payment":
            rounds = int(p.get("rounds") or 0) if (p.get("rounds") or "").isdigit() else 0
            amount = _decimal(p.get("amount", "")) if not rounds else None
            rec = _do(request, lambda: services.record_office_payment(
                m, request.user, method=p.get("method", ""), amount=amount, rounds=rounds or None,
                reference=p.get("reference", ""), note=p.get("note", ""), notify_member=bool(p.get("notify"))))
            if rec:
                covered = [r["round"] for r in rec.allocation.get("rounds", [])]
                messages.success(request, f"Receipt {rec.reference}: {services.money(rec.amount)} ETB recorded"
                                          + (f" – paid round{'s' if len(covered) > 1 else ''} {', '.join(map(str, covered))}." if covered
                                             else " – kept as credit for the next round."))
        elif a == "log_call":
            _do(request, lambda: ops.log_call(m, request.user, outcome=p.get("outcome", ""),
                                              promised_date=_date(p.get("promised_date")), feedback=p.get("feedback", "")),
                "Call saved.")
        elif a == "toggle_called":
            ops.set_called(m, request.user, m.called_at is None)
            messages.success(request, "Marked as called." if m.called_at else "Marked as not called.")
        elif a == "block":
            until = _date(p.get("until"))
            if not until:
                messages.error(request, "Choose until when.")
            else:
                _do(request, lambda: ops.set_draw_block(m, request.user, until=until, reason=p.get("reason", "")),
                    f"Held out of draws until {until:%d %b %Y}.")
        elif a == "unblock":
            ops.clear_draw_block(m)
            messages.success(request, "Draw hold removed.")
        elif a == "request_termination":
            _do(request, lambda: ops.request_termination(m, request.user, note=p.get("note", "")), "Sent to the termination list.")
        elif a == "cancel_termination":
            ops.cancel_termination_request(m)
            messages.success(request, "Removed from the termination list.")
        elif a == "terminate":
            _do(request, lambda: ops.terminate(m, request.user, reason=p.get("reason", ""), note=p.get("note", ""),
                                               notify_member=bool(p.get("notify"))), "Place terminated.")
        elif a == "reinstate":
            t = m.terminations.filter(reinstated_at__isnull=True).first()
            if t:
                _do(request, lambda: ops.reinstate(t, request.user), "Place reinstated.")
        elif a == "approve_exit":
            _do(request, lambda: ops.terminate(m, request.user, reason=Termination.Reason.MEMBER_REQUEST,
                                               note=p.get("note", "") or m.exit_request_reason, notify_member=True),
                "Exit approved – the place is closed.")
        elif a == "decline_exit":
            _do(request, lambda: ops.decline_exit_request(m, request.user, note=p.get("note", "")), "Exit request declined.")
        elif a == "waive_fee":
            c = m.contributions.filter(round_id=p.get("round_id") or 0).first()
            if c:
                _do(request, lambda: ops.waive_late_fee(c, request.user, reason=p.get("reason", "")), "Late fee waived.")
        elif a == "notes":
            m.notes = p.get("notes", "")
            m.save(update_fields=["notes", "updated_at"])
            messages.success(request, "Notes saved.")
        elif a == "paid_out":
            payout = EqubPayout.objects.filter(membership=m).first()
            if payout:
                _do(request, lambda: services.mark_payout_paid_out(payout, request.user, method=p.get("method", "Cash"),
                                                                   reference=p.get("reference", "")), "Payout marked as paid.")
        return redirect(_next(request, here))

    rules = EqubSettings.load()
    standing = st.standing_for(m, rules)
    plan = services.payment_plan(m, rules)
    schedule = st.schedule_for(m)
    for row in schedule:  # the API speaks ISO strings; the page wants real dates
        row["due"] = date.fromisoformat(row["due_date"])
        row["paid"] = datetime.fromisoformat(row["paid_at"]) if row["paid_at"] else None
    payments = _payment_history(m)
    for p_ in payments:
        when = p_.get("paid_at") or p_.get("created_at")
        p_["when"] = datetime.fromisoformat(when) if isinstance(when, str) else when
    return render(request, "console/equb_member.html", {
        "nav": "equb", "m": m, "g": m.group, "s": standing, "schedule": schedule, "plan": plan,
        "payments": payments, "calls": m.call_logs.select_related("called_by")[:30],
        "terminations": m.terminations.select_related("terminated_by", "reinstated_by"), "exit": ops.exit_info(m, standing),
        "payout": EqubPayout.objects.filter(membership=m).select_related("round", "booking").first(),
        "outcomes": CallLog.Outcome.choices, "reasons": Termination.Reason.choices, "rules": rules,
        "methods": [(k, v) for k, v in EqubPayment.Method.choices if k in EqubPayment.OFFICE_METHODS],
        "today": timezone.localdate(), "active_termination": m.terminations.filter(reinstated_at__isnull=True).first(),
    })


# --------------------------------------------------------------------------- collections (receivables)
@staff_required
def equb_collections(request):
    pool = request.GET.get("pool", "")
    q = request.GET.get("q", "").strip()
    band = request.GET.get("band", "")
    never = bool(request.GET.get("never"))
    members, standings = reports.live_standings(group_id=int(pool) if pool.isdigit() else None, search=q)
    rows = reports.debtors(members, standings, band=band, only_never_paid=never)
    if request.GET.get("export") == "csv":
        return _csv(f"vacation-equb-arrears-{timezone.localdate()}.csv",
                    ["Member", "Phone", "Equb", "Place", "Standing", "Missed rounds", "Days overdue", "Arrears (ETB)",
                     "Paid (ETB)", "Credit (ETB)", "Won", "Last called"],
                    [[r["m"].user.full_name, r["m"].user.phone, r["m"].group.name, r["m"].position, r["s"].status_label,
                      r["s"].missed_rounds, r["s"].days_overdue, r["s"].arrears, r["s"].paid_amount, r["s"].credit,
                      "yes" if r["s"].has_won else "no",
                      timezone.localtime(r["m"].called_at).strftime("%Y-%m-%d %H:%M") if r["m"].called_at else ""]
                     for r in rows])
    summary = reports.receivables_summary(members, standings)
    ageing = reports.ageing(members, standings)
    charts = {
        "ageing": {"type": "bar", "money": True, "labels": [b["label"] for b in ageing],
                   "datasets": [{"label": "Arrears", "data": [float(b["amount"]) for b in ageing],
                                 "colors": ["#C9A227", "#D98E04", "#C0392B", "#8E2A20"]}]},
        "standing": {"type": "doughnut", "labels": [r["label"] for r in summary["by_status"]],
                     "datasets": [{"label": "Members", "data": [r["count"] for r in summary["by_status"]],
                                   "colors": [r["color"] for r in summary["by_status"]]}]},
    }
    return render(request, "console/equb_collections.html", {
        "nav": "collections", "summary": summary, "ageing": ageing, "pools": reports.by_pool(members, standings),
        "page": _pager(request, rows), "charts": charts, "pool": pool, "q": q, "band": band, "never": never,
        "pool_choices": _pools(), "refund": reports.credit_to_refund(), "terminated": reports.terminated_debt(),
        "liability": reports.payout_liability(),
    })


# --------------------------------------------------------------------------- call centre
CALL_TABS = [("behind", "All behind"), ("d7_14", "7–14 days"), ("d15_30", "15–30 days"), ("d31_60", "31–60 days"),
             ("d60", "Over 60 days"), ("never", "Never paid"), ("promised", "Promised"), ("broken", "Broken promises"),
             ("uncalled", "Uncalled"), ("called", "Called")]


def _call_tab(key, r, today):
    s, last = r["s"], r["last"]
    return {
        "behind": True, "d7_14": 7 <= s.days_overdue <= 14, "d15_30": 15 <= s.days_overdue <= 30,
        "d31_60": 31 <= s.days_overdue <= 60, "d60": s.days_overdue > 60, "never": s.status == st.NEVER_PAID,
        "promised": bool(last and last.outcome == CallLog.Outcome.PROMISED and last.promised_date and last.promised_date >= today),
        "broken": bool(last and last.outcome == CallLog.Outcome.PROMISED and last.promised_date and last.promised_date < today),
        "uncalled": r["m"].called_at is None, "called": r["m"].called_at is not None,
    }[key]


@staff_required
def call_center(request):
    if request.method == "POST":
        from .templatetags.console_tags import bust_equb_counts

        bust_equb_counts()
        a = request.POST.get("action")
        ids = [int(i) for i in request.POST.getlist("ids") if i.isdigit()]
        mid = request.POST.get("membership", "")
        single = EqubMembership.objects.select_related("user").filter(pk=mid).first() if mid.isdigit() else None
        targets = list(EqubMembership.objects.select_related("user").filter(pk__in=ids))
        if a in ("toggle_called", "log_call"):
            targets = [single] if single else []
        if a in ("mark_called", "mark_uncalled"):
            for m in targets:
                ops.set_called(m, request.user, a == "mark_called")
            messages.success(request, f"{len(targets)} member(s) marked {'called' if a == 'mark_called' else 'not called'}.")
        elif a == "toggle_called" and targets:
            m = targets[0]
            ops.set_called(m, request.user, m.called_at is None)
            messages.success(request, f"{m.user.full_name}: {'called' if m.called_at else 'not called'}.")
        elif a == "send_termination":
            n = 0
            for m in targets:
                if _do(request, lambda m=m: ops.request_termination(m, request.user, note="Sent from the call centre")):
                    n += 1
            messages.success(request, f"{n} member(s) sent to the termination list.")
        elif a == "log_call" and targets:
            m = targets[0]
            _do(request, lambda: ops.log_call(m, request.user, outcome=request.POST.get("outcome", ""),
                                              promised_date=_date(request.POST.get("promised_date")),
                                              feedback=request.POST.get("feedback", "")),
                f"Call with {m.user.full_name} saved and marked as called.")
        return redirect(_next(request, reverse("console:call_center")))

    rules = EqubSettings.load()
    today = timezone.localdate()
    pool = request.GET.get("pool", "")
    q = request.GET.get("q", "").strip()
    tab = request.GET.get("tab", "behind")
    members, standings = reports.live_standings(group_id=int(pool) if pool.isdigit() else None, search=q, rules=rules)
    base = [m for m in members if standings[m.id].arrears > 0 and standings[m.id].days_overdue >= rules.call_after_days]
    latest = ops.latest_calls([m.id for m in base])
    rows = [{"m": m, "s": standings[m.id], "last": latest.get(m.id)} for m in base]
    rows.sort(key=lambda r: (-r["s"].critical, -r["s"].days_overdue, -r["s"].arrears))
    counts = {k: sum(1 for r in rows if _call_tab(k, r, today)) for k, _ in CALL_TABS}
    shown = [r for r in rows if _call_tab(tab, r, today)] if tab in counts else rows
    return render(request, "console/equb_call_center.html", {
        "nav": "call_center", "tabs": CALL_TABS, "tab": tab, "counts": counts, "page": _pager(request, shown),
        "pool": pool, "q": q, "pool_choices": _pools(), "outcomes": CallLog.Outcome.choices, "rules": rules, "today": today,
        "owed_total": sum((r["s"].arrears for r in shown), Decimal("0")),
    })


# --------------------------------------------------------------------------- terminations & exit requests
TERM_TABS = [("requested", "Termination list"), ("exits", "Exit requests"), ("candidates", "Overdue candidates"),
             ("terminated", "Terminated"), ("rejoin", "Rejoin requests")]


@staff_required
def terminations(request):
    if request.method == "POST":
        from .templatetags.console_tags import bust_equb_counts

        bust_equb_counts()
        a, p = request.POST.get("action"), request.POST
        m = EqubMembership.objects.filter(pk=p.get("membership") or 0).select_related("user").first()
        if a == "terminate" and m:
            _do(request, lambda: ops.terminate(m, request.user, reason=p.get("reason", ""), note=p.get("note", ""),
                                               notify_member=bool(p.get("notify"))), f"{m.user.full_name}'s place terminated.")
        elif a == "keep" and m:
            ops.cancel_termination_request(m)
            messages.success(request, f"{m.user.full_name} kept – removed from the termination list.")
        elif a == "flag" and m:
            _do(request, lambda: ops.request_termination(m, request.user, note=p.get("note", "")),
                f"{m.user.full_name} added to the termination list.")
        elif a == "approve_exit" and m:
            _do(request, lambda: ops.terminate(m, request.user, reason=Termination.Reason.MEMBER_REQUEST,
                                               note=p.get("note", "") or m.exit_request_reason, notify_member=True),
                f"Exit approved for {m.user.full_name}.")
        elif a == "decline_exit" and m:
            _do(request, lambda: ops.decline_exit_request(m, request.user, note=p.get("note", "")),
                f"Exit request from {m.user.full_name} declined.")
        elif a in ("approve_rejoin", "decline_rejoin"):
            from apps.equb.models import JoinRequest

            req = JoinRequest.objects.filter(pk=p.get("join_request") or 0).select_related("user").first()
            if req:
                approve = a == "approve_rejoin"
                _do(request, lambda: ops.decide_rejoin(req, request.user, approve=approve, note=p.get("note", "")),
                    f"{req.user.full_name} {'is back in the Equb' if approve else 'was told the request was declined'}.")
        elif a == "reinstate":
            t = Termination.objects.filter(pk=p.get("termination") or 0).first()
            if t:
                _do(request, lambda: ops.reinstate(t, request.user), "Place reinstated.")
        return redirect(_next(request, reverse("console:terminations")))

    rules = EqubSettings.load()
    tab = request.GET.get("tab", "requested")
    live = st.live_memberships()
    requested = list(live.filter(termination_requested_at__isnull=False).order_by("termination_requested_at"))
    exits = list(live.filter(exit_requested_at__isnull=False).order_by("exit_requested_at"))
    members, standings = reports.live_standings(rules=rules)
    candidates = [m for m in members if standings[m.id].days_overdue >= rules.terminate_after_days
                  and not m.termination_requested_at]
    done = Termination.objects.select_related("membership__user", "membership__group", "terminated_by", "reinstated_by")
    from apps.equb.models import JoinRequest

    rejoin = JoinRequest.objects.filter(status=JoinRequest.Status.PENDING).select_related("user", "group", "membership")
    counts = {"requested": len(requested), "exits": len(exits), "candidates": len(candidates), "terminated": done.count(),
              "rejoin": rejoin.count()}
    if tab == "terminated":
        rows = _pager(request, done)
    elif tab == "rejoin":
        rows = _pager(request, rejoin.order_by("created_at"))
    else:
        source = {"requested": requested, "exits": exits, "candidates": candidates}.get(tab, requested)
        sts = standings if tab == "candidates" else st.standings_for(source, rules)
        rows = _pager(request, sorted(({"m": m, "s": sts[m.id]} for m in source),
                                      key=lambda r: -r["s"].days_overdue))
    return render(request, "console/equb_terminations.html", {
        "nav": "terminations", "tabs": TERM_TABS, "tab": tab, "counts": counts, "page": rows, "rules": rules,
        "reasons": Termination.Reason.choices, "debt": reports.terminated_debt(),
    })


# --------------------------------------------------------------------------- Equb draws (NiyaEqubBank style)
def _due_rounds():
    return (EqubRound.objects.filter(status=EqubRound.Status.COLLECTING, group__status=EqubGroup.Status.ACTIVE,
                                     group__current_round=F("number"))
            .select_related("group", "group__equb_package", "draw")
            .annotate(paid=Count("contributions", filter=Q(contributions__status__in=Contribution.SETTLED)),
                      slips=Count("contributions"))
            .order_by("draw_date", "group__name"))


@staff_required
def equb_draws(request):
    """Every round: what is due for a draw now, every draw ever run with its winners, and the winners' payouts."""
    if request.method == "POST" and request.POST.get("action") == "draw_now":
        from .templatetags.console_tags import bust_equb_counts

        bust_equb_counts()
        # "Draw now": the system picks the winner at random (provably fair) – for the whole Equb, or only among the
        # members of one Group Equb in it.
        rnd = EqubRound.objects.select_related("group").filter(pk=request.POST.get("round") or 0).first()
        team = GroupEqub.objects.filter(pk=request.POST.get("team") or 0).first() if request.POST.get("team") else None
        if not rnd:
            messages.error(request, "Choose the Equb to draw.")
        elif team and team.equb_id != rnd.group_id:
            messages.error(request, f"{team.name} is not part of {rnd.group.name}.")
        else:
            payouts = _do(request, lambda: services.run_draw(rnd, executed_by=request.user, team=team))
            if payouts:
                messages.success(request, f"🏆 {rnd.group.name} round {rnd.number}: "
                                          + ", ".join(f"{p.membership.display_name} wins {services.money(p.amount)} ETB" for p in payouts)
                                          + (f" (drawn within {team.name})" if team else ""))
                return redirect(reverse("console:equb_round", args=[rnd.group.slug, rnd.number]))
        return redirect(_next(request, reverse("console:equb_draws")))
    if request.method == "POST":
        payout = EqubPayout.objects.filter(pk=request.POST.get("payout") or 0).first()
        if payout:
            _do(request, lambda: services.mark_payout_paid_out(payout, request.user, method=request.POST.get("method", "Cash"),
                                                               reference=request.POST.get("reference", "")),
                "Payout marked as paid.")
        return redirect(_next(request, reverse("console:equb_draws") + "?tab=payouts"))

    today = timezone.localdate()
    tab = request.GET.get("tab", "due")
    q = request.GET.get("q", "").strip()
    pool = request.GET.get("pool", "")
    executed = EqubDraw.objects.filter(executed_at__isnull=False)
    due_all = _due_rounds()
    kpi = {
        "rounds": executed.count(),
        "rounds_week": executed.filter(executed_at__gte=timezone.now() - timedelta(days=7)).count(),
        "running": EqubGroup.objects.filter(status=EqubGroup.Status.ACTIVE).count(),
        "won": EqubPayout.objects.aggregate(s=Sum("amount"))["s"] or 0,
        "winners": EqubPayout.objects.count(),
        "waiting": EqubMembership.objects.filter(group__status=EqubGroup.Status.ACTIVE, status=EqubMembership.Status.ACTIVE,
                                                 has_won=False).count(),
        "due_now": due_all.filter(draw_date__lte=today).count(),
        "due_manual": due_all.filter(draw_date__lte=today, group__draw_type=EqubGroup.DrawType.MANUAL).count(),
        "random": executed.filter(mode__in=["RANDOM", "WEIGHTED"]).count(),
        "coming": due_all.count(),
    }
    kpi["verifiable"] = round(100 * kpi["random"] / kpi["rounds"]) if kpi["rounds"] else 100
    first_due = due_all.filter(draw_date__lte=today).first() or due_all.first()
    drawable = list(due_all)
    ctx = {"nav": "equb_draws", "tab": tab, "kpi": kpi, "q": q, "pool": pool, "pool_choices": _pools(), "today": today,
           "first_due": first_due, "liability": reports.payout_liability(), "drawable": drawable,
           "draw_teams": GroupEqub.objects.filter(is_active=True, equb_id__in=[r.group_id for r in drawable]).order_by("name")}

    if tab == "history":
        mode = request.GET.get("mode", "")
        qs = (executed.select_related("round__group__equb_package", "executed_by")
              .prefetch_related("round__payouts__membership__user").order_by("-executed_at"))
        if pool.isdigit():
            qs = qs.filter(round__group_id=int(pool))
        if mode == "RANDOM":
            qs = qs.filter(mode__in=["RANDOM", "WEIGHTED"])
        elif mode == "SELECTED":
            qs = qs.filter(mode=mode)
        if q:
            qs = qs.filter(Q(round__group__name__icontains=q) | Q(round__payouts__membership__user__full_name__icontains=q)
                           | Q(round__payouts__membership__user__phone__icontains=q)
                           | Q(round__payouts__voucher_code__iexact=q)).distinct()
        if request.GET.get("export") == "csv":
            rows = []
            for d in qs:
                for p in d.round.payouts.all():
                    rows.append([timezone.localtime(d.executed_at).strftime("%Y-%m-%d %H:%M"), d.round.group.name,
                                 d.round.number, p.membership.user.full_name, p.membership.user.phone, p.amount,
                                 p.get_status_display(), d.get_mode_display(), len(d.eligible_memberships), p.voucher_code])
            return _csv(f"vacation-equb-draws-{today}.csv", ["Drawn", "Equb", "Round", "Winner", "Phone", "Won (ETB)",
                                                          "Payout", "Mode", "Entries", "Voucher"], rows)
        ctx.update(page=_pager(request, qs), mode=mode)
    elif tab == "payouts":
        status = request.GET.get("status", "")
        qs = EqubPayout.objects.select_related("membership__user", "round__group", "booking", "paid_out_by").order_by("-created_at")
        if status:
            qs = qs.filter(status=status)
        if pool.isdigit():
            qs = qs.filter(round__group_id=int(pool))
        if q:
            qs = qs.filter(Q(membership__user__full_name__icontains=q) | Q(membership__user__phone__icontains=q)
                           | Q(voucher_code__iexact=q))
        agg = EqubPayout.objects.aggregate(n=Count("id"), s=Sum("amount"), fee=Sum("fee_amount"), gross=Sum("gross_amount"))
        ctx.update(page=_pager(request, qs), status=status, statuses=EqubPayout.Status.choices, agg=agg,
                   by_status=dict(EqubPayout.objects.values_list("status").annotate(s=Sum("amount"))),
                   refund=reports.credit_to_refund())
    elif tab == "members":
        # Everyone in a draw that is coming up: their Equb group, standing and their chance to win.
        show = request.GET.get("show", "")
        rules = EqubSettings.load()
        rounds = due_all.select_related("group")
        if pool.isdigit():
            rounds = rounds.filter(group_id=int(pool))
        rows = []
        for r in rounds:
            info = services.draw_pool(r, rules, today)
            for e in info["entries"]:
                m = e["_m"]
                waiting = m.status == EqubMembership.Status.ACTIVE and not m.has_won
                if not waiting and show != "all":
                    continue
                if show == "in" and not e["eligible"] or show == "out" and e["eligible"]:
                    continue
                if q and q.lower() not in e["name"].lower() and q not in (e["phone"] or ""):
                    continue
                rows.append({"m": m, "round": r, "name": e["name"], "phone": e["phone"], "pos": e["position"],
                             "eligible": e["eligible"], "reasons": e["reasons"], "standing": e["standing"],
                             "standing_label": e["standing_label"], "weight": e["weight"], "factors": e["factors"],
                             "odds": e["odds"], "share": e["share"], "won": m.has_won,
                             "in_draw": info["eligible_count"]})
        rows.sort(key=lambda x: (not x["eligible"], -x["odds"], x["round"].group.name, x["pos"]))
        ctx.update(page=_pager(request, rows), show=show, rules=rules,
                   member_total=len(rows), member_in=sum(1 for x in rows if x["eligible"]))
    else:
        tab = ctx["tab"] = "due"
        qs = due_all
        if pool.isdigit():
            qs = qs.filter(group_id=int(pool))
        if q:
            qs = qs.filter(group__name__icontains=q)
        when = request.GET.get("when", "")
        if when == "now":
            qs = qs.filter(draw_date__lte=today)
        page = _pager(request, qs)
        rules = EqubSettings.load()
        for r in page:
            info = services.draw_pool(r, rules, today)
            r.eligible, r.waiting = info["eligible_count"], info["waiting_count"]
            r.is_due = r.draw_date <= today
            r.days = (r.draw_date - today).days
        ctx.update(page=page, when=when)
    return render(request, "console/equb_draws.html", ctx)


@staff_required
def equb_chance_settings(request):
    """Winning-chance rules for automatic draws: who gets more (or less) chance, with a live example."""
    obj = EqubSettings.load()
    form = f.ChanceSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        rules = form.save(commit=False)
        rules.updated_by = request.user
        rules.save()
        messages.success(request, "Chance settings saved – they apply to the next automatic draw.")
        return redirect(reverse("console:equb_draws") + "?tab=members")
    return render(request, "console/equb_chance_settings.html", {"nav": "equb_draws", "form": form, "rules": obj})


@staff_required
def equb_payouts(request):
    """Old address – payouts now live on the Equb draws page."""
    return redirect(reverse("console:equb_draws") + "?tab=payouts")


# --------------------------------------------------------------------------- rules
@staff_required
def equb_rules(request):
    obj = EqubSettings.load()
    form = f.EqubRulesForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        rules = form.save(commit=False)
        rules.updated_by = request.user
        rules.save()
        messages.success(request, "Equb rules saved – they apply to every group straight away.")
        return redirect("console:equb_rules")
    from .manage_views import _render
    return _render(request, nav="settings", title="Equb rules", crumbs="Settings", form=form,
                   cancel=reverse("console:settings"), obj=obj,
                   intro="These rules apply to every Equb group: when members count as behind, when they leave the draw, "
                         "reminders and the call-centre thresholds. Each group's own contribution, late fee and grace days "
                         "are set on the group.")


def _draw_now_data(rounds) -> list[dict]:
    """Everything the Draw now page needs for each Equb that is collecting a round."""
    rules = EqubSettings.load()
    teams = defaultdict(list)
    for t in GroupEqub.objects.filter(is_active=True, equb_id__in=[r.group_id for r in rounds]).select_related("owner"):
        teams[t.equb_id].append(t)
    out = []
    for r in rounds:
        g = r.group
        pool = services.draw_pool(r, rules)
        members = []
        for e in pool["entries"]:
            m = e["_m"]
            members.append({"id": m.id, "name": e["name"], "phone": e["phone"], "pos": e["position"], "team": e["team_id"],
                            "eligible": e["eligible"], "won": m.has_won,
                            "waiting": m.status == EqubMembership.Status.ACTIVE and not m.has_won,
                            "why": "; ".join(e["reasons"]), "odds": e["odds"], "share": e["share"],
                            "weight": e["weight"], "factors": [f"{x['label']} {x['pct']:+d}%" for x in e["factors"]]})
        team_rows = []
        for t in teams[g.id]:
            mine = [x for x in members if x["team"] == t.id]
            team_rows.append({"id": t.id, "name": t.name, "kind": t.get_kind_display(), "code": t.invite_code,
                              "owner": t.owner.full_name, "size": len(mine),
                              "eligible": sum(1 for x in mine if x["eligible"]),
                              "waiting": sum(1 for x in mine if x["waiting"])})
        fund = services.equb_funds(g, r)
        out.append({"id": r.pk, "equb": g.name, "slug": g.slug, "package": g.equb_package.name if g.equb_package_id else "",
                    "funds": {k: (float(v) if not isinstance(v, int) else v) for k, v in fund.items()},
                    "price": float(g.price_per_person), "members_total": g.members_count,
                    "round": r.number, "total": g.total_rounds, "draw_date": r.draw_date.strftime("%d %b %Y"),
                    "due": r.draw_date <= timezone.localdate(), "draw_type": g.get_draw_type_display(),
                    "amount": float(g.contribution_amount), "every": g.every, "wpr": g.winners_per_round,
                    "expected": float(r.expected_amount), "collected": float(r.collected_amount),
                    "full_pot": g.payout_basis == EqubGroup.PayoutBasis.FULL_POT, "fee": float(g.service_fee_percent),
                    "in_draw": pool["eligible_count"], "waiting": pool["waiting_count"],
                    "members": members, "teams": team_rows})
    return out


@staff_required
def equb_draw_now(request):
    """
    Draw now (full page): 1 automatic or manual → 2 which Equb → 3 an Equb membership or a Group Equb →
    4 the person or the group (manual) – or let the system pick at random among them (automatic).
    """
    rounds = list(_due_rounds())
    if request.method == "POST":
        p = request.POST
        rnd = next((r for r in rounds if str(r.pk) == p.get("round")), None)
        method, scope = p.get("method"), p.get("scope")
        team = GroupEqub.objects.filter(pk=p.get("team") or 0, is_active=True).first() if p.get("team") else None
        note = p.get("note", "").strip()
        error = None
        if p.get("action") == "skip" and rnd:
            _do(request, lambda: services.close_round_without_winner(rnd, request.user, note),
                f"{rnd.group.name} round {rnd.number} closed without a winner – the money stays for the next draw.")
            return redirect(reverse("console:equb_draws"))
        if method not in ("auto", "manual"):
            error = "Choose how to draw: automatic or manual."
        elif not rnd:
            error = "Choose the Equb group to draw."
        elif scope not in ("member", "group"):
            error = "Choose whether an Equb membership or a Group Equb wins."
        elif scope == "group" and (not team or team.equb_id != rnd.group_id):
            error = f"Choose a Group Equb that is part of {rnd.group.name}."
        if error:
            messages.error(request, error)
            return redirect(reverse("console:equb_draw_now") + (f"?round={rnd.pk}" if rnd else ""))
        payouts = None
        if method == "auto":
            how = p.get("win_mode", "count")
            count = int(p.get("win_count") or 0) if (p.get("win_count") or "").isdigit() else None
            try:
                percent = Decimal(p.get("win_percent")) if how == "percent" and p.get("win_percent") else None
            except Exception:  # noqa: BLE001 – a bad number in the form
                percent = None
            if how == "percent" and (percent is None or percent <= 0):
                messages.error(request, "Enter the percentage of members in the draw who should win.")
                return redirect(reverse("console:equb_draw_now") + f"?round={rnd.pk}")
            payouts = _do(request, lambda: services.run_draw(
                rnd, executed_by=request.user, team=team if scope == "group" else None,
                count=count if how != "percent" else None, percent=percent))
        else:
            pool = services.draw_pool(rnd)
            if scope == "group":
                ids = [e["membership_id"] for e in pool["entries"] if e["team_id"] == team.id
                       and e["_m"].status == EqubMembership.Status.ACTIVE and not e["_m"].has_won]
                if not ids:
                    messages.error(request, f"Everyone in {team.name} has already won or left.")
                    return redirect(reverse("console:equb_draw_now") + f"?round={rnd.pk}")
            else:
                ids = [int(x) for x in p.getlist("winners") if x.isdigit()]
            eligible = {e["membership_id"] for e in pool["entries"] if e["eligible"]}
            outside = any(i not in eligible for i in ids)
            payouts = _do(request, lambda: services.run_draw(
                rnd, executed_by=request.user, winner_ids=ids, note=note, include_ineligible=outside,
                team=team if scope == "group" else None, exact=False))
        if payouts:
            messages.success(request, f"🏆 {rnd.group.name} round {rnd.number}: "
                             + ", ".join(f"{x.membership.display_name} wins {services.money(x.amount)} ETB" for x in payouts)
                             + (f" – Group Equb {team.name}" if scope == "group" else ""))
            return redirect(reverse("console:equb_round", args=[rnd.group.slug, rnd.number]))
        return redirect(reverse("console:equb_draw_now") + f"?round={rnd.pk}")
    data = _draw_now_data(rounds)
    return render(request, "console/equb_draw_now.html", {
        "nav": "equb_draws", "data": data, "rounds": rounds, "pre_round": request.GET.get("round", ""),
        "pre_method": request.GET.get("method", ""),
    })


@staff_required
def equb_draw_settings(request):
    obj = EqubSettings.load()
    form = f.DrawSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        rules = form.save(commit=False)
        rules.updated_by = request.user
        rules.save()
        changed = form.update_groups_draw_type()
        messages.success(request, "Draw settings saved." + (f" Draw type changed on {changed} group{'s' if changed != 1 else ''}."
                                                            if changed else ""))
        return redirect("console:equb_draws")
    from .manage_views import _render
    return _render(request, nav="equb_draws", title="Draw settings", crumbs="Equb draws", form=form,
                   cancel=reverse("console:equb_draws"), obj=obj,
                   intro="Who can win a round. Members left out are listed with the reason on every round's draw page. "
                         "Each group's own draw type (manual or automatic) is set on the group.")


# --------------------------------------------------------------------------- Equb packages (NiyaEqubBank style)
@staff_required
def equb_packages(request):
    active = request.GET.get("active", "")
    qs = EqubPackage.objects.annotate(
        n_groups=Count("groups", distinct=True),
        n_open=Count("groups", filter=Q(groups__status=EqubGroup.Status.OPEN), distinct=True),
        n_running=Count("groups", filter=Q(groups__status=EqubGroup.Status.ACTIVE), distinct=True),
        n_members=Count("groups__memberships", filter=~Q(groups__memberships__status=EqubMembership.Status.LEFT),
                        distinct=True),
        min_amount=Min("groups__contribution_amount"), max_amount=Max("groups__contribution_amount"))
    if active in ("1", "0"):
        qs = qs.filter(is_active=active == "1")
    collected = dict(Contribution.objects.filter(status__in=Contribution.SETTLED)
                     .values_list("round__group__equb_package").annotate(s=Sum("amount")))
    packages = list(qs)
    for p in packages:
        p.collected = collected.get(p.id) or 0
    return render(request, "console/equb_packages.html", {
        "nav": "equb_packages", "packages": packages, "active": active,
        "totals": {"packages": len(packages), "groups": sum(p.n_groups for p in packages),
                   "members": sum(p.n_members for p in packages), "collected": sum(p.collected for p in packages)},
    })


@staff_required
def equb_package_detail(request, slug):
    p = get_object_or_404(EqubPackage, slug=slug)
    groups = list(p.groups.annotate(
        members=Count("memberships", filter=~Q(memberships__status=EqubMembership.Status.LEFT))))
    groups.sort(key=lambda g: (STATUS_ORDER.get(g.status, 9), g.start_date, g.name))
    collected = dict(Contribution.objects.filter(status__in=Contribution.SETTLED, round__group__equb_package=p)
                     .values_list("round__group").annotate(s=Sum("amount")))
    for g in groups:
        g.fill = pct(g.members, g.max_members)
        g.collected = collected.get(g.id) or 0
    members = (EqubMembership.objects.filter(group__equb_package=p).exclude(status=EqubMembership.Status.LEFT)
               .select_related("group", "user", "won_round").order_by("-created_at")[:25])
    return render(request, "console/equb_package_detail.html", {
        "nav": "equb_packages", "p": p, "groups": groups, "members": list(members),
        "terms": [line.strip() for line in (p.terms_content or "").splitlines() if line.strip()],
        "stats": {"groups": len(groups), "members": sum(g.members for g in groups),
                  "open": sum(1 for g in groups if g.status == "OPEN"),
                  "collected": sum(collected.values(), Decimal("0"))},
    })


# --------------------------------------------------------------------------- Equb memberships
@staff_required
def equb_memberships(request):
    q, package, group, status = (request.GET.get(k, "").strip() for k in ("q", "package", "group", "status"))
    qs = (EqubMembership.objects.select_related("group__equb_package", "user", "won_round")
          .order_by("-created_at"))
    if q:
        qs = qs.filter(Q(user__full_name__icontains=q) | Q(user__phone__icontains=q) | Q(group__name__icontains=q))
    if package:
        qs = qs.filter(group__equb_package__slug=package)
    if group:
        qs = qs.filter(group__slug=group)
    if status:
        qs = qs.filter(status=status)
    if request.GET.get("export") == "csv":
        items = list(qs)
        stand = st.standings_for(items)
        return _csv(f"vacation-equb-memberships-{timezone.localdate()}.csv",
                    ["Package", "Group", "Position", "Member", "Phone", "Contribution (ETB)", "Status", "Standing",
                     "Paid (ETB)", "Arrears (ETB)", "Won round", "Joined"],
                    ([m.group.equb_package.name if m.group.equb_package_id else "", m.group.name, m.position,
                      m.user.full_name, m.user.phone, f"{m.group.contribution_amount:.2f}", m.get_status_display(),
                      stand[m.id].status_label, f"{m.total_paid:.2f}", f"{stand[m.id].arrears:.2f}",
                      m.won_round.number if m.won_round_id else "", m.created_at.date().isoformat()] for m in items))
    pg = _pager(request, qs)
    standings = st.standings_for(list(pg))
    rows = [{"m": m, "s": standings[m.id]} for m in pg]
    return render(request, "console/equb_memberships.html", {
        "nav": "equb_memberships", "page": pg, "rows": rows, "q": q, "package": package, "group": group,
        "status": status, "statuses": EqubMembership.Status.choices,
        "packages": EqubPackage.objects.order_by("name"),
        "groups": EqubGroup.objects.filter(**({"equb_package__slug": package} if package else {})).order_by("name"),
        "total": qs.count(),
    })


@staff_required
def equb_membership_add(request):
    """Create Equb membership: search customers by name or phone (several at once) or register a new one."""
    initial = {}
    if request.GET.get("group"):
        initial["group"] = EqubGroup.objects.filter(slug=request.GET["group"]).first()
    form = f.AddMemberForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        g, people, reg = form.cleaned_data["group"], list(form.cleaned_data["people"]), form.cleaned_data["register"]
        if reg:
            user = User.objects.create_user(reg["phone"], None, full_name=reg["full_name"])
            people.append(user)
            messages.info(request, f"{user.full_name} was registered as a new customer ({user.phone}). "
                                   "Use “Set password” so they can sign in to the app.")
        joined = []
        for user in people:
            m = _do(request, lambda u=user: services.join_group(u, g))
            if m:
                joined.append(m)
        if joined:
            messages.success(request, ", ".join(f"{m.user.full_name} (#{m.position})" for m in joined)
                             + f" joined {g.name}.")
            if request.POST.get("_another"):
                return redirect(reverse("console:equb_membership_add") + f"?group={g.slug}")
            if len(joined) == 1:
                return redirect("console:equb_member", pk=joined[0].pk)
            return redirect("console:equb", slug=g.slug)
    return render(request, "console/equb_membership_form.html", {"nav": "equb_memberships", "form": form})


# --------------------------------------------------------------------------- Group Equbs (NiyaEqubBank style)
@staff_required
def group_equbs(request):
    kind, state, q = request.GET.get("kind", ""), request.GET.get("state", ""), request.GET.get("q", "").strip()
    base = GroupEqub.objects.all()
    live = ~Q(memberships__status=EqubMembership.Status.LEFT)
    qs = base.select_related("owner", "equb", "equb__equb_package").annotate(
        members=Count("memberships", filter=live & Q(memberships__dependent_name=""), distinct=True),
        dependents=Count("memberships", filter=live & ~Q(memberships__dependent_name=""), distinct=True))
    if kind:
        qs = qs.filter(kind=kind)
    if state == "waiting":
        qs = qs.filter(is_active=True, equb__status=EqubGroup.Status.OPEN)
    elif state == "running":
        qs = qs.filter(is_active=True, equb__status=EqubGroup.Status.ACTIVE)
    elif state == "closed":
        qs = qs.filter(Q(is_active=False) | Q(equb__status__in=["COMPLETED", "CANCELLED"]))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(invite_code__iexact=q) | Q(owner__full_name__icontains=q)
                       | Q(owner__phone__icontains=q) | Q(equb__name__icontains=q))
    collected = dict(EqubMembership.objects.filter(team__isnull=False).values_list("team").annotate(s=Sum("total_paid")))
    page = _pager(request, qs.order_by("-created_at"))
    for t in page:
        t.collected = collected.get(t.id) or 0
    active = base.filter(is_active=True)
    kpi = {"groups": active.count(),
           "waiting": active.filter(equb__status=EqubGroup.Status.OPEN).count(),
           "running": active.filter(equb__status=EqubGroup.Status.ACTIVE).count(),
           "members": EqubMembership.objects.filter(team__is_active=True).exclude(status=EqubMembership.Status.LEFT).count(),
           "collected": sum(collected.values(), Decimal("0"))}
    by_kind = dict(active.values_list("kind").annotate(n=Count("id")))
    return render(request, "console/group_equbs.html", {
        "nav": "group_equbs", "page": page, "kpi": kpi, "kind": kind, "state": state, "q": q,
        "kinds": [(k, l, by_kind.get(k, 0)) for k, l in GroupEqub.Kind.choices]})


@staff_required
def group_equb_add(request):
    form = f.GroupEqubForm(request.POST or None, initial={"equb": request.GET.get("equb"), "kind": "FAMILY"})
    people = f.DependentFormSet(request.POST or None, prefix="people")
    if request.method == "POST" and form.is_valid() and people.is_valid():
        d = form.cleaned_data
        deps = [{"name": x["name"], "phone": x.get("phone", "")}
                for x in people.cleaned_data if x and x.get("name") and not x.get("DELETE")]
        try:
            team = teams.create_group_equb(d["owner"], d["equb"], name=d["name"], kind=d["kind"],
                                           description=d["description"], members=list(d["members"]), dependents=deps,
                                           created_by=request.user)
        except BusinessRuleError as exc:
            form.add_error(None, str(exc.detail))
        else:
            messages.success(request, f"Group Equb “{team.name}” created – {team.live_memberships.count()} places in "
                                      f"{team.equb.name}. Invite code {team.invite_code}.")
            if request.POST.get("_another"):
                return redirect("console:group_equb_add")
            return redirect("console:group_equb", pk=team.pk)
    return render(request, "console/group_equb_form.html", {"nav": "group_equbs", "form": form, "people": people})


@staff_required
def group_equb_detail(request, pk):
    team = get_object_or_404(GroupEqub.objects.select_related("owner", "equb", "equb__equb_package", "created_by"), pk=pk)
    here = reverse("console:group_equb", args=[pk])
    if request.method == "POST":
        a, p = request.POST.get("action"), request.POST
        if a == "add_member":
            from apps.accounts.models import User, normalize_phone
            user = User.objects.filter(phone=normalize_phone(p.get("phone", ""))).first()
            if not user:
                messages.error(request, "No customer is registered with that phone number.")
            else:
                _do(request, lambda: teams.add_member(request.user, team, user), f"{user.full_name} added to the group.")
        elif a == "add_dependent":
            _do(request, lambda: teams.add_dependent(request.user, team, name=p.get("name", ""),
                                                     phone=p.get("phone", "")), "Responsibility person added.")
        elif a == "remove":
            _do(request, lambda: teams.remove_member(request.user, team, int(p.get("membership") or 0)), "Removed from the group.")
        elif a == "new_code":
            teams.new_invite_code(request.user, team)
            messages.success(request, "New invite code created – the old one no longer works.")
        elif a == "close":
            if _do(request, lambda: teams.close_group(request.user, team), "Group closed – its places in the Equb were released."):
                return redirect("console:group_equbs")
        return redirect(here)
    info = teams.group_info(team, request.user)
    members = list(team.memberships.select_related("user", "won_round").order_by("position"))
    return render(request, "console/group_equb_detail.html", {
        "nav": "group_equbs", "t": team, "info": info, "members": members,
        "live": [m for m in members if m.status != EqubMembership.Status.LEFT],
        "can_manage": team.is_active and team.equb.status == EqubGroup.Status.OPEN})
