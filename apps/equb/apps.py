from django.apps import AppConfig


class EqubConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.equb"
    verbose_name = "Travel Equb"

    def ready(self):
        from apps.payments import registry

        from .services import on_contribution_failed, on_contribution_paid

        registry.register("EQUB_CONTRIBUTION", on_success=on_contribution_paid, on_failure=on_contribution_failed)
