"""Sandbox gateway used in development, automated tests and app demos."""
from __future__ import annotations

import hashlib
import hmac
import json

from django.conf import settings

from .base import GatewayError, InitiationResult, PaymentGateway, VerificationResult, WebhookResult


def mock_signature(body: bytes) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), body, hashlib.sha256).hexdigest()


class MockGateway(PaymentGateway):
    name = "mock"

    def initiate(self, txn, *, return_url, notify_url):
        from apps.core.middleware import public_base_url

        base = public_base_url()
        url = f"{base}/api/payments/mock/checkout/{txn.reference}/"
        return InitiationResult(checkout_url=url, provider_reference=f"MOCK-{txn.reference.hex[:12].upper()}",
                                raw={"sandbox": True})

    def verify(self, txn):
        # The sandbox has no remote state – the local record is authoritative.
        return VerificationResult(status=txn.status, provider_reference=txn.provider_reference, amount=txn.amount)

    def parse_webhook(self, body, headers):
        sig = headers.get("X-Mock-Signature") or headers.get("x-mock-signature", "")
        if not hmac.compare_digest(sig, mock_signature(body)):
            raise GatewayError("Invalid mock signature")
        data = json.loads(body)
        return WebhookResult(merchant_reference=data["reference"], status=data["status"],
                             provider_reference=data.get("provider_reference", ""), raw=data)
