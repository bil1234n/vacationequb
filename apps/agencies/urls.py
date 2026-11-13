from django.urls import path

from . import views

app_name = "agency"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("logout/", views.logout_view, name="logout"),
    path("customers/", views.customers, name="customers"),
    path("customers/new/", views.customer_add, name="customer_add"),
    path("customers/<int:pk>/", views.customer_detail, name="customer"),
    path("customers/<int:pk>/family/new/", views.family_form, name="family_add"),
    path("customers/<int:pk>/family/<int:member>/", views.family_form, name="family_edit"),
    path("equb/", views.equbs, name="equbs"),
    path("equb/<slug:slug>/enrol/", views.equb_enrol, name="equb_enrol"),
    path("equb/places/<int:pk>/", views.place, name="place"),
    path("book/", views.book, name="book"),
    path("book/flight/<int:fare_id>/", views.book_flight, name="book_flight"),
    path("book/package/<int:pk>/", views.book_package, name="book_package"),
    path("bookings/", views.bookings, name="bookings"),
    path("bookings/<str:reference>/", views.booking_detail, name="booking"),
    path("earnings/", views.earnings, name="earnings"),
    path("team/", views.team, name="team"),
    path("profile/", views.profile, name="profile"),
]
