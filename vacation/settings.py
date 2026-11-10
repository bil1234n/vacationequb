"""
Vacation Equb — Django settings.

All secrets and environment-specific values are read from environment variables
(see ``.env.example``). PostgreSQL is the primary database.
"""
from datetime import timedelta
from pathlib import Path
import os
import re

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader so the project runs without extra dependencies."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if value[:1] in {'"', "'"} and value[:1] in value[1:]:
            value = value[1:value.index(value[0], 1)]  # quoted value: keep '#' inside quotes
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()  # drop inline "  # comment"
        os.environ.setdefault(key.strip(), value)


_load_dotenv(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(os.environ.get(key, default)).lower() in {"1", "true", "yes", "on"}


def env_list(key, default=""):
    return [x.strip() for x in os.environ.get(key, default).split(",") if x.strip()]


SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-insecure-change-me")
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = ['.vercel.app', 'localhost', '127.0.0.1', '*']
# Tunnels (ngrok) serve the console over https; trust their domains for form posts.
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "") + [
    "https://*.ngrok-free.app", "https://*.ngrok-free.dev", "https://*.ngrok.app", "https://*.ngrok.io",
]
# ngrok / reverse proxies terminate https and say so in X-Forwarded-Proto.
if env_bool("TRUST_X_FORWARDED_PROTO", True):
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Third party
    "rest_framework",
    "rest_framework_simplejwt",
    "corsheaders",
    "django_filters",
    "drf_spectacular",
    # vacation apps
    "apps.core",
    "apps.accounts",
    "apps.payments",
    "apps.notifications",
    "apps.travel",
    "apps.equb",
    "apps.luckydraw",
    "apps.agencies",
    "apps.console",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware", # WhiteNoise for static files on Vercel
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "apps.core.middleware.RequestBaseURLMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "vacation.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "vacation.wsgi.application"
ASGI_APPLICATION = "vacation.asgi.application"

# --- Database (PostgreSQL / Neon) ------------------------------------------
if env("DATABASE_URL"):
    DATABASES = {
        "default": dj_database_url.parse(
            env("DATABASE_URL"), conn_max_age=int(env("DB_CONN_MAX_AGE", 60)), conn_health_checks=True,
        )
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "vacation_db"),
            "USER": env("DB_USER", "postgres"),
            "PASSWORD": env("DB_PASSWORD", "Bilal1234"),
            "HOST": env("DB_HOST", "localhost"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": int(env("DB_CONN_MAX_AGE", 60)),
            "CONN_HEALTH_CHECKS": True,
        }
    }

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 6}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Africa/Addis_Ababa"
USE_I18N = True
USE_TZ = True

STATIC_URL = 'static/'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')
# Vercel never runs collectstatic, so WhiteNoise must read CSS/JS straight
# from each app's static/ folder (apps/console/static/...).
WHITENOISE_USE_FINDERS = True
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        # Not the "Manifest" storage: that needs collectstatic and crashes every page without it.
        "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
    },
}

# Staff web console
LOGIN_URL = "console:login"
LOGIN_REDIRECT_URL = "console:dashboard"
LOGOUT_REDIRECT_URL = "console:login"

# --- REST framework --------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticatedOrReadOnly",),
    "DEFAULT_FILTER_BACKENDS": (
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.OrderingFilter",
        "rest_framework.filters.SearchFilter",
    ),
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 20,
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_THROTTLE_CLASSES": (
        "rest_framework.throttling.AnonRateThrottle",
        "rest_framework.throttling.UserRateThrottle",
    ),
    "DEFAULT_THROTTLE_RATES": {"anon": "60/min", "user": "240/min", "donations": "20/min"},
    "EXCEPTION_HANDLER": "apps.core.exceptions.vacation_exception_handler",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=int(env("JWT_ACCESS_MINUTES", 60))),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=int(env("JWT_REFRESH_DAYS", 30))),
    "ROTATE_REFRESH_TOKENS": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Vacation Equb API",
    "DESCRIPTION": "Flight & package booking, destination Equb pools, Sadaqah crowdfunding and free referral giveaways.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}

CORS_ALLOW_ALL_ORIGINS = env_bool("CORS_ALLOW_ALL", DEBUG)
CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")
from corsheaders.defaults import default_headers  # noqa: E402

CORS_ALLOW_HEADERS = (*default_headers, "ngrok-skip-browser-warning")

