from django.contrib.contenttypes.models import ContentType
from decimal import Decimal, InvalidOperation

from django.db.models import Count, F, Q
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsStaffOrReadOnly
from apps.payments.serializers import PayRequestSerializer, PaymentTransactionSerializer
from apps.travel.models import Booking

from . import operations, services, teams
from . import standing as standing_mod
from .models import (Contribution, EqubGroup, EqubMembership, EqubPackage, EqubPayment, EqubPayout, EqubRound,
                     EqubSettings, GroupEqub, GroupEqubInvite, is_monthly)
from .serializers import (ContributionSerializer, DependentSerializer, GroupEqubCreateSerializer, InviteIdSerializer, InvitePhoneSerializer, RemoveMemberSerializer, EqubGroupDetailSerializer, EqubGroupListSerializer,
                          EqubGroupWriteSerializer, EqubPaymentSerializer, EqubPayoutSerializer, EqubRoundSerializer,
                          ExitRequestSerializer, JoinByCodeSerializer, MemberPaySerializer, MembershipSerializer,
                          RedeemSerializer, rules_summary)


class EqubPackageViewSet(viewsets.ReadOnlyModelViewSet):
    """Equb packages (the templates groups are created from). List them, then ``equb/groups/?package=<slug>``."""

    serializer_class = None
    permission_classes = [permissions.AllowAny]
    lookup_field = "slug"
    pagination_class = None

    def get_serializer_class(self):
        from .serializers import EqubPackageSerializer

        return EqubPackageSerializer

    def get_queryset(self):
        from django.db.models import Count

        return (EqubPackage.objects.filter(is_active=True).prefetch_related("groups")
                .annotate(open_groups=Count("groups", filter=Q(groups__status=EqubGroup.Status.OPEN,
                                                               ))))


class EqubGroupViewSet(viewsets.ModelViewSet):
    """Destination Equb pools. Members join, contribute each round and a winner is drawn per cycle."""

    permission_classes = [IsStaffOrReadOnly]
    lookup_field = "slug"
    filterset_fields = ["status", "frequency"]
    search_fields = ["name", "rules_text", "equb_package__name"]
    ordering_fields = ["start_date", "contribution_amount", "max_members"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        qs = EqubGroup.objects.select_related("organizer", "equb_package")
        qp = self.request.query_params
        if qp.get("package"):
            qs = qs.filter(equb_package__slug=qp["package"])
        # Advanced search in the app: amount range, how often, groups with free places.
        for param, lookup in (("min_amount", "contribution_amount__gte"), ("max_amount", "contribution_amount__lte")):
            try:
                if qp.get(param):
                    qs = qs.filter(**{lookup: Decimal(qp[param])})
            except (InvalidOperation, ValueError):
                pass
        if (qp.get("frequency_days") or "").isdigit():
            days = int(qp["frequency_days"])
            qs = qs.filter(frequency_days__in=range(28, 32)) if is_monthly(days) else qs.filter(frequency_days=days)
        if qp.get("statuses"):
            qs = qs.filter(status__in=[x for x in qp["statuses"].upper().split(",") if x])
        if qp.get("has_spots") in ("1", "true"):
            qs = qs.annotate(_n=Count("memberships", filter=~Q(memberships__status=EqubMembership.Status.LEFT))) \
                   .filter(status=EqubGroup.Status.OPEN, _n__lt=F("max_members"))
        user = self.request.user
        if user.is_staff:
            return qs
        visible = ~Q(status=EqubGroup.Status.DRAFT)
        if user.is_authenticated:  # private pools stay visible to their members
            mine = EqubMembership.objects.filter(user=user).exclude(status=EqubMembership.Status.LEFT).values("group_id")
            visible |= Q(pk__in=mine)
        return qs.filter(visible)

    def get_serializer_class(self):
        if self.action in ("create", "partial_update"):
            return EqubGroupWriteSerializer
        return EqubGroupListSerializer if self.action == "list" else EqubGroupDetailSerializer

    def perform_create(self, serializer):
        serializer.save(organizer=self.request.user)

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAuthenticated])
    def join(self, request, slug=None):
        membership = services.join_group(request.user, self.get_object())
        data = EqubGroupDetailSerializer(self.get_object(), context={"request": request}).data
        data["membership_id"] = membership.id
        return Response(data, status=status.HTTP_201_CREATED)

    @extend_schema(request=JoinByCodeSerializer)
    @action(detail=False, methods=["post"], url_path="join-by-code", permission_classes=[permissions.IsAuthenticated])
    def join_by_code(self, request):
        s = JoinByCodeSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        membership = services.join_by_code(request.user, s.validated_data["code"])
        data = EqubGroupDetailSerializer(membership.group, context={"request": request}).data
        data["membership_id"] = membership.id
        return Response(data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAuthenticated])
    def leave(self, request, slug=None):
        services.leave_group(request.user, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAdminUser])
    def start(self, request, slug=None):
        group = services.start_group(self.get_object())
        return Response(EqubGroupDetailSerializer(group, context={"request": request}).data)

    @action(detail=True, methods=["get"])
    def rounds(self, request, slug=None):
        return Response(EqubRoundSerializer(self.get_object().rounds.select_related("draw"), many=True).data)

    @action(detail=True, methods=["get"])
    def ledger(self, request, slug=None):
        """Transparent member ledger (phone numbers masked)."""
        group = self.get_object()
        return Response({"group": group.slug, **services.group_totals(group), "members": services.member_ledger(group)})


class EqubRoundViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = EqubRound.objects.select_related("group", "draw")
    serializer_class = EqubRoundSerializer
    permission_classes = [permissions.AllowAny]
    filterset_fields = ["group__slug", "status"]

    @extend_schema(request=PayRequestSerializer, responses=PaymentTransactionSerializer)
    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAuthenticated])
    def contribute(self, request, pk=None):
        s = PayRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        txn = services.start_contribution_payment(request.user, self.get_object(), s.validated_data.get("provider"),
                                                  s.validated_data.get("payer_phone", ""))
        return Response(PaymentTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAdminUser])
    def draw(self, request, pk=None):
        payouts = services.run_draw(self.get_object(), executed_by=request.user)
        return Response({"winners": EqubPayoutSerializer(payouts, many=True).data,
                         "verification": services.verify_draw(self.get_object())})

    @action(detail=True, methods=["get"])
    def verify(self, request, pk=None):
        return Response(services.verify_draw(self.get_object()))

    @action(detail=True, methods=["get"])
    def pool(self, request, pk=None):
        """Who can win this round: counts and odds for everyone, your own position (with reasons) if you are a member."""
        rnd = self.get_object()
        user = request.user
        draw = getattr(rnd, "draw", None)
        if draw and draw.is_executed:
            snap = draw.pool_snapshot or {}
            data = {"round": rnd.number, "executed": True, "eligible_count": len(draw.eligible_memberships),
                    "excluded_count": snap.get("excluded_count"), "odds_percent": snap.get("odds_percent"), "me": None}
            if user.is_authenticated:
                mine = rnd.group.memberships.filter(user=user, dependent_name="").first()
                if mine:
                    excluded = {e["membership_id"]: e for e in snap.get("excluded", [])}
                    data["me"] = {"eligible": mine.id in draw.eligible_memberships,
                                  "reasons": excluded.get(mine.id, {}).get("reasons", [])}
            return Response(data)
        pool = services.draw_pool(rnd)
        me = None
        if user.is_authenticated:
            entry = next((e for e in pool["entries"] if e["_m"].user_id == user.id), None)
            if entry:
                me = {"eligible": entry["eligible"], "reasons": entry["reasons"], "odds_percent": entry["odds"]}
        return Response({"round": rnd.number, "executed": False, "eligible_count": pool["eligible_count"],
                         "excluded_count": pool["excluded_count"], "waiting_count": pool["waiting_count"],
                         "winners_to_draw": pool["winners_to_draw"], "odds_percent": pool["odds_percent"], "me": me})


def _payment_history(m: EqubMembership) -> list[dict]:
    """App payments, office payments and (older) single-round payments, newest first."""
    rows = EqubPaymentSerializer(m.payments.exclude(status=EqubPayment.Status.CANCELLED).select_related("membership__group")[:50],
                                 many=True).data
    ct = ContentType.objects.get_for_model(Contribution)
    legacy = {c.payment_id: c for c in Contribution.objects.filter(membership=m, payment__isnull=False,
                                                                   payment_record__isnull=True).select_related("round", "payment")}
    for c in legacy.values():
        t = c.payment
        if t.content_type_id != ct.id:
            continue
        rows.append({"id": None, "reference": str(t.reference)[:8].upper(), "group": m.group.name, "amount": str(t.amount),
                     "rounds_requested": 1, "method": t.provider, "method_display": t.get_provider_display(),
                     "status": "PAID", "status_display": "Paid", "external_reference": "", "is_office": t.is_office,
                     "paid_at": t.completed_at.isoformat() if t.completed_at else None, "created_at": t.created_at.isoformat(),
                     "rounds": [c.round.number], "credit_after": None, "note": ""})
    rows.sort(key=lambda r: r.get("paid_at") or r.get("created_at") or "", reverse=True)
    return rows


