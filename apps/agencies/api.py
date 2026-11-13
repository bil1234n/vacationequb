"""
Agency app API (/api/agency/…): travel-agency staff who sign in to the EthioVacation app with their phone and PIN
get their own agency workspace – dashboard, customers & families, Equb places and payments, bookings and earnings.
Everything is limited to the signed-in person's own agency.
"""
from __future__ import annotations

from datetime import datetime, time

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, serializers, status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import BusinessRuleError
from apps.equb import standing as standing_mod
from apps.equb.models import EqubGroup, EqubMembership, EqubPayment
from apps.equb.serializers import EqubGroupListSerializer, MembershipSerializer
from apps.equb.services import payment_plan
from apps.payments.serializers import PaymentTransactionSerializer
from apps.travel.models import Booking
from apps.travel.serializers import BookingListSerializer

from . import services
from .models import AgencyCustomer, AgencyLedgerEntry, FamilyMember


class IsAgencyStaff(permissions.BasePermission):
    message = "This account is not linked to an active travel agency."

    def has_permission(self, request, view):
        st = services.staff_for(request.user)
        if st is None:
            return False
        request.agency_staff, request.agency = st, st.agency
        return True


class AgencyView(APIView):
    permission_classes = [permissions.IsAuthenticated, IsAgencyStaff]

    def customer(self, request, pk) -> AgencyCustomer:
        return get_object_or_404(AgencyCustomer.objects.select_related("user"), pk=pk, agency=request.agency)


def money(v) -> str:
    return str(v or 0)


def _customer_row(c: AgencyCustomer) -> dict:
    return {"id": c.pk, "user_id": c.user_id, "full_name": c.user.full_name, "phone": c.user.phone,
            "email": c.user.email, "can_sign_in": c.user.has_usable_password(), "notes": c.notes,
            "created_at": c.created_at, "family_count": getattr(c, "n_family", None),
            "equb_count": getattr(c, "n_equb", None), "trip_count": getattr(c, "n_trips", None)}


def _family_row(fm: FamilyMember) -> dict:
    return {"id": fm.pk, "first_name": fm.first_name, "last_name": fm.last_name, "full_name": fm.full_name,
            "relation": fm.relation, "relation_display": fm.get_relation_display(), "gender": fm.gender,
            "date_of_birth": fm.date_of_birth, "phone": fm.phone, "passport_number": fm.passport_number,
            "passport_expiry": fm.passport_expiry}


def _ledger_row(e: AgencyLedgerEntry) -> dict:
    return {"id": e.pk, "created_at": e.created_at, "customer": e.customer.full_name,
            "booking": e.booking.reference if e.booking_id else None, "description": e.description,
            "gross_amount": money(e.gross_amount), "commission": money(e.commission),
            "agency_amount": money(e.agency_amount), "payout_status": e.payout_status,
            "payout_status_display": e.get_payout_status_display(), "kind": e.kind}


def _places(qs):
    items = list(qs)
    st = standing_mod.standings_for(items)
    return MembershipSerializer(items, many=True, context={"standings": st}).data


# --------------------------------------------------------------------------- dashboard
class DashboardView(AgencyView):
    def get(self, request):
        a = request.agency
        since = timezone.make_aware(datetime.combine(timezone.localdate().replace(day=1), time.min))
        bookings = Booking.objects.filter(user__agency_link__agency=a)
        month, total = services.summary(a, since=since), services.summary(a)
        places = EqubMembership.objects.filter(user__agency_link__agency=a, status__in=EqubMembership.LIVE_STATUSES)
        behind = [p for p, st in standing_mod.standings_for(places.select_related("group", "won_round")).items()
                  if st.is_behind]
        pending = bookings.filter(status=Booking.Status.PENDING_PAYMENT).select_related(
            "flight__origin", "flight__destination", "package").order_by("-created_at")[:5]
        return Response({
            "agency": {"id": a.pk, "name": a.name, "status": a.status, "status_display": a.get_status_display(),
                       "phone": a.phone, "email": a.email, "city": a.city, "address": a.address,
                       "license_number": a.license_number, "tin": a.tin,
                       "commission_percent": money(a.commission_percent), "has_split": a.has_split,
                       "bank_name": a.bank_name, "account_name": a.account_name,
                       "account_number_masked": ("•••• " + a.account_number[-4:]) if a.account_number else ""},
            "me": {"role": request.agency_staff.role, "role_display": request.agency_staff.get_role_display(),
                   "can_manage": request.agency_staff.can_manage},
            "month": {k: money(v) if k != "sales" else v for k, v in month.items()},
            "total": {k: money(v) if k != "sales" else v for k, v in total.items()},
            "counts": {"customers": a.customers.count(),
                       "family": FamilyMember.objects.filter(customer__agency=a).count(),
                       "equb_places": places.count(), "equb_behind": len(behind),
                       "trips": bookings.filter(status=Booking.Status.CONFIRMED).count(),
                       "pending_bookings": bookings.filter(status=Booking.Status.PENDING_PAYMENT).count()},
            "pending_bookings": BookingListSerializer(pending, many=True).data,
            "recent_sales": [_ledger_row(e) for e in a.ledger.select_related("customer", "booking")[:6]],
        })


