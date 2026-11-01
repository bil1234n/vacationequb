from django.contrib import admin

from .models import PaymentTransaction, WebhookEvent


@admin.register(PaymentTransaction)
class PaymentTransactionAdmin(admin.ModelAdmin):
    list_display = ["reference", "user", "provider", "purpose", "amount", "status", "fulfilled", "created_at"]
    list_filter = ["provider", "purpose", "status", "fulfilled"]
    search_fields = ["reference", "provider_reference", "user__phone", "payer_phone"]
    readonly_fields = [f.name for f in PaymentTransaction._meta.fields]
    date_hierarchy = "created_at"


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = ["provider", "signature_valid", "transaction", "created_at"]
    list_filter = ["provider", "signature_valid"]
    readonly_fields = [f.name for f in WebhookEvent._meta.fields]
