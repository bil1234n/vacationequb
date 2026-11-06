"""
Chapa aggregator (cards, telebirr, CBE Birr, M-Pesa, Amole…).

* Checkout: ``POST /transaction/initialize`` → hosted checkout URL.
* Split payments: when a transaction belongs to a travel agency with a Chapa subaccount, the
  ``subaccounts`` block tells Chapa to keep our commission (``split_value`` = 0.05 for 5 %) and settle
  the rest straight to the agency's bank account.
* Callback: Chapa calls ``callback_url`` with ``GET ?trx_ref=…&status=…`` (unsigned) – we only use it as a
  hint and re-verify with the API.
* Webhook: the dashboard webhook POSTs JSON signed with ``x-chapa-signature`` (HMAC-SHA256 of the body) and
  ``chapa-signature`` (HMAC-SHA256 of the secret, keyed with the secret).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
from decimal import Decimal

import requests

from .base import GatewayError, InitiationResult, PaymentGateway, VerificationResult, WebhookResult

log = logging.getLogger(__name__)

# Chapa validates these strictly: title ≤ 16 chars; description letters, numbers, - _ . and spaces only.
_DESC_RE = re.compile(r"[^A-Za-z0-9 _.\-]")


def _clean_description(text: str) -> str:
    return re.sub(r"\s+", " ", _DESC_RE.sub(" ", text or "")).strip()[:50] or "Payment"


def chapa_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return "0" + digits[-9:] if len(digits) >= 9 else ""


class ChapaGateway(PaymentGateway):
    name = "chapa"

    # ------------------------------------------------------------------ plumbing
    def _headers(self):
        if not self.config.get("SECRET_KEY"):
            raise GatewayError("CHAPA_SECRET_KEY is not configured")
        return {"Authorization": f"Bearer {self.config['SECRET_KEY']}", "Content-Type": "application/json"}

    def _url(self, path):
        return self.config.get("BASE_URL", "https://api.chapa.co/v1").rstrip("/") + path

    def _call(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = requests.request(method, self._url(path), headers=self._headers(),
                                    timeout=self.config.get("TIMEOUT", 20), **kwargs)
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise GatewayError(f"Chapa {path} failed: {exc}") from exc
        if not isinstance(data, dict):
            raise GatewayError(f"Chapa {path} returned an unexpected response")
        if resp.status_code >= 400 or str(data.get("status", "")).lower() == "failed":
            raise GatewayError(f"Chapa: {self._message(data)}")
        return data

    @staticmethod
    def _message(data: dict) -> str:
        msg = data.get("message")
        if isinstance(msg, dict):  # validation errors come back as {field: [errors]}
            return "; ".join(f"{k}: {', '.join(v) if isinstance(v, list) else v}" for k, v in msg.items())
        return str(msg or "request rejected")

    # ------------------------------------------------------------------ checkout
    def initiate(self, txn, *, return_url, notify_url):
        user = txn.user
        first, _, last = (user.full_name or "Vacation Equb Customer").strip().partition(" ")
        body = {
            "amount": f"{txn.amount:.2f}",
            "currency": txn.currency,
            "first_name": first[:50] or "Customer",
            "last_name": (last or first)[:50],
            "tx_ref": txn.reference.hex,
            "callback_url": notify_url,
            "return_url": return_url,
            "customization": {
                "title": (self.config.get("CHECKOUT_TITLE") or "Vacation Equb")[:16],
                "description": _clean_description(txn.get_purpose_display()),
            },
            "meta": {"purpose": txn.purpose},
        }
        if user.email:
            body["email"] = user.email
        phone = chapa_phone(txn.payer_phone or user.phone)
        if phone:
            body["phone_number"] = phone
        split = self.split_for(txn)
        if split:
            body["subaccounts"] = split
        data = self._call("POST", "/transaction/initialize", json=body)
        if str(data.get("status")).lower() != "success" or not (data.get("data") or {}).get("checkout_url"):
            raise GatewayError(f"Chapa rejected the order: {self._message(data)}")
        return InitiationResult(checkout_url=data["data"]["checkout_url"], provider_reference=txn.reference.hex,
                                raw={**data, "request": {k: v for k, v in body.items() if k != "meta"}})

    @staticmethod
    def split_for(txn) -> dict | None:
        """Chapa split block for agency transactions (we keep ``platform_fee_percent``)."""
        agency = getattr(txn, "agency", None)
        sub = getattr(agency, "chapa_subaccount_id", "") if agency else ""
        if not sub or not getattr(agency, "is_active", False):
            return None
        rate = Decimal(str(agency.commission_percent)) / 100
        return {"id": sub, "split_type": "percentage", "split_value": float(rate.quantize(Decimal("0.0001")))}

    def verify(self, txn):
        data = self._call("GET", f"/transaction/verify/{txn.reference.hex}")
        info = data.get("data") or {}
        state = (info.get("status") or "").lower()
        status = {"success": "SUCCESS", "failed": "FAILED", "cancelled": "CANCELLED",
                  "pending": "PENDING"}.get(state, "PENDING")
        reason = ""
        if status == "SUCCESS" and (info.get("currency") or txn.currency) != txn.currency:
            status, reason = "FAILED", f"Currency mismatch: {info.get('currency')}"
        return VerificationResult(
            status=status,
            provider_reference=info.get("reference", "") or "",
            amount=Decimal(str(info["amount"])) if info.get("amount") not in (None, "") else None,
            raw=data,
            failure_reason=reason,
        )

    # ------------------------------------------------------------------ webhooks
    def _secrets(self) -> list[bytes]:
        keys = [self.config.get("WEBHOOK_SECRET"), self.config.get("SECRET_KEY")]
        return [k.encode() for k in keys if k]

    def signature_ok(self, body: bytes, headers: dict) -> bool:
        lower = {k.lower(): v for k, v in headers.items()}
        x_sig = lower.get("x-chapa-signature", "")
        c_sig = lower.get("chapa-signature", "")
        for secret in self._secrets():
            if x_sig and hmac.compare_digest(hmac.new(secret, body, hashlib.sha256).hexdigest(), x_sig):
                return True
            if c_sig and hmac.compare_digest(hmac.new(secret, secret, hashlib.sha256).hexdigest(), c_sig):
                return True
        return False

    def parse_webhook(self, body, headers):
        if not self._secrets():
            raise GatewayError("CHAPA_WEBHOOK_SECRET is not configured")
        if not self.signature_ok(body, headers):
            raise GatewayError("Invalid Chapa signature")
        data = json.loads(body)
        state = (data.get("status") or "").lower()
        event = (data.get("event") or "").lower()
        if event.startswith("payout") or data.get("type") == "Payout":
            state = "ignored"  # transfers are not customer payments
        status = {"success": "SUCCESS", "failed": "FAILED", "cancelled": "CANCELLED",
                  "reversed": "REFUNDED", "refunded": "REFUNDED"}.get(state, "PENDING")
        return WebhookResult(
            merchant_reference=data.get("tx_ref", "") or "",
            status=status,
            provider_reference=data.get("reference", "") or "",
            amount=Decimal(str(data["amount"])) if data.get("amount") not in (None, "") else None,
            raw=data,
        )

    # ------------------------------------------------------------------ refunds
    def refund(self, txn, amount: Decimal | None = None, reason: str = ""):
        body = {"reason": (reason or "Refund")[:120]}
        if amount is not None:
            body["amount"] = f"{Decimal(amount):.2f}"
        data = self._call("POST", f"/refund/{txn.reference.hex}", json=body)
        return VerificationResult(status="REFUNDED", provider_reference=txn.provider_reference, amount=amount, raw=data)

    # ------------------------------------------------------------------ banks & subaccounts (agency payouts)
    def banks(self) -> list[dict]:
        data = self._call("GET", "/banks")
        rows = data.get("data") or []
        out = [{"id": r.get("id"), "name": r.get("name", ""), "slug": r.get("slug", ""),
                "length": r.get("acct_length"), "is_mobile": bool(r.get("is_mobilemoney"))}
               for r in rows if isinstance(r, dict) and r.get("id") is not None]
        return sorted(out, key=lambda b: b["name"].lower())

    def create_subaccount(self, *, business_name: str, account_name: str, bank_code: int, account_number: str,
                          split_value: Decimal) -> str:
        body = {
            "business_name": business_name[:100],
            "account_name": account_name[:100],
            "bank_code": int(bank_code),
            "account_number": str(account_number).strip(),
            "split_type": "percentage",
            "split_value": float(Decimal(split_value).quantize(Decimal("0.0001"))),
        }
        data = self._call("POST", "/subaccount", json=body)
        info = data.get("data") or {}
        sub_id = (info.get("subaccount_id") or info.get("subaccounts[id]") or info.get("id")
                  if isinstance(info, dict) else info)
        if not sub_id:
            raise GatewayError(f"Chapa did not return a subaccount id: {self._message(data)}")
        return str(sub_id)