def membership_detail(m: EqubMembership, request) -> dict:
    rules = EqubSettings.load()
    st = standing_mod.standing_for(m, rules)
    g = m.group
    payout = EqubPayout.objects.filter(membership=m).select_related("round__group", "booking").first()
    current = g.rounds.select_related("draw").filter(status=EqubRound.Status.COLLECTING).first()
    current_data = None
    if current:
        pool = services.draw_pool(current, rules)
        entry = next((e for e in pool["entries"] if e["membership_id"] == m.id), None)
        current_data = {**EqubRoundSerializer(current).data, "eligible_count": pool["eligible_count"],
                        "odds_percent": pool["odds_percent"],
                        "me": {"eligible": entry["eligible"], "reasons": entry["reasons"]} if entry else None}
    group_data = EqubGroupListSerializer(g).data
    group_data.update({"late_fee": str(g.late_fee), "grace_days": g.grace_days, "service_fee_percent": str(g.service_fee_percent),
                       "require_paid_to_draw": g.require_paid_to_draw, "payout_basis": g.payout_basis,
                       "package_name": g.equb_package.name if g.equb_package_id else None, "join_code": g.join_code,
                       "winners_per_round": g.winners_per_round})
    return {
        "id": m.id, "position": m.position, "status": m.status, "status_display": m.get_status_display(),
        "has_won": m.has_won, "won_round": st.won_round, "total_paid": str(m.total_paid), "created_at": m.created_at,
        "group": group_data, "standing": st.as_dict(), "schedule": standing_mod.schedule_for(m),
        "payments": _payment_history(m), "exit": operations.exit_info(m, st),
        "payout": EqubPayoutSerializer(payout).data if payout else None, "current_round": current_data,
        "rules": {"warn_at": rules.warn_at, "block_at": rules.block_at, "suspend_at": rules.suspend_at,
                  "min_paid_rounds_to_win": rules.min_paid_rounds_to_win, "max_advance_rounds": rules.max_advance_rounds},
        "rules_summary": rules_summary(g, rules),
    }


