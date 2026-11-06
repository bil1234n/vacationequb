"""
Agency portal (/agency/): a travel agency's own workspace on Vacation Equb.

Agency staff sign in with the same login page as the console and only ever see their own agency's data:
customers and their families, Equb places, ticket & package bookings, and their earnings after Vacation Equb's
commission.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta
from functools import wraps

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.core.exceptions import BusinessRuleError
from apps.equb.models import EqubGroup, EqubMembership
from apps.payments.models import PaymentTransaction
from apps.travel.models import Airport, Booking, FareClass, Flight, TravelPackage
from apps.travel.services import bookings as booking_svc

from . import forms as f
from . import services
from .models import AgencyCustomer, AgencyLedgerEntry, AgencyStaff, FamilyMember

PER_PAGE = 25


def agency_required(manage: bool = False):
    """Only signed-in staff of an active (or pending) agency; ``manage`` = owners and managers only."""

    def deco(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect(f"{reverse('console:login')}?next={request.get_full_path()}")
            st = services.staff_for(request.user)
            if st is None:
                messages.error(request, "This account is not linked to an active travel agency.")
                return redirect("console:login")
            if manage and not st.can_manage:
                messages.error(request, "Only the agency owner or a manager can do that.")
                return redirect("agency:dashboard")
            request.agency_staff, request.agency = st, st.agency
            return view(request, *args, **kwargs)
        return wrapper
    return deco


def _page(request, qs):
    return Paginator(qs, PER_PAGE).get_page(request.GET.get("page"))


def _do(request, fn, ok: str = ""):
    try:
        out = fn()
    except BusinessRuleError as exc:
        messages.error(request, str(exc.detail))
        return None
    if ok:
        messages.success(request, ok)
    return out if out is not None else True


def _ctx(request, nav, **extra):
    return {"nav": nav, "agency": request.agency, "me": request.agency_staff, **extra}


def _customer(request, pk) -> AgencyCustomer:
    return get_object_or_404(AgencyCustomer.objects.select_related("user"), pk=pk, agency=request.agency)


def _agency_bookings(request):
    return Booking.objects.filter(user__agency_link__agency=request.agency)


# --------------------------------------------------------------------------- dashboard
@agency_required()
def dashboard(request):
    a = request.agency
    month_start = timezone.localdate().replace(day=1)
    since = timezone.make_aware(datetime.combine(month_start, time.min))
    bookings = _agency_bookings(request)
    month = services.summary(a, since=since)
    total = services.summary(a)
    recent = a.ledger.select_related("customer", "booking").order_by("-created_at")[:8]
    pending = bookings.filter(status=Booking.Status.PENDING_PAYMENT).select_related("user", "flight__origin",
                                                                                     "flight__destination", "package")[:6]
    equb_places = EqubMembership.objects.filter(user__agency_link__agency=a, status__in=EqubMembership.LIVE_STATUSES)
    return render(request, "agencies/dashboard.html", _ctx(
        request, "dashboard", month=month, total=total, recent=recent, pending=pending,
        counts={"customers": a.customers.count(),
                "family": FamilyMember.objects.filter(customer__agency=a).count(),
                "equb": equb_places.count(),
                "trips": bookings.filter(status=Booking.Status.CONFIRMED).count()}))


# --------------------------------------------------------------------------- customers
@agency_required()
def customers(request):
    q = request.GET.get("q", "").strip()
    qs = services.search_customers(request.agency, q).annotate(
        n_family=Count("family", distinct=True),
        n_trips=Count("user__bookings", filter=Q(user__bookings__status=Booking.Status.CONFIRMED), distinct=True),
        n_equb=Count("user__equb_memberships", filter=Q(user__equb_memberships__status__in=EqubMembership.LIVE_STATUSES),
                     distinct=True)).order_by("-created_at")
    return render(request, "agencies/customers.html", _ctx(request, "customers", page=_page(request, qs), q=q,
                                                          total=request.agency.customers.count()))


@agency_required()
def customer_add(request):
    form = f.CustomerForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        res = _do(request, lambda: services.add_customer(request.agency, full_name=d["full_name"], phone=d["phone"],
                                                         email=d["email"], password=d["password"], notes=d["notes"],
                                                         added_by=request.user))
        if res:
            cust, created = res
            messages.success(request, f"{cust.user.full_name} is now your customer"
                             + ("." if created else " (they already had a Vacation Equb account)."))
            if request.POST.get("_another"):
                return redirect("agency:customer_add")
            return redirect("agency:customer", pk=cust.pk)
    return render(request, "agencies/form.html", _ctx(request, "customers", form=form, title="New customer",
                                                     cancel=reverse("agency:customers"), another=True))


@agency_required()
def customer_detail(request, pk):
    c = _customer(request, pk)
    here = reverse("agency:customer", args=[pk])
    if request.method == "POST":
        a, p = request.POST.get("action"), request.POST
        if a == "remove_family":
            FamilyMember.objects.filter(pk=p.get("member"), customer=c).delete()
            messages.success(request, "Family member removed.")
        elif a == "password":
            _do(request, lambda: services.set_customer_password(c, p.get("password", "")),
                f"{c.user.first_name} can now sign in to the app with their phone and this password.")
        elif a == "notes":
            c.notes = p.get("notes", "")[:2000]
            c.save(update_fields=["notes", "updated_at"])
            messages.success(request, "Notes saved.")
        return redirect(here)
    places = (EqubMembership.objects.filter(user=c.user).exclude(status=EqubMembership.Status.LEFT)
              .select_related("group").order_by("-created_at"))
    trips = (Booking.objects.filter(user=c.user).select_related("flight__origin", "flight__destination", "package")
             .order_by("-created_at")[:20])
    return render(request, "agencies/customer_detail.html", _ctx(
        request, "customers", c=c, family=c.family.all(), places=places, trips=trips))


@agency_required()
def family_form(request, pk, member=None):
    c = _customer(request, pk)
    obj = get_object_or_404(FamilyMember, pk=member, customer=c) if member else None
    form = f.FamilyForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        fm = form.save(commit=False)
        fm.customer = c
        fm.save()
        messages.success(request, f"{fm.full_name} saved to {c.user.first_name}'s family.")
        if request.POST.get("_another"):
            return redirect("agency:family_add", pk=c.pk)
        return redirect("agency:customer", pk=c.pk)
    return render(request, "agencies/form.html", _ctx(
        request, "customers", form=form, obj=obj, another=not obj,
        title=f"{'Edit' if obj else 'Add'} family member · {c.user.full_name}",
        cancel=reverse("agency:customer", args=[c.pk])))


# --------------------------------------------------------------------------- Equb
@agency_required()
def equbs(request):
    a = request.agency
    groups = (EqubGroup.objects.filter(status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
              .annotate(n=Count("memberships", filter=~Q(memberships__status=EqubMembership.Status.LEFT)))
              .order_by("status", "start_date"))
    mine = (EqubMembership.objects.filter(user__agency_link__agency=a).exclude(status=EqubMembership.Status.LEFT)
            .select_related("group", "user").order_by("-created_at"))
    return render(request, "agencies/equbs.html", _ctx(request, "equb", groups=groups, page=_page(request, mine)))


@agency_required()
def equb_enrol(request, slug):
    g = get_object_or_404(EqubGroup, slug=slug, status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
    custs = request.agency.customers.select_related("user").prefetch_related("family").order_by("user__full_name")
    if request.method == "POST":
        c = _customer(request, request.POST.get("customer") or 0)
        who = request.POST.get("who", "self")
        fm = None
        if who != "self":
            fm = FamilyMember.objects.filter(pk=who, customer=c).first()
            if not fm:
                messages.error(request, "Choose who gets the place.")
                return redirect("agency:equb_enrol", slug=slug)
        m = _do(request, lambda: services.enrol(c, g, family_member=fm))
        if m:
            messages.success(request, f"{m.display_name} holds place #{m.position} in {g.name}"
                             + (f" – {c.user.first_name} pays for them." if fm else "."))
            return redirect("agency:customer", pk=c.pk)
        return redirect("agency:equb_enrol", slug=slug)
    return render(request, "agencies/equb_enrol.html", _ctx(
        request, "equb", g=g, customers=custs, pre=request.GET.get("customer", "")))


# --------------------------------------------------------------------------- booking
@agency_required()
def book(request):
    """Step 1: pick the customer, then a flight (search) or a package."""
    a = request.agency
    cust = None
    if request.GET.get("customer"):
        cust = _customer(request, request.GET["customer"])
    origin, dest, day = (request.GET.get(k, "").strip() for k in ("origin", "destination", "date"))
    flights = Flight.objects.filter(departure_time__gt=timezone.now() + timedelta(hours=2),
                                    status__in=[Flight.Status.SCHEDULED, Flight.Status.DELAYED])
    if origin:
        flights = flights.filter(origin__iata=origin.upper())
    if dest:
        flights = flights.filter(destination__iata=dest.upper())
    if day:
        flights = flights.filter(departure_time__date=day)
    flights = flights.select_related("origin", "destination").prefetch_related("fares").order_by("departure_time")[:30]
    packages = TravelPackage.objects.filter(is_active=True, departure_date__gt=timezone.localdate()).order_by(
        "departure_date")[:12]
    return render(request, "agencies/book.html", _ctx(
        request, "book", cust=cust, customers=a.customers.select_related("user").order_by("user__full_name"),
        flights=flights, packages=packages, airports=Airport.objects.order_by("city"),
        origin=origin, destination=dest, date=day))


def _traveller_initial(c: AgencyCustomer, picked: list[str]) -> list[dict]:
    rows = []
    if "self" in picked:
        first, _, last = c.user.full_name.partition(" ")
        rows.append({"first_name": first, "last_name": last or first, "date_of_birth": c.user.date_of_birth,
                     "passport_number": c.user.passport_number, "passport_expiry": c.user.passport_expiry})
    for fm in c.family.filter(pk__in=[p for p in picked if p.isdigit()]):
        rows.append({"first_name": fm.first_name, "last_name": fm.last_name, "gender": fm.gender,
                     "date_of_birth": fm.date_of_birth, "passport_number": fm.passport_number,
                     "passport_expiry": fm.passport_expiry})
    return rows or [{}]


def _book_product(request, *, fare: FareClass | None = None, package: TravelPackage | None = None):
    c = _customer(request, request.GET.get("customer") or request.POST.get("customer") or 0)
    picked = request.GET.getlist("who") or ["self"]
    formset = f.TravellerFormSet(request.POST or None, initial=None if request.method == "POST"
                                 else _traveller_initial(c, picked), prefix="pax")
    if request.method == "POST" and formset.is_valid():
        pax = [services.passenger_from(**{k: v for k, v in row.items() if k != "DELETE"})
               for row in formset.cleaned_data if row]
        kw = dict(user=c.user, passengers=pax, contact_phone=c.user.phone, contact_email=c.user.email)
        b = _do(request, lambda: (booking_svc.create_flight_booking(fare_class_id=fare.pk, **kw) if fare
                                  else booking_svc.create_package_booking(package_id=package.pk, **kw)))
        if b:
            booking_svc.log_event(b, "NOTE", f"Booked by {request.user.full_name} for {request.agency.name}.",
                                  by=request.user)
            messages.success(request, f"Booking {b.reference} created – seats held until "
                                      f"{timezone.localtime(b.expires_at):%H:%M}. Send the customer the payment link.")
            return redirect("agency:booking", reference=b.reference)
    return render(request, "agencies/book_travellers.html", _ctx(
        request, "book", c=c, fare=fare, package=package, formset=formset,
        family=c.family.all(), picked=picked))


@agency_required()
def book_flight(request, fare_id):
    fare = get_object_or_404(FareClass.objects.select_related("flight__origin", "flight__destination"), pk=fare_id)
    return _book_product(request, fare=fare)


@agency_required()
def book_package(request, pk):
    package = get_object_or_404(TravelPackage, pk=pk, is_active=True)
    return _book_product(request, package=package)


@agency_required()
def bookings(request):
    status = request.GET.get("status", "")
    q = request.GET.get("q", "").strip()
    qs = _agency_bookings(request).select_related("user", "flight__origin", "flight__destination", "package")
    if status:
        qs = qs.filter(status=status)
    if q:
        qs = qs.filter(Q(reference__iexact=q) | Q(user__full_name__icontains=q) | Q(user__phone__icontains=q))
    return render(request, "agencies/bookings.html", _ctx(
        request, "bookings", page=_page(request, qs.order_by("-created_at")), status=status, q=q,
        statuses=Booking.Status.choices))


@agency_required()
def booking_detail(request, reference):
    b = get_object_or_404(_agency_bookings(request).select_related(
        "user", "flight__origin", "flight__destination", "fare_class", "package"), reference=reference)
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "pay":
            txn = _do(request, lambda: services.start_checkout(b))
            if txn:
                messages.success(request, "Payment link ready – open it now or send it to the customer.")
        elif action == "refresh":
            from apps.payments.services import refresh_from_provider

            for txn in PaymentTransaction.objects.filter(object_id=b.pk, purpose="BOOKING", status__in=["INITIATED", "PENDING"]):
                refresh_from_provider(txn)
            messages.info(request, "Payment status checked with the provider.")
        elif action == "cancel":
            _do(request, lambda: booking_svc.cancel_booking(b, f"Cancelled by {request.agency.name}"),
                "Booking cancelled – the seats were released.")
        return redirect("agency:booking", reference=reference)
    payments = PaymentTransaction.objects.filter(purpose="BOOKING", object_id=b.pk).order_by("-created_at")
    live = next((p for p in payments if p.status in ("INITIATED", "PENDING") and p.checkout_url), None)
    entries = AgencyLedgerEntry.objects.filter(booking=b)
    return render(request, "agencies/booking_detail.html", _ctx(
        request, "bookings", b=b, passengers=b.passengers.select_related("seat"), payments=payments, live=live,
        entries=entries, policy=booking_svc.cancellation_policy(b)))


# --------------------------------------------------------------------------- earnings & team
@agency_required()
def earnings(request):
    a = request.agency
    status = request.GET.get("payout", "")
    qs = a.ledger.select_related("customer", "booking")
    if status:
        qs = qs.filter(payout_status=status)
    return render(request, "agencies/earnings.html", _ctx(
        request, "earnings", page=_page(request, qs), total=services.summary(a), payout=status,
        payouts=AgencyLedgerEntry.Payout.choices))


@agency_required()
def team(request):
    a = request.agency
    form = f.StaffForm(request.POST or None)
    if request.method == "POST":
        if not request.agency_staff.can_manage:
            messages.error(request, "Only the agency owner or a manager can manage the team.")
            return redirect("agency:team")
        if request.POST.get("action") == "toggle":
            st = get_object_or_404(AgencyStaff, pk=request.POST.get("staff"), agency=a)
            if st.role == AgencyStaff.Role.OWNER or st.user_id == request.user.id:
                messages.error(request, "You can't disable the owner or yourself.")
            else:
                st.is_active = not st.is_active
                st.save(update_fields=["is_active", "updated_at"])
                messages.success(request, f"{st.user.full_name} {'enabled' if st.is_active else 'disabled'}.")
            return redirect("agency:team")
        if form.is_valid():
            d = form.cleaned_data
            st = _do(request, lambda: services.add_staff(a, full_name=d["full_name"], phone=d["phone"],
                                                         password=d["password"], role=d["role"]))
            if st:
                messages.success(request, f"{st.user.full_name} added as {st.get_role_display().lower()}.")
                return redirect("agency:team")
    return render(request, "agencies/team.html", _ctx(
        request, "team", staff=a.staff.select_related("user"), form=form))


@agency_required()
def profile(request):
    return render(request, "agencies/profile.html", _ctx(request, "profile"))


@require_POST
def logout_view(request):
    from django.contrib.auth import logout

    logout(request)
    return redirect("console:login")
