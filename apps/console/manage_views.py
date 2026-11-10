"""Create / edit / delete screens for the staff console (replaces the raw Django admin for daily work)."""
from datetime import timedelta

from django.contrib import messages
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.accounts.models import User
from apps.equb.models import EqubGroup, EqubPackage, EqubSettings
from apps.travel.models import Airport, BaggageOption, FareClass, Flight, TravelPackage

from . import forms as f
from .views import staff_required

DEFAULT_FARES = [
    {"cabin": "ECONOMY", "code": "K", "name": "Economy Saver", "checked_baggage_kg": 23, "cabin_baggage_kg": 7, "change_fee": 2500},
    {"cabin": "ECONOMY", "code": "Y", "name": "Economy Flex", "checked_baggage_kg": 46, "cabin_baggage_kg": 7, "refundable": True, "free_seat_selection": True},
    {"cabin": "BUSINESS", "code": "J", "name": "Business", "checked_baggage_kg": 64, "cabin_baggage_kg": 14, "refundable": True, "free_seat_selection": True},
]


def _render(request, *, nav, title, crumbs, form, cancel, delete_url=None, formset=None, intro=None, obj=None):
    return render(request, "console/form.html", {"nav": nav, "title": title, "crumbs": crumbs, "form": form, "formset": formset,
                                                 "cancel": cancel, "delete_url": delete_url, "intro": intro, "obj": obj})


@staff_required
def package_form(request, pk=None):
    obj = get_object_or_404(TravelPackage, pk=pk) if pk else None
    form = f.PackageForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        pkg = form.save()
        messages.success(request, f"Package “{pkg.name}” {'updated' if obj else 'created'}.")
        return redirect("console:packages")
    return _render(request, nav="packages", title=f"Edit {obj.name}" if obj else "New travel package",
                   crumbs="Hajj, Umrah & tours", form=form, cancel=reverse("console:packages"), obj=obj,
                   delete_url=reverse("console:package_delete", args=[obj.pk]) if obj and not obj.bookings.exists() else None)


@staff_required
@require_POST
def package_delete(request, pk):
    obj = get_object_or_404(TravelPackage, pk=pk)
    if obj.bookings.exists():
        messages.error(request, "This package has bookings; untick “Available for booking” instead.")
    else:
        obj.delete()
        messages.success(request, "Package deleted.")
    return redirect("console:packages")


@staff_required
def flight_form(request, pk=None):
    obj = get_object_or_404(Flight, pk=pk) if pk else None
    form = f.FlightForm(request.POST or None, instance=obj)
    FS = f.forms.inlineformset_factory(Flight, FareClass, form=f.FareForm, formset=f.BaseFareFormSet,
                                       extra=0 if obj else 3, can_delete=True)
    formset = FS(request.POST or None, instance=obj or Flight(), prefix="fares", initial=None if obj else DEFAULT_FARES)
    if request.method == "POST":
        form_ok = form.is_valid()  # validate both so every error shows at once
        formset.aircraft = (getattr(form, "cleaned_data", {}) or {}).get("aircraft") or (obj.aircraft if obj else None)
        fares_ok = formset.is_valid()
    if request.method == "POST" and form_ok and fares_ok:
        with transaction.atomic():
            flight = form.save()
            formset.instance = flight
            formset.save()
            created = flight.generate_seats()
        msg = f"Flight {flight.flight_number} {'updated' if obj else 'created'}."
        if created:
            msg += f" Seat map generated ({created} seats)."
        messages.success(request, msg)
        return redirect("console:flight", pk=flight.pk)
    return _render(request, nav="flights", title=f"Edit {obj.flight_number}" if obj else "New flight", crumbs="Flights & seats",
                   form=form, formset=formset, cancel=reverse("console:flight", args=[obj.pk]) if obj else reverse("console:flights"),
                   obj=obj, intro="Seats are generated automatically from the aircraft layout when you save.",
                   delete_url=reverse("console:flight_delete", args=[obj.pk]) if obj and not obj.bookings.exists() else None)


