"""
Equb receivables & reports.

Receivables are a level ("what was owed on a day"), collections are a flow ("what came in
between two days") – they are computed separately on purpose.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from django.contrib.contenttypes.models import ContentType
from django.db.models import Count, Q, Sum
from django.utils import timezone

from apps.payments.models import PaymentTransaction

from . import standing as standing_mod
from .models import Contribution, EqubGroup, EqubMembership, EqubPayment, EqubPayout, EqubSettings, Termination

ZERO = Decimal("0")
AGEING_BANDS = [(1, 30, "1–30 days"), (31, 60, "31–60 days"), (61, 90, "61–90 days"), (91, None, "Over 90 days")]


def _pct(a, b) -> float:
    return round(float(a) / float(b) * 100, 1) if b else 0.0


# --------------------------------------------------------------------------- receivables
def live_standings(group_id: int | None = None, search: str = "", rules: EqubSettings | None = None):
    qs = standing_mod.live_memberships()
    if group_id:
        qs = qs.filter(group_id=group_id)
    if search:
        qs = qs.filter(Q(user__full_name__icontains=search) | Q(user__phone__icontains=search) | Q(group__name__icontains=search))
    members = list(qs.order_by("group__name", "position"))
    return members, standing_mod.standings_for(members, rules)


def receivables_summary(members, standings) -> dict:
    sts = [standings[m.id] for m in members]
    expected = sum((s.expected_to_date for s in sts), ZERO)
    paid = sum((s.paid_amount for s in sts), ZERO)
    paid_due = sum((s.paid_due for s in sts), ZERO)
    arrears = sum((s.arrears for s in sts), ZERO)
    behind = [s for s in sts if s.arrears > 0]
    never = [s for s in sts if s.status == standing_mod.NEVER_PAID]
    critical = [s for s in sts if s.critical]
    return {
        "memberships": len(sts), "people": len({m.user_id for m in members}),
        "expected": expected, "paid": paid, "paid_due": paid_due, "paid_early": paid - paid_due, "arrears": arrears,
        "credit": sum((s.credit for s in sts), ZERO), "due_now": sum((s.due_now for s in sts), ZERO),
        "members_in_arrears": len(behind), "never_paid_count": len(never),
        "never_paid_amount": sum((s.arrears for s in never), ZERO),
        "critical_count": len(critical), "critical_amount": sum((s.arrears for s in critical), ZERO),
        "collection_rate": _pct(paid_due, expected) if expected else 100.0, "arrears_rate": _pct(arrears, expected),
        "by_status": _count_by_status(sts),
    }


def _count_by_status(sts) -> list[dict]:
    counts = defaultdict(int)
    for s in sts:
        counts[s.status] += 1
    order = [standing_mod.CURRENT, standing_mod.AHEAD, standing_mod.WARNED, standing_mod.BLOCKED,
             standing_mod.SUSPENDED, standing_mod.NEVER_PAID, standing_mod.COMPLETED]
    return [{"status": k, "label": standing_mod.STATUS_META[k][0], "tone": standing_mod.STATUS_META[k][1],
             "color": standing_mod.STATUS_COLORS[k], "count": counts[k]} for k in order if counts.get(k)]


def ageing(members, standings) -> list[dict]:
    bands = []
    behind = [standings[m.id] for m in members if standings[m.id].arrears > 0]
    total = sum((s.arrears for s in behind), ZERO)
    for lo, hi, label in AGEING_BANDS:
        rows = [s for s in behind if s.days_overdue >= lo and (hi is None or s.days_overdue <= hi)]
        amount = sum((s.arrears for s in rows), ZERO)
        bands.append({"key": f"{lo}-{hi or ''}", "label": label, "from": lo, "to": hi, "count": len(rows),
                      "amount": amount, "share": _pct(amount, total), "tone": "gold" if lo == 1 else "red"})
    # Owed but within the first days of a missed round (days_overdue == 0 cannot be in arrears; kept for completeness)
    return bands


def debtors(members, standings, *, band: str = "", only_never_paid: bool = False, limit: int = 500) -> list[dict]:
    rows = []
    for m in members:
        s = standings[m.id]
        if s.arrears <= 0:
            continue
        if only_never_paid and s.status != standing_mod.NEVER_PAID:
            continue
        if band:
            lo, _, hi = band.partition("-")
            if s.days_overdue < int(lo or 0) or (hi and s.days_overdue > int(hi)):
                continue
        rows.append({"m": m, "s": s})
    rows.sort(key=lambda r: (-r["s"].critical, -r["s"].arrears, -r["s"].days_overdue))
    return rows[:limit]


def by_pool(members, standings) -> list[dict]:
    pools: dict[int, dict] = {}
    for m in members:
        s = standings[m.id]
        p = pools.setdefault(m.group_id, {"group": m.group, "expected": ZERO, "paid": ZERO, "paid_due": ZERO,
                                          "arrears": ZERO, "credit": ZERO, "debtors": 0, "memberships": 0})
        p["expected"] += s.expected_to_date
        p["paid"] += s.paid_amount
        p["paid_due"] += s.paid_due
        p["arrears"] += s.arrears
        p["credit"] += s.credit
        p["memberships"] += 1
        p["debtors"] += 1 if s.arrears > 0 else 0
    out = list(pools.values())
    for p in out:
        p["collection_rate"] = _pct(p["paid_due"], p["expected"]) if p["expected"] else None
    out.sort(key=lambda p: -p["arrears"])
    return out


# --------------------------------------------------------------------------- collections (flow)
def period_bounds(key: str, start: str = "", end: str = "") -> tuple[date, date, str]:
    today = timezone.localdate()
    if key == "week":
        return today - timedelta(days=6), today, "Last 7 days"
    if key == "90":
        return today - timedelta(days=89), today, "Last 90 days"
    if key == "year":
        return today.replace(month=1, day=1), today, f"{today.year} so far"
    if key == "all":
        first = PaymentTransaction.objects.filter(purpose=PaymentTransaction.Purpose.EQUB_CONTRIBUTION).order_by("completed_at").values_list("completed_at", flat=True).first()
        return (timezone.localtime(first).date() if first else today), today, "All time"
    if key == "custom":
        try:
            s, e = date.fromisoformat(start), date.fromisoformat(end)
            if s > e:
                s, e = e, s
            return s, e, f"{s:%d %b %Y} – {e:%d %b %Y}"
        except ValueError:
            pass
    return today.replace(day=1), today, "This month"


def _aware(d: date, end=False) -> datetime:
    return timezone.make_aware(datetime.combine(d, time.max if end else time.min))


def _txn_group_map(txns) -> dict[int, EqubGroup]:
    """Resolve each Equb payment transaction to its pool (target is a Contribution or an EqubPayment)."""
    ct_c = ContentType.objects.get_for_model(Contribution)
    ct_p = ContentType.objects.get_for_model(EqubPayment)
    c_ids = [t.object_id for t in txns if t.content_type_id == ct_c.id]
    p_ids = [t.object_id for t in txns if t.content_type_id == ct_p.id]
    c_group = dict(Contribution.objects.filter(id__in=c_ids).values_list("id", "round__group_id"))
    p_group = dict(EqubPayment.objects.filter(id__in=p_ids).values_list("id", "membership__group_id"))
    groups = EqubGroup.objects.in_bulk(set(c_group.values()) | set(p_group.values()))
    out = {}
    for t in txns:
        gid = c_group.get(t.object_id) if t.content_type_id == ct_c.id else p_group.get(t.object_id)
        if gid in groups:
            out[t.id] = groups[gid]
    return out


def collections(start: date, end: date, group_id: int | None = None) -> dict:
    txns = list(PaymentTransaction.objects.filter(
        purpose=PaymentTransaction.Purpose.EQUB_CONTRIBUTION, status=PaymentTransaction.Status.SUCCESS,
        completed_at__gte=_aware(start), completed_at__lte=_aware(end, True)).select_related("user"))
    gmap = _txn_group_map(txns)
    if group_id:
        txns = [t for t in txns if getattr(gmap.get(t.id), "id", None) == group_id]
    days = (end - start).days + 1
    monthly = days > 62
    buckets: dict[str, Decimal] = {}
    cursor = start.replace(day=1) if monthly else start
    while cursor <= end:
        buckets[cursor.strftime("%b %Y") if monthly else cursor.strftime("%d %b")] = ZERO
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1) if monthly else cursor + timedelta(days=1)
    by_method: dict[str, Decimal] = defaultdict(lambda: ZERO)
    by_pool_: dict[str, Decimal] = defaultdict(lambda: ZERO)
    payers = set()
    for t in txns:
        d = timezone.localtime(t.completed_at).date()
        key = d.strftime("%b %Y") if monthly else d.strftime("%d %b")
        buckets[key] = buckets.get(key, ZERO) + t.amount
        by_method[t.get_provider_display()] += t.amount
        by_pool_[gmap[t.id].name if t.id in gmap else "—"] += t.amount
        payers.add(t.user_id)
    total = sum((t.amount for t in txns), ZERO)
    office = sum((t.amount for t in txns if t.is_office), ZERO)
    payouts = EqubPayout.objects.filter(created_at__gte=_aware(start), created_at__lte=_aware(end, True))
    if group_id:
        payouts = payouts.filter(round__group_id=group_id)
    pagg = payouts.aggregate(n=Count("id"), s=Sum("amount"), gross=Sum("gross_amount"), fee=Sum("fee_amount"))
    return {
        "start": start, "end": end, "total": total, "count": len(txns), "payers": len(payers),
        "office": office, "online": total - office, "average": (total / len(txns)) if txns else ZERO,
        "series": {"labels": list(buckets), "values": [float(v) for v in buckets.values()], "monthly": monthly},
        "by_method": sorted(((k, v) for k, v in by_method.items()), key=lambda kv: -kv[1]),
        "by_pool": sorted(((k, v) for k, v in by_pool_.items()), key=lambda kv: -kv[1]),
        "payouts_count": pagg["n"] or 0, "payouts_total": pagg["s"] or ZERO,
        "service_fees": pagg["fee"] or ZERO,
        "revenue": pagg["fee"] or ZERO,
        "transactions": sorted(txns, key=lambda t: t.completed_at, reverse=True)[:200],
        "txn_groups": gmap,
    }


def payout_liability() -> dict:
    issued = EqubPayout.objects.filter(status=EqubPayout.Status.ISSUED)
    agg = issued.aggregate(n=Count("id"), s=Sum("amount"))
    return {"issued_count": agg["n"] or 0, "issued_amount": agg["s"] or ZERO}


def credit_to_refund() -> dict:
    """Credit left on places whose Equb is finished or that are closed – owed back to members."""
    qs = EqubMembership.objects.filter(prepaid_credit__gt=0).filter(
        Q(group__status__in=[EqubGroup.Status.COMPLETED, EqubGroup.Status.CANCELLED])
        | Q(status__in=[EqubMembership.Status.LEFT, EqubMembership.Status.DEFAULTED]))
    agg = qs.aggregate(n=Count("id"), s=Sum("prepaid_credit"))
    return {"count": agg["n"] or 0, "amount": agg["s"] or ZERO, "rows": qs.select_related("user", "group")[:100]}


def terminated_debt() -> dict:
    qs = Termination.objects.filter(reinstated_at__isnull=True)
    agg = qs.aggregate(n=Count("id"), s=Sum("arrears_at_termination"))
    return {"count": agg["n"] or 0, "amount": agg["s"] or ZERO}
