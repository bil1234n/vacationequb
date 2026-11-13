from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.accounts.web import delete_account, privacy_policy
from apps.core.views import AppVersionView, BannerListView, DashboardView, HealthView, LandingView

admin.site.site_header = "EthioVacation Administration"
admin.site.site_title = "EthioVacation Admin"

urlpatterns = [
    path("", LandingView.as_view(), name="landing"),
    path("privacy/", privacy_policy, name="privacy"),
    path("account/delete/", delete_account, name="delete-account-web"),
    path("admin/", admin.site.urls),
    path("console/", include("apps.console.urls")),
    path("agency/", include("apps.agencies.urls")),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/home/dashboard/", DashboardView.as_view(), name="dashboard"),
    path("api/home/banners/", BannerListView.as_view(), name="banners"),
    path("api/app/version/", AppVersionView.as_view(), name="app-version"),
    path("api/auth/", include("apps.accounts.urls")),
    path("api/agency/", include("apps.agencies.api_urls")),
    path("api/travel/", include("apps.travel.urls")),
    path("api/equb/", include("apps.equb.urls")),
    path("api/community/", include("apps.luckydraw.urls")),
    path("api/payments/", include("apps.payments.urls")),
    path("api/notifications/", include("apps.notifications.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