@staff_required
@require_POST
def flight_delete(request, pk):
    obj = get_object_or_404(Flight, pk=pk)
    if obj.bookings.exists():
        messages.error(request, "This flight has bookings; set its status to Cancelled instead.")
        return redirect("console:flight", pk=pk)
    obj.delete()
    messages.success(request, "Flight deleted.")
    return redirect("console:flights")


@staff_required
def equb_form(request, slug=None):
    obj = get_object_or_404(EqubGroup, slug=slug) if slug else None
    initial = None
    if not obj:
        initial = {"status": "OPEN", "duration_value": 12, "duration_unit": "MONTHS",
                   "draw_type": "MANUAL", "max_members": 12, "start_date": timezone.localdate() + timedelta(days=7)}
        pkg = EqubPackage.objects.filter(pk=request.GET.get("package") or 0, is_active=True).first()
        if pkg:  # "+ New group" from a package page
            n = pkg.groups.count() + 1
            initial.update(pkg.group_defaults(), equb_package=pkg, name=f"{pkg.name} – Group {n}")
    form = f.EqubForm(request.POST or None, instance=obj, initial=initial)
    if request.method == "POST" and form.is_valid():
        from apps.equb import services as equb_services

        group = form.save(commit=False)
        if not obj:
            group.organizer = request.user
        was = obj and EqubGroup.objects.filter(pk=obj.pk).values_list("status", flat=True).first()
        starting = group.status == EqubGroup.Status.ACTIVE and was != EqubGroup.Status.ACTIVE
        if starting:  # "Started" chosen in the form: start it properly (rounds, first round open, members told)
            group.status = EqubGroup.Status.OPEN
        group.save()
        if starting:
            equb_services.start_group(group)
        elif obj and group.status == EqubGroup.Status.ACTIVE:
            equb_services.sync_running_group(group)  # new amount / schedule / rounds / size apply to the running Equb
        messages.success(request, f"Equb group “{group.name}” {'updated' if obj else 'created'}.")
        if not obj and request.POST.get("_another"):
            return redirect(reverse("console:equb_add") + (f"?package={group.equb_package_id}" if group.equb_package_id else ""))
        return redirect("console:equb", slug=group.slug)
    return _render(request, nav="equb", title=f"Edit {obj.name}" if obj else "Create Equb group", crumbs="Equb groups", form=form,
                   cancel=reverse("console:equb", args=[obj.slug]) if obj else reverse("console:equbs"), obj=obj,
                   delete_url=reverse("console:equb_delete", args=[obj.slug]) if obj and not obj.memberships.exists() else None)


@staff_required
@require_POST
def equb_delete(request, slug):
    obj = get_object_or_404(EqubGroup, slug=slug)
    if obj.memberships.exists():
        messages.error(request, "Members have joined this Equb; set it to Cancelled instead.")
        return redirect("console:equb", slug=slug)
    obj.delete()
    messages.success(request, "Equb deleted.")
    return redirect("console:equbs")


@staff_required
def equb_package_form(request, slug=None):
    obj = get_object_or_404(EqubPackage, slug=slug) if slug else None
    form = f.EqubPackageForm(request.POST or None, instance=obj, initial=None if obj else {"is_active": True})
    if request.method == "POST" and form.is_valid():
        pkg = form.save()
        messages.success(request, f"Equb package “{pkg.name}” {'updated' if obj else 'created'}.")
        if not obj and request.POST.get("_another"):
            return redirect("console:equb_package_add")
        return redirect("console:equb_package", slug=pkg.slug)
    return _render(request, nav="equb_packages", title=f"Edit {obj.name}" if obj else "Create Equb package",
                   crumbs="Equb packages", form=form, obj=obj,
                   cancel=reverse("console:equb_package", args=[obj.slug]) if obj else reverse("console:equb_packages"),
                   delete_url=reverse("console:equb_package_delete", args=[obj.slug]) if obj and not obj.groups.exists() else None)


@staff_required
@require_POST
def equb_package_delete(request, slug):
    obj = get_object_or_404(EqubPackage, slug=slug)
    if obj.groups.exists():
        messages.error(request, "This package has groups; make it inactive instead.")
        return redirect("console:equb_package", slug=slug)
    obj.delete()
    messages.success(request, "Equb package deleted.")
    return redirect("console:equb_packages")




