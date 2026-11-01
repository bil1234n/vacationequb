from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    ordering = ["-date_joined"]
    list_display = ["phone", "full_name", "email", "referral_code", "is_phone_verified", "is_staff", "date_joined"]
    search_fields = ["phone", "full_name", "email", "referral_code", "passport_number"]
    list_filter = ["is_staff", "is_active", "is_phone_verified", "preferred_language"]
    readonly_fields = ["referral_code", "date_joined", "last_login"]
    fieldsets = (
        (None, {"fields": ("phone", "password")}),
        ("Profile", {"fields": ("full_name", "email", "preferred_language", "date_of_birth")}),
        ("Travel documents", {"fields": ("national_id", "passport_number", "passport_expiry")}),
        ("Growth", {"fields": ("referral_code", "referred_by", "push_token")}),
        ("Permissions", {"fields": ("is_active", "is_phone_verified", "is_staff", "is_superuser", "groups", "user_permissions")}),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = ((None, {"classes": ("wide",), "fields": ("phone", "full_name", "password1", "password2")}),)
    raw_id_fields = ["referred_by"]
