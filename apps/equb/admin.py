from django.contrib import admin, messages

from apps.core.exceptions import BusinessRuleError

from . import services
from .models import (CallLog, Contribution, EqubDraw, EqubGroup, EqubMembership, EqubPackage, EqubPayment, EqubPayout, EqubRound,
                     EqubSettings, GroupEqub, Termination)


class MembershipInline(admin.TabularInline):
    model = EqubMembership
    extra = 0
    raw_id_fields = ["user", "won_round", "called_by"]
    readonly_fields = ["total_paid", "missed_count", "prepaid_credit"]
    fields = ["position", "user", "dependent_name", "team", "status", "has_won", "won_round", "total_paid",
              "prepaid_credit", "missed_count", "draw_blocked_until"]
    raw_id_fields = ["user", "won_round", "called_by", "team"]


@admin.register(EqubPackage)
class EqubPackageAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(EqubGroup)
class EqubGroupAdmin(admin.ModelAdmin):
    list_display = ["name", "equb_package", "contribution_amount", "frequency_days", "max_members", "rounds_drawn", "members_count",
                    "status", "draw_type", "current_round", "start_date", "join_code"]
    list_filter = ["equb_package", "status", "frequency", "draw_type"]
    search_fields = ["name", "join_code"]
    prepopulated_fields = {"slug": ("name",)}
    inlines = [MembershipInline]
    actions = ["start_groups"]

    @admin.action(description="Start selected Equbs (generate rounds)")
    def start_groups(self, request, queryset):
        for g in queryset:
            try:
                services.start_group(g)
            except BusinessRuleError as exc:
                self.message_user(request, f"{g}: {exc.detail}", messages.WARNING)


class ContributionInline(admin.TabularInline):
    model = Contribution
    extra = 0
    raw_id_fields = ["membership", "payment", "payment_record"]


@admin.register(EqubRound)
class EqubRoundAdmin(admin.ModelAdmin):
    list_display = ["group", "number", "due_date", "draw_date", "status", "expected_amount", "collected_amount"]
    list_filter = ["status", "group"]
    inlines = [ContributionInline]
    actions = ["draw_winner"]

    @admin.action(description="Run the provably-fair draw")
    def draw_winner(self, request, queryset):
        for r in queryset:
            try:
                payouts = services.run_draw(r, executed_by=request.user)
                names = ", ".join(p.membership.user.full_name for p in payouts)
                self.message_user(request, f"{r}: winner(s) {names}", messages.SUCCESS)
            except BusinessRuleError as exc:
                self.message_user(request, f"{r}: {exc.detail}", messages.ERROR)


@admin.register(EqubDraw)
class EqubDrawAdmin(admin.ModelAdmin):
    list_display = ["round", "seed_commitment", "executed_at", "executed_by"]
    readonly_fields = [f.name for f in EqubDraw._meta.fields]


@admin.register(EqubPayout)
class EqubPayoutAdmin(admin.ModelAdmin):
    list_display = ["voucher_code", "membership", "round", "gross_amount", "fee_amount", "amount", "status", "booking",
                    "redeemed_at", "paid_out_at"]
    list_filter = ["status"]
    search_fields = ["voucher_code", "membership__user__phone"]


@admin.register(EqubPayment)
class EqubPaymentAdmin(admin.ModelAdmin):
    list_display = ["reference", "membership", "amount", "method", "status", "paid_at", "recorded_by"]
    list_filter = ["status", "method"]
    search_fields = ["reference", "external_reference", "membership__user__phone", "membership__user__full_name"]
    readonly_fields = ["reference", "allocation", "transaction", "paid_at"]
    raw_id_fields = ["membership", "user", "recorded_by"]


@admin.register(CallLog)
class CallLogAdmin(admin.ModelAdmin):
    list_display = ["membership", "outcome", "promised_date", "called_by", "created_at"]
    list_filter = ["outcome"]
    search_fields = ["membership__user__full_name", "membership__user__phone", "feedback"]
    raw_id_fields = ["membership", "called_by"]


@admin.register(Termination)
class TerminationAdmin(admin.ModelAdmin):
    list_display = ["membership", "reason", "arrears_at_termination", "terminated_by", "created_at", "reinstated_at"]
    list_filter = ["reason"]
    raw_id_fields = ["membership", "terminated_by", "reinstated_by"]


@admin.register(EqubSettings)
class EqubSettingsAdmin(admin.ModelAdmin):
    list_display = ["__str__", "warn_at", "block_at", "suspend_at", "reminder_days_before", "updated_at"]

    def has_add_permission(self, request):
        return not EqubSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(GroupEqub)
class GroupEqubAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "equb", "owner", "invite_code", "is_active", "created_at"]
    list_filter = ["kind", "is_active"]
    search_fields = ["name", "invite_code", "owner__full_name", "owner__phone"]
    raw_id_fields = ["equb", "owner", "created_by"]
