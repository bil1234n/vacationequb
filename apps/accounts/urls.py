from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import CheckPhoneView, DeleteAccountView, LoginView, MeView, PushTokenView, RegisterView, SetPinView

urlpatterns = [
    path("register/", RegisterView.as_view(), name="register"),
    path("check-phone/", CheckPhoneView.as_view(), name="check-phone"),
    path("login/", LoginView.as_view(), name="login"),
    path("set-pin/", SetPinView.as_view(), name="set-pin"),
    path("refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", MeView.as_view(), name="me"),
    path("push-token/", PushTokenView.as_view(), name="push-token"),
    path("delete-account/", DeleteAccountView.as_view(), name="delete-account"),
]
