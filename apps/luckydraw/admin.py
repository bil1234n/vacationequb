from django.contrib import admin

from .models import CrowdfundingCampaign, Disbursement, Donation, Giveaway, GiveawayParticipant, GiveawayWinner


@admin.register(CrowdfundingCampaign)
class CrowdfundingCampaignAdmin(admin.ModelAdmin):
    list_display = ["title", "category", "status", "goal_amount", "raised_amount", "disbursed_amount", "donors_count"]
    list_filter = ["status", "category"]
    search_fields = ["title", "beneficiary_name"]
    readonly_fields = ["raised_amount", "fees_collected", "disbursed_amount", "donations_count", "donors_count"]


@admin.register(Donation)
class DonationAdmin(admin.ModelAdmin):
    list_display = ["campaign", "user", "kind", "amount", "fee_amount", "total_amount", "status", "paid_at"]
    list_filter = ["status", "kind"]
    search_fields = ["user__phone", "user__full_name", "campaign__title"]


admin.site.register(Disbursement)


@admin.register(Giveaway)
class GiveawayAdmin(admin.ModelAdmin):
    list_display = ["title", "sponsor_name", "status", "referrals_per_entry", "entries_issued", "participants_count", "draw_at"]
    list_filter = ["status", "sponsor_type"]
    readonly_fields = ["entries_issued", "participants_count", "referrals_count", "seed_commitment", "server_seed",
                       "public_input", "drawn_at"]


admin.site.register(GiveawayParticipant)
admin.site.register(GiveawayWinner)