# --------------------------------------------------------------------------- customers
class CustomerCreateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=20)
    email = serializers.EmailField(required=False, allow_blank=True, default="")
    pin = serializers.CharField(required=False, allow_blank=True, default="", max_length=6)
    notes = serializers.CharField(required=False, allow_blank=True, default="", max_length=2000)


class CustomersView(AgencyView):
    def get(self, request):
        q = request.query_params.get("q", "").strip()
        qs = services.search_customers(request.agency, q).annotate(
            n_family=Count("family", distinct=True),
            n_trips=Count("user__bookings", filter=Q(user__bookings__status=Booking.Status.CONFIRMED), distinct=True),
            n_equb=Count("user__equb_memberships",
                         filter=Q(user__equb_memberships__status__in=EqubMembership.LIVE_STATUSES), distinct=True),
        ).order_by("-created_at")[:200]
        return Response([_customer_row(c) for c in qs])

    def post(self, request):
        s = CustomerCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        c, created = services.add_customer(request.agency, full_name=d["full_name"], phone=d["phone"],
                                           email=d["email"], password=d["pin"], notes=d["notes"],
                                           added_by=request.user)
        return Response({**_customer_row(c), "created_account": created}, status=status.HTTP_201_CREATED)


class CustomerDetailView(AgencyView):
    def get(self, request, pk):
        c = self.customer(request, pk)
        places = (EqubMembership.objects.filter(user=c.user).exclude(status=EqubMembership.Status.LEFT)
                  .select_related("group", "won_round", "team").order_by("-created_at"))
        trips = (Booking.objects.filter(user=c.user).select_related("flight__origin", "flight__destination",
                                                                   "package")
                 .order_by("-created_at")[:20])
        return Response({**_customer_row(c), "family": [_family_row(f) for f in c.family.all()],
                         "places": _places(places), "trips": BookingListSerializer(trips, many=True).data})

    def patch(self, request, pk):
        c = self.customer(request, pk)
        if "notes" in request.data:
            c.notes = str(request.data.get("notes", ""))[:2000]
            c.save(update_fields=["notes", "updated_at"])
        if request.data.get("pin"):
            services.set_customer_password(c, str(request.data["pin"]))
        return self.get(request, pk)


class FamilySerializer(serializers.ModelSerializer):
    class Meta:
        model = FamilyMember
        fields = ["first_name", "last_name", "relation", "gender", "date_of_birth", "phone", "passport_number",
                  "passport_expiry"]

    def validate_date_of_birth(self, d):
        if d > timezone.localdate():
            raise serializers.ValidationError("The date of birth can't be in the future.")
        return d


class FamilyView(AgencyView):
    def post(self, request, pk):
        c = self.customer(request, pk)
        s = FamilySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        fm = s.save(customer=c)
        return Response(_family_row(fm), status=status.HTTP_201_CREATED)

    def delete(self, request, pk):
        c = self.customer(request, pk)
        FamilyMember.objects.filter(pk=request.query_params.get("member") or 0, customer=c).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------------- Equb
