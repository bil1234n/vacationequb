from rest_framework import generics, permissions, status
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from apps.core.exceptions import BusinessRuleError

from . import services
from .serializers import (DeleteAccountSerializer, PhoneTokenObtainPairSerializer, PushTokenSerializer,
                          RegisterSerializer, UserSerializer)


class RegisterView(generics.CreateAPIView):
    serializer_class = RegisterSerializer
    permission_classes = [permissions.AllowAny]


class LoginView(TokenObtainPairView):
    serializer_class = PhoneTokenObtainPairSerializer


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
    ``POST {"reason": "..."}`` – ask Vacation Equb to delete this account.

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
