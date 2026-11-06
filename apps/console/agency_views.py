"""Console: manage travel agencies (partners), their Chapa payout subaccount, commission and payouts."""
from __future__ import annotations

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from apps.agencies import services as ag
from apps.agencies.forms import AgencyForm, StaffForm
from apps.agencies.models import Agency, AgencyLedgerEntry
from apps.core.exceptions import BusinessRuleError

from .views import page, staff_required

Payout = AgencyLedgerEntry.Payout


@staff_required
def agencies(request):
    q, status = request.GET.get("q", "").strip(), request.GET.get("status", "")
    qs = Agency.objects.annotate(
        n_customers=Count("customers", distinct=True),
        sales=Sum("ledger__gross_amount", filter=~Q(ledger__payout_status=Payout.REVERSED)),
        commission=Sum("ledger__commission", filter=~Q(ledger__payout_status=Payout.REVERSED)),
        owed=Sum("ledger__agency_amount", filter=Q(ledger__payout_status=Payout.OWED)))
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(phone__icontains=q) | Q(license_number__icontains=q))
    if status:
        qs = qs.filter(status=status)
    live = AgencyLedgerEntry.objects.exclude(payout_status=Payout.REVERSED)
    kpi = {"agencies": Agency.objects.filter(status=Agency.Status.ACTIVE).count(),
           "sales": live.aggregate(s=Sum("gross_amount"))["s"] or 0,
           "commission": live.aggregate(s=Sum("commission"))["s"] or 0,
           "owed": AgencyLedgerEntry.objects.filter(payout_status=Payout.OWED).aggregate(s=Sum("agency_amount"))["s"] or 0}
    return render(request, "console/agencies.html", {
        "nav": "agencies", "page": page(request, qs.order_by("name")), "q": q, "status": status,
        "statuses": Agency.Status.choices, "kpi": kpi})


@staff_required
def agency_form(request, pk=None):
    obj = get_object_or_404(Agency, pk=pk) if pk else None
    form = AgencyForm(request.POST or None, instance=obj, banks=ag.chapa_banks())
    if request.method == "POST" and form.is_valid():
        if obj:
            form.save()
            messages.success(request, "Agency saved.")
            return redirect("console:agency", pk=obj.pk)
        d = form.cleaned_data
        try:
            with transaction.atomic():
                agency = form.save(commit=False)
                agency.created_by = request.user
                agency.save()
                ag.add_staff(agency, full_name=d["owner_name"], phone=d["owner_phone"],
                             password=d["owner_password"], role="OWNER")
        except BusinessRuleError as exc:
            form.instance.pk = None
            form.add_error(None, str(exc.detail))
        else:
            messages.success(request, f"{agency.name} created. The owner signs in at /console/login/ with "
                                      f"{d['owner_phone']} and is taken to the agency portal.")
            return redirect("console:agency", pk=agency.pk)
    from .manage_views import _render

    return _render(request, nav="agencies", title=f"Edit {obj.name}" if obj else "New travel agency",
                   crumbs="Travel agencies", form=form, obj=obj,
                   cancel=reverse("console:agency", args=[obj.pk]) if obj else reverse("console:agencies"))


@staff_required
def agency_detail(request, pk):
    a = get_object_or_404(Agency, pk=pk)
    here = reverse("console:agency", args=[pk])
    staff_form = StaffForm(request.POST if request.POST.get("action") == "add_staff" else None)
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "subaccount":
                sub = ag.create_chapa_subaccount(a)
                messages.success(request, f"Chapa subaccount created ({sub}). From now on Chapa pays "
                                          f"{a.name} its share of every online sale automatically.")
            elif action == "mark_paid":
                ids = request.POST.getlist("entries")
                n = ag.mark_paid(list(a.ledger.filter(pk__in=ids)), by=request.user,
                                 reference=request.POST.get("reference", ""))
                messages.success(request, f"{n} sale{'s' if n != 1 else ''} marked as paid to {a.name}.")
            elif action == "status":
                new = request.POST.get("status")
                if new in Agency.Status.values:
                    a.status = new
                    a.save(update_fields=["status", "updated_at"])
                    messages.success(request, f"{a.name} is now {a.get_status_display().lower()}.")
            elif action == "add_staff":
                if staff_form.is_valid():
                    d = staff_form.cleaned_data
                    st = ag.add_staff(a, full_name=d["full_name"], phone=d["phone"], password=d["password"],
                                      role=d["role"])
                    messages.success(request, f"{st.user.full_name} can now sign in to {a.name}'s portal.")
                else:
                    messages.error(request, "Check the team member's details.")
        except BusinessRuleError as exc:
            messages.error(request, str(exc.detail))
        return redirect(here)
    owed = a.ledger.filter(payout_status=Payout.OWED).select_related("customer", "booking")
    return render(request, "console/agency_detail.html", {
        "nav": "agencies", "a": a, "total": ag.summary(a), "owed": owed,
        "ledger": a.ledger.select_related("customer", "booking")[:30],
        "staff": a.staff.select_related("user"), "customers": a.customers.select_related("user")[:15],
        "n_customers": a.customers.count(), "staff_form": StaffForm(), "statuses": Agency.Status.choices})
