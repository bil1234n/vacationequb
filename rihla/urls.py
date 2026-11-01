from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.views import DashboardView, HealthView, LandingView

admin.site.site_header = "Rihla Travel Administration"
admin.site.site_title = "Rihla Admin"

urlpatterns = [
    path("", LandingView.as_view(), name="landing"),
    path("admin/", admin.site.urls),
    path("console/", include("apps.console.urls")),
    path("api/health/", HealthView.as_view(), name="health"),
    path("api/home/dashboard/", DashboardView.as_view(), name="dashboard"),
    path("api/auth/", include("apps.accounts.urls")),
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
