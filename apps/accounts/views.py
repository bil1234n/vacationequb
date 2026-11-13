from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.core.exceptions import BusinessRuleError

from . import services
from .models import User, normalize_phone
from .serializers import (CheckPhoneSerializer, DeleteAccountSerializer, PhoneTokenObtainPairSerializer,
                          PushTokenSerializer, RegisterSerializer, SetPinSerializer, UserSerializer)


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class LoginView(TokenObtainPairView):
    serializer_class = PhoneTokenObtainPairSerializer


class CheckPhoneView(APIView):
    """
    ``POST {"phone": "09…"}`` – step 1 of the telebirr-style sign-in: is there an account for this number, and does
    it already use a 6-digit PIN? (Older accounts sign in once with their password, then set a PIN.)
    """

    permission_classes = [permissions.AllowAny]

    def post(self, request):
        s = CheckPhoneSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        phone = normalize_phone(s.validated_data["phone"])
        u = User.objects.filter(phone=phone, is_active=True).only("full_name", "pin_set").first()
        return Response({"phone": phone, "exists": bool(u), "has_pin": bool(u and u.pin_set),
                         "first_name": u.first_name if u else ""})


class SetPinView(APIView):
    """``POST {"current": "<password or old PIN>", "pin": "123987"}`` – choose a new 6-digit PIN."""

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        s = SetPinSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = request.user
        if not user.check_password(s.validated_data["current"]):
            raise BusinessRuleError("Your current password or PIN is not correct.")
        user.set_password(s.validated_data["pin"])
        user.pin_set = True
        user.save(update_fields=["password", "pin_set"])
        return Response(UserSerializer(user).data)


class MeView(generics.RetrieveUpdateAPIView):
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user


class PushTokenView(APIView):
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        s = PushTokenSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        request.user.push_token = s.validated_data["push_token"]
        request.user.save(update_fields=["push_token"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class DeleteAccountView(APIView):
    """
    ``POST {"reason": "..."}`` – ask EthioVacation to delete this account.

    The account is suspended at once (sign-in stops working) and waits for staff approval in the console.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        if request.user.is_staff:
            raise BusinessRuleError("Staff accounts are closed by an administrator.")
        s = DeleteAccountSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        user = services.request_deletion(request.user, s.validated_data["reason"])
        return Response({"status": user.deletion_status, "requested_at": user.deletion_requested_at},
                        status=status.HTTP_202_ACCEPTED)
