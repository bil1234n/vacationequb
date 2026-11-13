import json
import logging

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.utils.html import escape
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions, viewsets
from rest_framework.response import Response
from rest_framework.views import APIView

from .gateways import GatewayError, get_gateway
from .gateways.mock import mock_signature
from .models import PaymentTransaction, WebhookEvent
from .serializers import PaymentTransactionSerializer
from .services import apply_status, refresh_from_provider

log = logging.getLogger(__name__)


class PaymentTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    """A user's payment history. ``?refresh=1`` on detail re-queries the provider."""

    serializer_class = PaymentTransactionSerializer
    permission_classes = [permissions.IsAuthenticated]
    lookup_field = "reference"
    filterset_fields = ["status", "purpose", "provider"]

    def get_queryset(self):
        if getattr(self, "swagger_fake_view", False):
            return PaymentTransaction.objects.none()
        return PaymentTransaction.objects.filter(user=self.request.user)

    def retrieve(self, request, *args, **kwargs):
        txn = self.get_object()
        if request.query_params.get("refresh"):
            txn = refresh_from_provider(txn)
        return Response(self.get_serializer(txn).data)


@method_decorator(csrf_exempt, name="dispatch")
class WebhookView(APIView):
    """Inbound provider callbacks. Signature verification happens in the gateway."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def post(self, request, provider):
        body = request.body
        headers = {k: v for k, v in request.headers.items()}
        event = WebhookEvent.objects.create(provider=provider, headers=headers, body=body.decode(errors="replace"))
        try:
            result = get_gateway(provider).parse_webhook(body, headers)
        except (GatewayError, ValueError, KeyError) as exc:
            event.processing_error = str(exc)
            event.save(update_fields=["processing_error"])
            log.warning("Rejected %s webhook: %s", provider, exc)
            return Response({"detail": "invalid notification"}, status=400)

        event.signature_valid = True
        txn = _find_txn(result.merchant_reference)
        if not txn:
            event.processing_error = "unknown transaction"
            event.save()
            # Chapa retries anything that is not 200 for 72 h (payouts, payment links…): acknowledge and move on.
            return Response({"detail": "ignored"}, status=200 if provider == "chapa" else 404)
        event.transaction = txn
        event.save()
        # Never trust the callback alone for success: confirm with the provider API.
        if result.status == PaymentTransaction.Status.SUCCESS and provider != "mock":
            refresh_from_provider(txn)
        else:
            apply_status(txn.pk, result.status, provider_reference=result.provider_reference,
                         amount=result.amount, payload=result.raw)
        return Response({"code": 0, "msg": "success"})


@method_decorator(csrf_exempt, name="dispatch")
class ChapaCallbackView(APIView):
    """
    Chapa's ``callback_url``: an unsigned ``GET ?trx_ref=…&ref_id=…&status=…`` after each checkout.
    It is only a hint – the status is always confirmed with Chapa's verify API.
    """

    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = []

    def get(self, request):
        ref = request.query_params.get("trx_ref") or request.query_params.get("tx_ref") or ""
        txn = _find_txn(ref)
        if txn and txn.provider == "chapa":
            txn = refresh_from_provider(txn)
            return Response({"reference": str(txn.reference), "status": txn.status})
        return Response({"detail": "ignored"})

    post = get  # some Chapa flows POST the same fields


def _find_txn(ref: str):
    ref = (ref or "").replace("-", "")
    if len(ref) != 32:
        return None
    return PaymentTransaction.objects.filter(reference=ref).first()


class PaymentReturnView(APIView):
    """Browser lands here after checkout; bounce back into the mobile app."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def get(self, request, reference):
        txn = get_object_or_404(PaymentTransaction, reference=reference)
        txn = refresh_from_provider(txn)
        deep_link = f"{settings.ETHIOVACATION['APP_DEEP_LINK']}?reference={txn.reference}&status={txn.status}"
        return HttpResponseRedirect(deep_link) if request.query_params.get("redirect") else Response(
            {"reference": str(txn.reference), "status": txn.status, "deep_link": deep_link})


@method_decorator(csrf_exempt, name="dispatch")
class MockCheckoutView(APIView):
    """A tiny hosted checkout page for the sandbox gateway (dev/demo only)."""

    authentication_classes = []
    permission_classes = [permissions.AllowAny]

    def _txn(self, reference):
        if not settings.ETHIOVACATION["ENABLE_MOCK_GATEWAY"]:
            raise Http404
        return get_object_or_404(PaymentTransaction, reference=reference, provider="mock")

    def get(self, request, reference):
        txn = self._txn(reference)
        html = f"""<!doctype html><html><head><meta name=viewport content="width=device-width,initial-scale=1">
<title>EthioVacation Sandbox Pay</title><style>body{{font-family:system-ui;background:#fff;color:#0F382B;padding:24px}}
.card{{border:1px solid #d9e5df;border-radius:16px;padding:24px;max-width:420px;margin:auto}}
button{{width:100%;padding:14px;border:0;border-radius:12px;font-size:16px;margin-top:12px;cursor:pointer}}
.pay{{background:#0F382B;color:#fff}}.fail{{background:#f3f5f4;color:#0F382B}}</style></head>
<body><div class=card><h2>Sandbox checkout</h2><p>{escape(txn.get_purpose_display())}</p>
<h1>{txn.amount} {txn.currency}</h1><p>Status: <b>{txn.status}</b></p>
<form method=post><button class=pay name=result value=SUCCESS>Pay with telebirr (simulated)</button>
<button class=fail name=result value=FAILED>Simulate failure</button></form></div></body></html>"""
        return HttpResponse(html)

    def post(self, request, reference):
        txn = self._txn(reference)
        result = request.data.get("result", "SUCCESS")
        result = result if result in {"SUCCESS", "FAILED", "CANCELLED"} else "SUCCESS"
        payload = {"reference": str(txn.reference), "status": result}
        body = json.dumps(payload).encode()
        get_gateway("mock").parse_webhook(body, {"X-Mock-Signature": mock_signature(body)})
        txn = apply_status(txn.pk, result, payload=payload)
        if request.content_type and "json" in request.content_type:
            return Response(PaymentTransactionSerializer(txn).data)
        return HttpResponseRedirect(f"{settings.ETHIOVACATION['APP_DEEP_LINK']}?reference={txn.reference}&status={txn.status}")