class EqubsView(AgencyView):
    """Equbs a customer can be given a place in, and every place the agency's customers hold."""

    def get(self, request):
        groups = (EqubGroup.objects.filter(status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
                  .select_related("equb_package").order_by("status", "start_date"))
        places = (EqubMembership.objects.filter(user__agency_link__agency=request.agency)
                  .exclude(status=EqubMembership.Status.LEFT).select_related("group", "user", "won_round", "team")
                  .order_by("-created_at")[:300])
        rows = _places(places)
        owners = {m.id: (m.user.full_name, m.user.agency_link.pk) for m in places}
        for r in rows:
            r["customer_name"], r["customer_id"] = owners.get(r["id"], ("", None))
        return Response({"groups": EqubGroupListSerializer(groups, many=True).data, "places": rows})


class EnrolView(AgencyView):
    def post(self, request, pk):
        c = self.customer(request, pk)
        g = get_object_or_404(EqubGroup, slug=request.data.get("group", ""),
                              status__in=[EqubGroup.Status.OPEN, EqubGroup.Status.ACTIVE])
        fm = None
        if request.data.get("family_member"):
            fm = FamilyMember.objects.filter(pk=request.data["family_member"], customer=c).first()
            if not fm:
                raise BusinessRuleError("Choose who gets the place.")
        m = services.enrol(c, g, family_member=fm)
        return Response({"membership_id": m.id, "position": m.position, "name": m.display_name},
                        status=status.HTTP_201_CREATED)


class PlaceView(AgencyView):
    """One customer place: what is owed and the ways to pay (recorded by the agency, or a payment link)."""

    def get(self, request, membership_id):
        m = services.customer_place(request.agency, membership_id)
        st = standing_mod.standing_for(m)
        return Response({"place": MembershipSerializer(m, context={"standings": {m.id: st}}).data,
                         "customer_name": m.user.full_name, "customer_phone": m.user.phone,
                         "schedule": standing_mod.schedule_for(m), "plan": payment_plan(m),
                         "methods": [{"value": k, "label": v} for k, v in EqubPayment.Method.choices
                                     if k in EqubPayment.OFFICE_METHODS]})


class RecordPaymentSerializer(serializers.Serializer):
    method = serializers.ChoiceField(choices=[m.value for m in EqubPayment.OFFICE_METHODS])
    rounds = serializers.IntegerField(required=False, min_value=1, max_value=120)
    amount = serializers.DecimalField(required=False, max_digits=12, decimal_places=2, min_value=1)
    reference = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    note = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)


class PlacePayView(AgencyView):
    def post(self, request, membership_id):
        s = RecordPaymentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        if not d.get("rounds") and not d.get("amount"):
            raise BusinessRuleError("Choose how many rounds were paid, or enter the amount received.")
        record = services.record_equb_payment(request.agency, request.user, membership_id, method=d["method"],
                                              rounds=d.get("rounds"), amount=d.get("amount"),
                                              reference=d["reference"], note=d["note"])
        return Response({"reference": record.reference, "amount": money(record.amount),
                         "allocation": record.allocation}, status=status.HTTP_201_CREATED)


class PlaceCheckoutView(AgencyView):
    def post(self, request, membership_id):
        rounds = int(request.data.get("rounds") or 1)
        txn = services.equb_checkout(request.agency, membership_id, rounds)
        return Response(PaymentTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)


# --------------------------------------------------------------------------- bookings & earnings
class BookingsView(AgencyView):
    def get(self, request):
        qs = (Booking.objects.filter(user__agency_link__agency=request.agency)
              .select_related("user", "flight__origin", "flight__destination", "package")
              .order_by("-created_at"))
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        rows = []
        for b in qs[:100]:
            row = BookingListSerializer(b).data
            row["customer_name"], row["customer_phone"] = b.user.full_name, b.user.phone
            rows.append(row)
        return Response(rows)


class BookingCheckoutView(AgencyView):
    def post(self, request, reference):
        b = get_object_or_404(Booking, reference=reference.upper(), user__agency_link__agency=request.agency)
        txn = services.start_checkout(b)
        return Response(PaymentTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)


class EarningsView(AgencyView):
    def get(self, request):
        a = request.agency
        qs = a.ledger.select_related("customer", "booking")
        payout = request.query_params.get("payout")
        if payout:
            qs = qs.filter(payout_status=payout)
        return Response({"total": {k: money(v) if k != "sales" else v for k, v in services.summary(a).items()},
                         "entries": [_ledger_row(e) for e in qs[:200]]})


class TeamView(AgencyView):
    def get(self, request):
        return Response([{"id": s.pk, "full_name": s.user.full_name, "phone": s.user.phone, "role": s.role,
                          "role_display": s.get_role_display(), "is_active": s.is_active,
                          "is_me": s.user_id == request.user.id}
                         for s in request.agency.staff.select_related("user")])
