"""Chapa aggregator (cards, telebirr, CBE Birr, M-Pesa, Amole…)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from decimal import Decimal

import requests

from .base import GatewayError, InitiationResult, PaymentGateway, VerificationResult, WebhookResult

log = logging.getLogger(__name__)


class ChapaGateway(PaymentGateway):
    name = "chapa"

    def _headers(self):
        if not self.config.get("SECRET_KEY"):
            raise GatewayError("CHAPA_SECRET_KEY is not configured")
        return {"Authorization": f"Bearer {self.config['SECRET_KEY']}", "Content-Type": "application/json"}

    def _url(self, path):
        return self.config["BASE_URL"].rstrip("/") + path

    def initiate(self, txn, *, return_url, notify_url):
        user = txn.user
        first, _, last = (user.full_name or "Rihla Customer").partition(" ")
        body = {
            "amount": f"{txn.amount:.2f}",
            "currency": txn.currency,
            "email": user.email or None,
            "first_name": first,
            "last_name": last or first,
            "phone_number": "0" + user.phone[-9:],
            "tx_ref": txn.reference.hex,
            "callback_url": notify_url,
            "return_url": return_url,
            "customization": {"title": "Rihla Travel", "description": txn.get_purpose_display()[:50]},
        }
        try:
            resp = requests.post(self._url("/transaction/initialize"), json=body, headers=self._headers(),
                                 timeout=self.config.get("TIMEOUT", 20))
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise GatewayError(f"Chapa initialize failed: {exc}") from exc
        if data.get("status") != "success":
            raise GatewayError(f"Chapa rejected the order: {data.get('message')}")
        return InitiationResult(checkout_url=data["data"]["checkout_url"], provider_reference=txn.reference.hex, raw=data)

    def verify(self, txn):
        try:
            resp = requests.get(self._url(f"/transaction/verify/{txn.reference.hex}"), headers=self._headers(),
                                timeout=self.config.get("TIMEOUT", 20))
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise GatewayError(f"Chapa verify failed: {exc}") from exc
        info = data.get("data") or {}
        state = (info.get("status") or "").lower()
        status = {"success": "SUCCESS", "failed": "FAILED", "pending": "PENDING"}.get(state, "PENDING")
        return VerificationResult(
            status=status,
            provider_reference=info.get("reference", ""),
            amount=Decimal(str(info["amount"])) if info.get("amount") is not None else None,
            raw=data,
        )

    def parse_webhook(self, body, headers):
        secret = (self.config.get("WEBHOOK_SECRET") or "").encode()
        expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
        provided = headers.get("X-Chapa-Signature") or headers.get("Chapa-Signature") or ""
        if not secret or not hmac.compare_digest(expected, provided):
            raise GatewayError("Invalid Chapa signature")
        data = json.loads(body)
        state = (data.get("status") or "").lower()
        return WebhookResult(
            merchant_reference=data.get("tx_ref", ""),
            status={"success": "SUCCESS", "failed": "FAILED"}.get(state, "PENDING"),
            provider_reference=data.get("reference", ""),
            amount=Decimal(str(data["amount"])) if data.get("amount") else None,
            raw=data,
        )
