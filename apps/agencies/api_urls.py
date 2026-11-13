from django.urls import path

from . import api

urlpatterns = [
    path("dashboard/", api.DashboardView.as_view(), name="agency-api-dashboard"),
    path("customers/", api.CustomersView.as_view(), name="agency-api-customers"),
    path("customers/<int:pk>/", api.CustomerDetailView.as_view(), name="agency-api-customer"),
    path("customers/<int:pk>/family/", api.FamilyView.as_view(), name="agency-api-family"),
    path("customers/<int:pk>/enrol/", api.EnrolView.as_view(), name="agency-api-enrol"),
    path("equbs/", api.EqubsView.as_view(), name="agency-api-equbs"),
    path("places/<int:membership_id>/", api.PlaceView.as_view(), name="agency-api-place"),
    path("places/<int:membership_id>/pay/", api.PlacePayView.as_view(), name="agency-api-place-pay"),
    path("places/<int:membership_id>/checkout/", api.PlaceCheckoutView.as_view(), name="agency-api-place-checkout"),
    path("bookings/", api.BookingsView.as_view(), name="agency-api-bookings"),
    path("bookings/<str:reference>/checkout/", api.BookingCheckoutView.as_view(), name="agency-api-booking-checkout"),
    path("earnings/", api.EarningsView.as_view(), name="agency-api-earnings"),
    path("team/", api.TeamView.as_view(), name="agency-api-team"),
]
