from django.db.models import Sum
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle
from rest_framework.views import APIView

from apps.core.permissions import IsStaffOrReadOnly
from apps.payments.serializers import PaymentTransactionSerializer

from . import services
from .models import CrowdfundingCampaign, Donation, Giveaway, GiveawayWinner
from .serializers import (ClaimSerializer, CrowdfundingCampaignSerializer, DisbursementSerializer, DonateSerializer,
                          DonationSerializer, GiveawaySerializer, MyWinSerializer, WinnerSerializer)


class DonationThrottle(UserRateThrottle):
    scope = "donations"


class CrowdfundingViewSet(viewsets.ReadOnlyModelViewSet):
    """Crowdfunding Hub (donations). The gift goes 100% to the cause; the platform fee is added on top."""

    serializer_class = CrowdfundingCampaignSerializer
    permission_classes = [IsStaffOrReadOnly]
    lookup_field = "slug"
    filterset_fields = ["status", "category", "is_featured"]

    def get_queryset(self):
        qs = CrowdfundingCampaign.objects.exclude(status__in=["DRAFT", "CANCELLED"])
        if self.request.query_params.get("live"):
            qs = qs.live()
        return qs

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def quote(self, request, slug=None):
        """``?amount=100`` → the gift, the platform fee and the total to pay."""
        try:
            amount = int(request.query_params.get("amount", "0"))
        except ValueError:
            amount = 0
        q = services.quote(self.get_object(), max(amount, 0))
        return Response({k: str(v) for k, v in q.items()})

    @extend_schema(request=DonateSerializer, responses=PaymentTransactionSerializer)
    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAuthenticated], throttle_classes=[DonationThrottle])
    def donate(self, request, slug=None):
        s = DonateSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        donation, txn = services.start_donation(request.user, self.get_object(), d["amount"], kind=d["kind"],
                                                provider=d.get("provider"), payer_phone=d.get("payer_phone", ""),
                                                is_anonymous=d["is_anonymous"], message=d["message"])
        return Response({"donation": DonationSerializer(donation).data, "payment": PaymentTransactionSerializer(txn).data},
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def donors(self, request, slug=None):
        c = self.get_object()
        return Response({"donors": services.recent_donors(c, 30),
                         "disbursements": DisbursementSerializer(c.disbursements.all()[:20], many=True).data})


class MyDonationsViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    serializer_class = DonationSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return Donation.objects.none()
        return Donation.objects.filter(user=self.request.user).exclude(status="FAILED").select_related("campaign")


class GiveawayViewSet(viewsets.ReadOnlyModelViewSet):
    """Community Giveaways – free, sponsored trips. Invite friends: every 10 who register = 1 free entry."""

    serializer_class = GiveawaySerializer
    permission_classes = [IsStaffOrReadOnly]
    lookup_field = "slug"
    filterset_fields = ["status", "prize_type", "is_featured"]

    def get_queryset(self):
        qs = Giveaway.objects.exclude(status="CANCELLED")
        if self.request.query_params.get("live"):
            qs = qs.live()
        return qs

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def leaderboard(self, request, slug=None):
        return Response(services.leaderboard(self.get_object()))

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def winners(self, request, slug=None):
        return Response(WinnerSerializer(self.get_object().winners.select_related("user"), many=True).data)

    @action(detail=True, methods=["get"], permission_classes=[permissions.AllowAny])
    def verify(self, request, slug=None):
        return Response(services.verify(self.get_object()))

    @action(detail=True, methods=["post"], permission_classes=[permissions.IsAdminUser])
    def draw(self, request, slug=None):
        g = self.get_object()
        if g.status == Giveaway.Status.LIVE and request.data.get("force_close"):
            g.status = Giveaway.Status.CLOSED
            g.save(update_fields=["status"])
        winners = services.close_and_draw(g)
        return Response({"winners": WinnerSerializer(winners, many=True).data, "verification": services.verify(self.get_object())})


class MyWinsViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    serializer_class = MyWinSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return GiveawayWinner.objects.none()
        return GiveawayWinner.objects.filter(user=self.request.user).select_related("giveaway")

    @extend_schema(request=ClaimSerializer)
    @action(detail=True, methods=["post"])
    def claim(self, request, pk=None):
        s = ClaimSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        w = services.claim_prize(self.get_object(), booking_reference=s.validated_data.get("booking_reference"))
        return Response(MyWinSerializer(w, context={"request": request}).data)


class CommunitySummaryView(APIView):
    """The Community tab header: my giving, my referrals and my code."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        u = request.user
        paid = Donation.objects.filter(user=u, status=Donation.Status.PAID)
        return Response({
            "referral_code": u.referral_code, "friends_invited": u.referrals.filter(is_active=True).count(),
            "given_total": str(paid.aggregate(s=Sum("amount"))["s"] or 0), "campaigns_supported": paid.values("campaign").distinct().count(),
            "live_entries": sum(p["entries"] for p in u.giveaway_progress.filter(giveaway__status="LIVE").values("entries")),
            "wins": GiveawayWinner.objects.filter(user=u).count(),
            "live_campaigns": CrowdfundingCampaign.objects.live().count(), "live_giveaways": Giveaway.objects.live().count(),
        })