class MembershipViewSet(viewsets.ReadOnlyModelViewSet):
    """My Equb places: standing, schedule, payments, exit requests."""

    serializer_class = MembershipSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return EqubMembership.objects.none()
        return (EqubMembership.objects.filter(user=self.request.user).exclude(status=EqubMembership.Status.LEFT)
                .select_related("group", "won_round").order_by("-created_at"))

    def list(self, request, *args, **kwargs):
        qs = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(qs)
        items = list(page if page is not None else qs)
        ctx = {**self.get_serializer_context(), "standings": standing_mod.standings_for(items)}
        data = MembershipSerializer(items, many=True, context=ctx).data
        return self.get_paginated_response(data) if page is not None else Response(data)

    def retrieve(self, request, *args, **kwargs):
        return Response(membership_detail(self.get_object(), request))

    @action(detail=False, methods=["get"])
    def contributions(self, request):
        qs = (Contribution.objects.filter(membership__user=request.user).select_related("round__group", "payment_record")
              .order_by("-round__due_date"))
        page = self.paginate_queryset(qs)
        return self.get_paginated_response(ContributionSerializer(page, many=True).data)

    @action(detail=True, methods=["get"], url_path="pay-options")
    def pay_options(self, request, pk=None):
        return Response(services.payment_plan(self.get_object()))

    @extend_schema(request=MemberPaySerializer, responses=PaymentTransactionSerializer)
    @action(detail=True, methods=["post"])
    def pay(self, request, pk=None):
        s = MemberPaySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        txn = services.start_member_payment(request.user, self.get_object(), d["rounds"], d.get("provider"),
                                            d.get("payer_phone", ""))
        return Response(PaymentTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def payments(self, request, pk=None):
        return Response(_payment_history(self.get_object()))

    @extend_schema(request=ExitRequestSerializer)
    @action(detail=True, methods=["post"], url_path="request-exit")
    def request_exit(self, request, pk=None):
        s = ExitRequestSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        m = operations.request_exit(request.user, self.get_object(), s.validated_data["reason"])
        return Response(membership_detail(m, request))

    @action(detail=True, methods=["post"], url_path="cancel-exit")
    def cancel_exit(self, request, pk=None):
        m = operations.cancel_exit_request(request.user, self.get_object())
        return Response(membership_detail(m, request))


class PayoutViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = EqubPayoutSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return EqubPayout.objects.none()
        return EqubPayout.objects.filter(membership__user=self.request.user).select_related("round__group", "booking")

    @extend_schema(request=RedeemSerializer)
    @action(detail=True, methods=["post"])
    def redeem(self, request, pk=None):
        s = RedeemSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        booking = get_object_or_404(Booking, reference=s.validated_data["booking_reference"].upper(), user=request.user)
        payout = services.redeem_payout(self.get_object(), booking)
        return Response(EqubPayoutSerializer(payout).data)


class GroupEqubViewSet(viewsets.GenericViewSet):
    """
    Group Equb – a family, friends, a company team or a community saving in one Equb together.

    ``GET /equb/group-equbs/`` my groups · ``POST`` create one (it joins an open Equb) · ``GET /{id}/`` members and
    invite code · owner actions: ``add-dependent``, ``remove-member``, ``close``, ``new-code``. Others join with
    ``POST /equb/groups/join-by-code/`` using the group's invite code.
    """

    permission_classes = [permissions.IsAuthenticated]
    serializer_class = GroupEqubCreateSerializer
    pagination_class = None

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return GroupEqub.objects.none()
        user = self.request.user
        mine = EqubMembership.objects.filter(user=user).exclude(status=EqubMembership.Status.LEFT).values("team_id")
        return (GroupEqub.objects.filter(is_active=True).filter(Q(owner=user) | Q(pk__in=mine))
                .select_related("equb", "owner", "equb__equb_package").distinct().order_by("-created_at"))

    def _detail(self, team, request, code=status.HTTP_200_OK):
        team.refresh_from_db()
        data = teams.group_info(team, request.user)
        data["equb"] = EqubGroupDetailSerializer(team.equb, context={"request": request}).data
        return Response(data, status=code)

    def list(self, request):
        out = []
        for t in self.get_queryset():
            info = teams.group_info(t, request.user)
            info.pop("members")
            info["equb"] = EqubGroupListSerializer(t.equb, context={"request": request}).data
            out.append(info)
        return Response(out)

    def create(self, request):
        s = GroupEqubCreateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        team = teams.create_group_equb(request.user, d["equb"], name=d["name"], kind=d["kind"],
                                       description=d["description"], dependents=d["dependents"],
                                       invite_phones=d["invite_phones"])
        return self._detail(team, request, status.HTTP_201_CREATED)

    def retrieve(self, request, pk=None):
        return self._detail(self.get_object(), request)

    @extend_schema(request=DependentSerializer)
    @action(detail=True, methods=["post"], url_path="add-dependent")
    def add_dependent(self, request, pk=None):
        s = DependentSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        team = self.get_object()
        teams.add_dependent(request.user, team, **s.validated_data)
        return self._detail(team, request)

    @extend_schema(request=RemoveMemberSerializer)
    @action(detail=True, methods=["post"], url_path="remove-member")
    def remove_member(self, request, pk=None):
        s = RemoveMemberSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        team = self.get_object()
        teams.remove_member(request.user, team, s.validated_data["membership_id"])
        return self._detail(team, request)

    @action(detail=True, methods=["post"])
    def close(self, request, pk=None):
        teams.close_group(request.user, self.get_object())
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=["post"], url_path="new-code")
    def new_code(self, request, pk=None):
        return self._detail(teams.new_invite_code(request.user, self.get_object()), request)

    @action(detail=False, methods=["get"], url_path="lookup-phone")
    def lookup_phone(self, request):
        """Is this full phone number a Rihla customer? ``?phone=+251912345678`` (``&team=<id>`` to check that group)."""
        team = None
        if request.query_params.get("team"):
            team = self.get_queryset().filter(pk=request.query_params["team"]).first()
        return Response(teams.lookup_phone(request.user, request.query_params.get("phone", ""), team))

    @extend_schema(request=InvitePhoneSerializer)
    @action(detail=True, methods=["post"])
    def invite(self, request, pk=None):
        """Ask a customer to join. They must accept; then they pay for their own place."""
        s = InvitePhoneSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        team = self.get_object()
        teams.invite(request.user, team, s.validated_data["phone"])
        return self._detail(team, request)

    @extend_schema(request=InviteIdSerializer)
    @action(detail=True, methods=["post"], url_path="cancel-invite")
    def cancel_invite(self, request, pk=None):
        s = InviteIdSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        team = self.get_object()
        teams.cancel_invite(request.user, team, s.validated_data["invite_id"])
        return self._detail(team, request)


class GroupInviteViewSet(viewsets.GenericViewSet):
    """Invitations to Group Equbs that I received: ``GET`` list, ``POST /{id}/accept/``, ``POST /{id}/decline/``."""

    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None
    serializer_class = InviteIdSerializer

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return GroupEqubInvite.objects.none()
        return GroupEqubInvite.objects.filter(user=self.request.user).select_related("team__equb", "team__owner")

    def list(self, request):
        qs = self.get_queryset()
        if request.query_params.get("all") != "1":
            qs = qs.filter(status=GroupEqubInvite.Status.PENDING)
        return Response([teams.invite_info(i) for i in qs[:50]])

    @action(detail=True, methods=["post"])
    def accept(self, request, pk=None):
        inv = teams.respond_invite(request.user, self.get_object(), True)
        out = teams.invite_info(inv)
        out["membership_id"] = inv.membership_id
        return Response(out)

    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        return Response(teams.invite_info(teams.respond_invite(request.user, self.get_object(), False)))
