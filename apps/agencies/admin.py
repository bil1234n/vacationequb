from django.contrib import admin

from .models import Agency, AgencyCustomer, AgencyLedgerEntry, AgencyStaff, FamilyMember


class StaffInline(admin.TabularInline):
    model = AgencyStaff
    extra = 0
    raw_id_fields = ["user"]


@admin.register(Agency)
class AgencyAdmin(admin.ModelAdmin):
    list_display = ["name", "phone", "status", "commission_percent", "chapa_subaccount_id", "created_at"]
    list_filter = ["status"]
    search_fields = ["name", "phone", "license_number"]
    inlines = [StaffInline]


@admin.register(AgencyCustomer)
class AgencyCustomerAdmin(admin.ModelAdmin):
    list_display = ["user", "agency", "created_at"]
    list_filter = ["agency"]
    search_fields = ["user__full_name", "user__phone"]
    raw_id_fields = ["user", "added_by"]


@admin.register(FamilyMember)
class FamilyMemberAdmin(admin.ModelAdmin):
    list_display = ["first_name", "last_name", "relation", "customer"]
    search_fields = ["first_name", "last_name"]


@admin.register(AgencyLedgerEntry)
class AgencyLedgerEntryAdmin(admin.ModelAdmin):
    list_display = ["created_at", "agency", "kind", "gross_amount", "commission", "agency_amount", "payout_status"]
    list_filter = ["agency", "kind", "payout_status"]
    raw_id_fields = ["booking", "payment", "customer", "paid_by"]
