from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import (AirportViewSet, BaggageOptionViewSet, BookingViewSet, FlightViewSet, TicketVerifyView,
                    TravelPackageViewSet)

router = DefaultRouter()
router.register("airports", AirportViewSet, basename="airport")
router.register("flights", FlightViewSet, basename="flight")
router.register("baggage-options", BaggageOptionViewSet, basename="baggage")
router.register("packages", TravelPackageViewSet, basename="package")
router.register("bookings", BookingViewSet, basename="booking")

urlpatterns = [path("tickets/verify/", TicketVerifyView.as_view(), name="ticket-verify")] + router.urls
