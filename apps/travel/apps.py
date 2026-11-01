from django.apps import AppConfig


class TravelConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.travel"
    verbose_name = "Flights & Travel"

    def ready(self):
        from apps.payments import registry

        from .services.bookings import on_booking_paid, on_booking_payment_failed

        registry.register("BOOKING", on_success=on_booking_paid, on_failure=on_booking_payment_failed)
