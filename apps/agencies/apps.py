from django.apps import AppConfig


class AgenciesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.agencies"
    verbose_name = "Travel agencies"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.travel.models import Booking

        from .services import on_booking_saved

        post_save.connect(on_booking_saved, sender=Booking, dispatch_uid="agency_credit_sale")