# --- Business rules --------------------------------------------------------
VACATION = {
    "CURRENCY": "ETB",
    "SEAT_LOCK_MINUTES": int(env("SEAT_LOCK_MINUTES", 10)),
    "BOOKING_PAYMENT_WINDOW_MINUTES": int(env("BOOKING_PAYMENT_WINDOW_MINUTES", 30)),
    "MAX_SEATS_PER_LOCK": 9,
    "CROWDFUND_FEE_PERCENT": float(env("CROWDFUND_FEE_PERCENT", 5)),
    "COMPANY_PROFIT_PERCENT": float(env("COMPANY_PROFIT_PERCENT", 5)),
    # Vacation Equb's cut of every travel-agency sale (taken from the agency's share).
    "AGENCY_COMMISSION_PERCENT": float(env("AGENCY_COMMISSION_PERCENT", 5)),
    "REFERRALS_PER_GIVEAWAY_ENTRY": int(env("REFERRALS_PER_GIVEAWAY_ENTRY", 10)),
    "TICKET_SIGNING_KEY": env("TICKET_SIGNING_KEY", SECRET_KEY),
    "DEFAULT_PAYMENT_PROVIDER": env("DEFAULT_PAYMENT_PROVIDER", "mock" if DEBUG else "telebirr"),
    "ENABLE_MOCK_GATEWAY": env_bool("ENABLE_MOCK_GATEWAY", DEBUG),
    "PUBLIC_BASE_URL": env("PUBLIC_BASE_URL", "http://localhost:8000"),
    "APP_DEEP_LINK": env("APP_DEEP_LINK", "vacation://payment-result"),
    # Shown on /privacy/ and /account/delete/ – set SUPPORT_EMAIL to the inbox you really read.
    "SUPPORT_EMAIL": env("SUPPORT_EMAIL", "support@vacationequb.com"),
    "PRIVACY_UPDATED": env("PRIVACY_UPDATED", "6 November 2026"),
}

# --- Payment gateways ------------------------------------------------------
PAYMENT_GATEWAYS = {
    "telebirr": {
        "BASE_URL": env("TELEBIRR_BASE_URL", "https://developerportal.ethiotelebirr.et:38443/apiaccess/payment/gateway"),
        "WEB_CHECKOUT_URL": env("TELEBIRR_WEB_CHECKOUT_URL", "https://developerportal.ethiotelebirr.et:38443/payment/web/paygate?"),
        "FABRIC_APP_ID": env("TELEBIRR_FABRIC_APP_ID", ""),
        "APP_SECRET": env("TELEBIRR_APP_SECRET", ""),
        "MERCHANT_APP_ID": env("TELEBIRR_MERCHANT_APP_ID", ""),
        "MERCHANT_CODE": env("TELEBIRR_MERCHANT_CODE", ""),
        "PRIVATE_KEY_PEM": env("TELEBIRR_PRIVATE_KEY_PEM", ""),
        "PUBLIC_KEY_PEM": env("TELEBIRR_PUBLIC_KEY_PEM", ""),
        "TIMEOUT": int(env("TELEBIRR_TIMEOUT", 20)),
        "VERIFY_SSL": env_bool("TELEBIRR_VERIFY_SSL", True),
    },
    "chapa": {
        "BASE_URL": env("CHAPA_BASE_URL", "https://api.chapa.co/v1"),
        "PUBLIC_KEY": env("CHAPA_PUBLIC_KEY", ""),
        "SECRET_KEY": env("CHAPA_SECRET_KEY", ""),
        "ENCRYPTION_KEY": env("CHAPA_ENCRYPTION_KEY", ""),
        # The "secret hash" typed in Chapa dashboard → Settings → Webhooks.
        "WEBHOOK_SECRET": env("CHAPA_WEBHOOK_SECRET", ""),
        "CHECKOUT_TITLE": env("CHAPA_CHECKOUT_TITLE", "Vacation Equb"),
        "TIMEOUT": int(env("CHAPA_TIMEOUT", 20)),
    },
}

# --- Push notifications ----------------------------------------------------
PUSH = {
    "BACKEND": env("PUSH_BACKEND", "apps.notifications.push.LogPushBackend"),
    "EXPO_ACCESS_TOKEN": env("EXPO_ACCESS_TOKEN", ""),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"simple": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "simple"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}

if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_CONTENT_TYPE_NOSNIFF = True