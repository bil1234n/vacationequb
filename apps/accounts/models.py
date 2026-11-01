import re

from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from apps.core.utils import generate_code

ETH_PHONE_RE = re.compile(r"^\+2519\d{8}$|^\+2517\d{8}$")


def normalize_phone(raw: str) -> str:
    """Normalise Ethiopian numbers to E.164 (+2519XXXXXXXX)."""
    digits = re.sub(r"[^\d+]", "", raw or "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    if digits.startswith("0") and len(digits) == 10:
        digits = "+251" + digits[1:]
    elif digits.startswith("251"):
        digits = "+" + digits
    elif len(digits) == 9 and digits[0] in "79":
        digits = "+251" + digits
    return digits


def validate_phone(value: str):
    if not ETH_PHONE_RE.match(value):
        raise ValidationError("Enter a valid Ethiopian mobile number, e.g. +251911234567.")


class UserManager(BaseUserManager):
    use_in_migrations = True

    def _create_user(self, phone, password, **extra):
        if not phone:
            raise ValueError("Phone number is required")
        phone = normalize_phone(phone)
        user = self.model(phone=phone, **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, phone, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(phone, password, **extra)

    def create_superuser(self, phone, password=None, **extra):
        extra.update(is_staff=True, is_superuser=True)
        return self._create_user(phone, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    class Language(models.TextChoices):
        AMHARIC = "am", "Amharic"
        ENGLISH = "en", "English"
        OROMO = "om", "Afaan Oromo"
        ARABIC = "ar", "Arabic"

    phone = models.CharField(max_length=16, unique=True, validators=[validate_phone])
    email = models.EmailField(blank=True)
    full_name = models.CharField(max_length=150)
    preferred_language = models.CharField(max_length=2, choices=Language.choices, default=Language.ENGLISH)

    # Travel documents (used to pre-fill passenger forms)
    national_id = models.CharField(max_length=32, blank=True)
    passport_number = models.CharField(max_length=32, blank=True)
    passport_expiry = models.DateField(null=True, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)

    # Viral acquisition
    referral_code = models.CharField(max_length=8, unique=True, editable=False)
    referred_by = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="referrals")

    push_token = models.CharField(max_length=255, blank=True)
    is_phone_verified = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    date_joined = models.DateTimeField(default=timezone.now)

    objects = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        ordering = ["-date_joined"]

    def __str__(self):
        return f"{self.full_name} ({self.phone})"

    def save(self, *args, **kwargs):
        self.phone = normalize_phone(self.phone)
        if not self.referral_code:
            code = generate_code(7)
            while User.objects.filter(referral_code=code).exists():
                code = generate_code(7)
            self.referral_code = code
        super().save(*args, **kwargs)

    @property
    def first_name(self):
        return self.full_name.split(" ")[0] if self.full_name else ""
