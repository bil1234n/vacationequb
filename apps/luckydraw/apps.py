from django.apps import AppConfig


class LuckyDrawConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.luckydraw"
    label = "luckydraw"
    verbose_name = "Community Finance & Impact"

    def ready(self):
        from apps.payments import registry

        from .services import on_donation_failed, on_donation_paid

        registry.register("DONATION", on_success=on_donation_paid, on_failure=on_donation_failed)
