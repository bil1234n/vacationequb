from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import ChapaCallbackView, MockCheckoutView, PaymentReturnView, PaymentTransactionViewSet, WebhookView

router = DefaultRouter()
router.register("transactions", PaymentTransactionViewSet, basename="payment")

urlpatterns = [
    path("chapa/callback/", ChapaCallbackView.as_view(), name="chapa-callback"),
    path("webhooks/<str:provider>/", WebhookView.as_view(), name="payment-webhook"),
    path("return/<uuid:reference>/", PaymentReturnView.as_view(), name="payment-return"),
    path("mock/checkout/<uuid:reference>/", MockCheckoutView.as_view(), name="mock-checkout"),
] + router.urls
