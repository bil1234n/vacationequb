from django.contrib.auth import password_validation
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken

from .models import User, normalize_phone, validate_phone


class UserSerializer(serializers.ModelSerializer):
    referrals_count = serializers.IntegerField(source="referrals.count", read_only=True)

    class Meta:
        model = User
        fields = [
            "id", "phone", "email", "full_name", "preferred_language", "national_id",
            "passport_number", "passport_expiry", "date_of_birth", "referral_code",
            "referrals_count", "is_phone_verified", "date_joined",
        ]
        read_only_fields = ["phone", "referral_code", "is_phone_verified", "date_joined"]


class RegisterSerializer(serializers.ModelSerializer):
    phone = serializers.CharField(max_length=20)  # normalised + validated in validate_phone
    password = serializers.CharField(write_only=True, min_length=6)
    referral_code = serializers.CharField(write_only=True, required=False, allow_blank=True)
    tokens = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ["phone", "full_name", "email", "password", "preferred_language", "referral_code", "tokens"]

    def validate_phone(self, value):
        value = normalize_phone(value)
        validate_phone(value)
        if User.objects.filter(phone=value).exists():
            raise serializers.ValidationError("An account with this phone number already exists.")
        return value

    def validate_password(self, value):
        password_validation.validate_password(value)
        return value

    def validate_referral_code(self, value):
        if not value:
            return None
        referrer = User.objects.filter(referral_code=value.strip().upper()).first()
        if not referrer:
            raise serializers.ValidationError("Invalid referral code.")
        return referrer

    def create(self, validated_data):
        from apps.luckydraw.services import on_referral_signup

        referrer = validated_data.pop("referral_code", None)
        password = validated_data.pop("password")
        user = User.objects.create_user(password=password, referred_by=referrer, **validated_data)
        if referrer:
            on_referral_signup(referrer=referrer, new_user=user)
        return user

    def get_tokens(self, user) -> dict:
        refresh = RefreshToken.for_user(user)
        return {"refresh": str(refresh), "access": str(refresh.access_token)}


class PhoneTokenObtainPairSerializer(TokenObtainPairSerializer):
    username_field = User.USERNAME_FIELD

    def validate(self, attrs):
        attrs[self.username_field] = normalize_phone(attrs.get(self.username_field, ""))
        data = super().validate(attrs)
        data["user"] = UserSerializer(self.user).data
        return data


class PushTokenSerializer(serializers.Serializer):
    push_token = serializers.CharField(max_length=255)