@staff_required
def settings_page(request):
    return render(request, "console/settings.html", {
        "nav": "settings",
        "airports": Airport.objects.annotate(n=Count("departures", distinct=True) + Count("arrivals", distinct=True)).order_by("city"),
        "bags": BaggageOption.objects.all(),
        "staff": User.objects.filter(is_staff=True).order_by("-is_superuser", "full_name"),
        "aircraft": f.AircraftType.objects.all(),
        "equb_rules": EqubSettings.load(),
    })


def _simple(request, form_cls, model, pk, title_new, label):
    obj = get_object_or_404(model, pk=pk) if pk else None
    form = form_cls(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"{label} saved.")
        return redirect("console:settings")
    return _render(request, nav="settings", title=f"Edit {obj}" if obj else title_new, crumbs="Settings", form=form,
                   cancel=reverse("console:settings"), obj=obj)


@staff_required
def airport_form(request, pk=None):
    return _simple(request, f.AirportForm, Airport, pk, "New airport", "Airport")


@staff_required
def baggage_form(request, pk=None):
    return _simple(request, f.BaggageForm, BaggageOption, pk, "New baggage option", "Baggage option")


@staff_required
def staff_form(request):
    if not request.user.is_superuser:
        messages.error(request, "Only full administrators can add staff.")
        return redirect("console:settings")
    form = f.StaffForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        u = form.save()
        messages.success(request, f"{u.full_name} can now sign in with {u.phone}.")
        return redirect("console:settings")
    return _render(request, nav="settings", title="Add staff member", crumbs="Settings", form=form, cancel=reverse("console:settings"))


@staff_required
@require_POST
def staff_toggle(request, pk):
    u = get_object_or_404(User, pk=pk, is_staff=True)
    if not request.user.is_superuser or u == request.user:
        messages.error(request, "You can't change this account.")
    else:
        u.is_active = not u.is_active
        u.save(update_fields=["is_active"])
        messages.success(request, f"{u.full_name} {'re-activated' if u.is_active else 'deactivated'}.")
    return redirect("console:settings")


# --------------------------------------------------------------------------- mobile app: banners & updates
@staff_required
def app_content(request):
    from apps.core.models import AppRelease, Banner

    return render(request, "console/app_content.html", {
        "nav": "app_content", "banners": Banner.objects.all(), "releases": AppRelease.objects.all()[:30],
        "now": timezone.now()})


@staff_required
def banner_form(request, pk=None):
    from apps.core.models import Banner

    obj = get_object_or_404(Banner, pk=pk) if pk else None
    form = f.BannerForm(request.POST or None, request.FILES or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, f"Banner {'updated' if obj else 'added'}.")
        return redirect("console:app_content")
    return _render(request, nav="app_content", title="Edit banner" if obj else "New home banner", crumbs="Mobile app",
                   form=form, cancel=reverse("console:app_content"), obj=obj,
                   delete_url=reverse("console:banner_delete", args=[obj.pk]) if obj else None)


@staff_required
@require_POST
def banner_delete(request, pk):
    from apps.core.models import Banner

    get_object_or_404(Banner, pk=pk).delete()
    messages.success(request, "Banner deleted.")
    return redirect("console:app_content")


@staff_required
def release_form(request, pk=None):
    from apps.core.models import AppRelease

    obj = get_object_or_404(AppRelease, pk=pk) if pk else None
    form = f.AppReleaseForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        rel = form.save()
        messages.success(request, f"{rel} saved – older apps now show the "
                                  f"{'forced' if rel.kind == 'FORCE' else 'optional'} update popup.")
        return redirect("console:app_content")
    return _render(request, nav="app_content", title="Edit app update" if obj else "New app update", crumbs="Mobile app",
                   form=form, cancel=reverse("console:app_content"), obj=obj,
                   delete_url=reverse("console:release_delete", args=[obj.pk]) if obj else None)


@staff_required
@require_POST
def release_delete(request, pk):
    from apps.core.models import AppRelease

    get_object_or_404(AppRelease, pk=pk).delete()
    messages.success(request, "App update removed.")
    return redirect("console:app_content")
