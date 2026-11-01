from rest_framework import serializers

from .models import CrowdfundingCampaign, Disbursement, Donation, Giveaway, GiveawayWinner


class CrowdfundingCampaignSerializer(serializers.ModelSerializer):
    is_live = serializers.BooleanField(read_only=True)
    progress_percent = serializers.FloatField(read_only=True)
    remaining = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    category_display = serializers.CharField(source="get_category_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    my_total = serializers.SerializerMethodField()

    class Meta:
        model = CrowdfundingCampaign
        fields = ["id", "title", "slug", "category", "category_display", "story", "image_url", "beneficiary_name",
                  "goal_amount", "raised_amount", "remaining", "progress_percent", "disbursed_amount", "donors_count",
                  "donations_count", "platform_fee_percent", "min_donation", "starts_at", "ends_at", "status",
                  "status_display", "is_live", "is_featured", "my_total"]

    def get_my_total(self, obj) -> str | None:
        req = self.context.get("request")
        if not req or not req.user.is_authenticated:
            return None
        s = obj.donations.filter(user=req.user, status=Donation.Status.PAID).values_list("amount", flat=True)
        return str(sum(s)) if s else "0"


class DonateSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)
    kind = serializers.ChoiceField(choices=Donation.Kind.choices, default=Donation.Kind.SADAQAH)
    provider = serializers.ChoiceField(choices=["telebirr", "chapa", "mock"], required=False)
    payer_phone = serializers.CharField(required=False, allow_blank=True, max_length=16)
    is_anonymous = serializers.BooleanField(default=False)
    message = serializers.CharField(required=False, allow_blank=True, max_length=200, default="")


class DonationSerializer(serializers.ModelSerializer):
    campaign = serializers.CharField(source="campaign.slug", read_only=True)
    campaign_title = serializers.CharField(source="campaign.title", read_only=True)
    kind_display = serializers.CharField(source="get_kind_display", read_only=True)

    class Meta:
        model = Donation
        fields = ["id", "campaign", "campaign_title", "kind", "kind_display", "amount", "fee_amount", "total_amount",
                  "status", "is_anonymous", "message", "paid_at", "created_at"]


class DisbursementSerializer(serializers.ModelSerializer):
    class Meta:
        model = Disbursement
        fields = ["id", "amount", "paid_to", "method", "paid_on"]


class GiveawaySerializer(serializers.ModelSerializer):
    is_live = serializers.BooleanField(read_only=True)
    prize_type_display = serializers.CharField(source="get_prize_type_display", read_only=True)
    sponsor_type_display = serializers.CharField(source="get_sponsor_type_display", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    my_progress = serializers.SerializerMethodField()

    class Meta:
        model = Giveaway
        fields = ["id", "title", "slug", "description", "image_url", "sponsor_type", "sponsor_type_display", "sponsor_name",
                  "sponsor_note", "sponsor_logo_url", "prize_type", "prize_type_display", "prize_title", "prize_value",
                  "prize_package", "number_of_winners", "referrals_per_entry", "max_entries_per_user", "starts_at",
                  "ends_at", "draw_at", "status", "status_display", "is_live", "is_featured", "entries_issued",
                  "participants_count", "referrals_count", "seed_commitment", "my_progress"]

    def get_my_progress(self, obj) -> dict | None:
        req = self.context.get("request")
        if not req or not req.user.is_authenticated:
            return None
        from .services import progress

        return progress(obj, req.user)


class WinnerSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()

    class Meta:
        model = GiveawayWinner
        fields = ["rank", "name", "ticket_number", "prize_value", "created_at"]

    def get_name(self, obj) -> str:
        from .services import mask

        return mask(obj.user.full_name)


class MyWinSerializer(serializers.ModelSerializer):
    giveaway = GiveawaySerializer(read_only=True)

    class Meta:
        model = GiveawayWinner
        fields = ["id", "giveaway", "rank", "ticket_number", "prize_value", "voucher_code", "claimed_at"]


class ClaimSerializer(serializers.Serializer):
    booking_reference = serializers.CharField(required=False, allow_blank=True, max_length=6)
