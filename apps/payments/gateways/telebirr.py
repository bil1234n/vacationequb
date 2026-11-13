"""
telebirr (Ethio Telecom) H5 / Web Checkout integration.

Flow
----
1. ``apply_fabric_token`` – exchange Fabric App ID + App Secret for a bearer token.
2. ``preOrder``           – create a signed order and receive ``prepay_id``.
3. Build the checkout URL (``rawRequest``) that the app opens in a WebView, or
   hand ``receiveCode`` to the native telebirr SDK (in-app payment).
4. telebirr POSTs a signed notification to ``notify_url``; we verify it with the
   telebirr platform public key, then confirm with ``queryOrder``.

Signing: all request fields (top level + ``biz_content``) except ``sign``,
``sign_type`` and ``biz_content`` itself are sorted by key, joined as
``k=v&k2=v2`` and signed with SHA256withRSA (PSS) using the merchant private key.

The field names follow telebirr's merchant integration guide; confirm them
against the document issued with your merchant credentials before go-live.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from apps.core.utils import generate_code

from .base import GatewayError, InitiationResult, PaymentGateway, VerificationResult, WebhookResult

log = logging.getLogger(__name__)

EXCLUDED_FROM_SIGN = {"sign", "sign_type", "header", "refund_info", "openType", "raw_request", "biz_content"}

STATUS_MAP = {
    "Completed": "SUCCESS",
    "SUCCESS": "SUCCESS",
    "PAY_SUCCESS": "SUCCESS",
    "Paying": "PENDING",
    "WAIT_PAY": "PENDING",
    "Pending": "PENDING",
    "Expired": "EXPIRED",
    "ORDER_CLOSED": "CANCELLED",
    "Failure": "FAILED",
    "PAY_FAILED": "FAILED",
}


def _load_pem(value: str) -> bytes:
    if not value:
        raise GatewayError("telebirr key is not configured")
    if "BEGIN" in value:
        return value.replace("\\n", "\n").encode()
    return Path(value).read_bytes()


def build_sign_string(payload: dict) -> str:
    flat: dict[str, str] = {}
    for key, val in payload.items():
        if key in EXCLUDED_FROM_SIGN or val in (None, ""):
            continue
        flat[key] = str(val)
    for key, val in (payload.get("biz_content") or {}).items():
        if key in EXCLUDED_FROM_SIGN or val in (None, ""):
            continue
        flat[key] = str(val)
    return "&".join(f"{k}={flat[k]}" for k in sorted(flat))


class TelebirrGateway(PaymentGateway):
    name = "telebirr"

    # ------------------------------------------------------------------ crypto
    def _private_key(self):
        return serialization.load_pem_private_key(_load_pem(self.config["PRIVATE_KEY_PEM"]), password=None)

    def _public_key(self):
        return serialization.load_pem_public_key(_load_pem(self.config["PUBLIC_KEY_PEM"]))

    def sign(self, payload: dict) -> str:
        message = build_sign_string(payload).encode()
        signature = self._private_key().sign(
            message,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode()

    def verify_signature(self, payload: dict, signature_b64: str) -> bool:
        message = build_sign_string(payload).encode()
        try:
            self._public_key().verify(
                base64.b64decode(signature_b64),
                message,
                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.AUTO),
                hashes.SHA256(),
            )
            return True
        except (InvalidSignature, ValueError):
            return False

    # ------------------------------------------------------------------- http
    def _post(self, path: str, body: dict, token: str | None = None) -> dict:
        headers = {"Content-Type": "application/json", "X-APP-Key": self.config["FABRIC_APP_ID"]}
        if token:
            headers["Authorization"] = token
        url = self.config["BASE_URL"].rstrip("/") + path
        try:
            resp = requests.post(url, json=body, headers=headers, timeout=self.config.get("TIMEOUT", 20),
                                 verify=self.config.get("VERIFY_SSL", True))
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            log.exception("telebirr call %s failed", path)
            raise GatewayError(f"telebirr request failed: {exc}") from exc

    def apply_fabric_token(self) -> str:
        data = self._post("/payment/v1/token", {"appSecret": self.config["APP_SECRET"]})
        token = data.get("token")
        if not token:
            raise GatewayError(f"telebirr token error: {data}")
        return token

    def _envelope(self, method: str, biz: dict) -> dict:
        payload = {
            "timestamp": str(int(time.time())),
            "nonce_str": generate_code(32, "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"),
            "method": method,
            "version": "1.0",
            "biz_content": biz,
            "sign_type": "SHA256WithRSA",
        }
        payload["sign"] = self.sign(payload)
        return payload

    # -------------------------------------------------------------- contract
    def initiate(self, txn, *, return_url, notify_url):
        token = self.apply_fabric_token()
        biz = {
            "notify_url": notify_url,
            "redirect_url": return_url,
            "appid": self.config["MERCHANT_APP_ID"],
            "merch_code": self.config["MERCHANT_CODE"],
            "merch_order_id": txn.reference.hex,
            "trade_type": "Checkout",
            "title": f"EthioVacation {txn.get_purpose_display()}"[:64],
            "total_amount": f"{txn.amount:.2f}",
            "trans_currency": txn.currency,
            "timeout_express": "120m",
            "business_type": "BuyGoods",
            "payee_identifier": self.config["MERCHANT_CODE"],
            "payee_identifier_type": "04",
            "payee_type": "5000",
            "callback_info": str(txn.reference),
        }
        request_body = self._envelope("payment.preorder", biz)
        data = self._post("/payment/v1/merchant/preOrder", request_body, token)
        prepay_id = (data.get("biz_content") or {}).get("prepay_id")
        if not prepay_id:
            raise GatewayError(f"telebirr preOrder rejected: {data.get('errorMsg') or data}")

        raw = {
            "appid": self.config["MERCHANT_APP_ID"],
            "merch_code": self.config["MERCHANT_CODE"],
            "nonce_str": request_body["nonce_str"],
            "prepay_id": prepay_id,
            "timestamp": request_body["timestamp"],
        }
        raw_sign = self.sign(raw)
        raw_request = "&".join(f"{k}={v}" for k, v in raw.items()) + f"&sign={quote(raw_sign)}&sign_type=SHA256WithRSA"
        checkout_url = f"{self.config['WEB_CHECKOUT_URL']}{raw_request}&version=1.0&trade_type=Checkout"
        return InitiationResult(
            checkout_url=checkout_url,
            provider_reference=prepay_id,
            raw={"request": {k: v for k, v in request_body.items() if k != "sign"}, "response": data},
            client_payload={"raw_request": raw_request, "receive_code": f"TELEBIRR$BUYGOODS${raw['merch_code']}${txn.amount:.2f}${prepay_id}$120m"},
        )

    def verify(self, txn):
        token = self.apply_fabric_token()
        biz = {
            "appid": self.config["MERCHANT_APP_ID"],
            "merch_code": self.config["MERCHANT_CODE"],
            "merch_order_id": txn.reference.hex,
        }
        data = self._post("/payment/v1/merchant/queryOrder", self._envelope("payment.queryorder", biz), token)
        biz_resp = data.get("biz_content") or {}
        state = biz_resp.get("order_status") or biz_resp.get("trade_status") or ""
        amount = biz_resp.get("total_amount")
        return VerificationResult(
            status=STATUS_MAP.get(state, "PENDING"),
            provider_reference=biz_resp.get("payment_order_id", txn.provider_reference),
            amount=Decimal(amount) if amount else None,
            raw=data,
        )

    def parse_webhook(self, body, headers):
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise GatewayError("Malformed telebirr notification") from exc
        signature = data.get("sign", "")
        if not signature or not self.verify_signature(data, signature):
            raise GatewayError("Invalid telebirr signature")
        merch_order_id = data.get("merch_order_id", "")
        return WebhookResult(
            merchant_reference=merch_order_id,
            status=STATUS_MAP.get(data.get("trade_status", ""), "PENDING"),
            provider_reference=data.get("payment_order_id", ""),
            amount=Decimal(data["total_amount"]) if data.get("total_amount") else None,
            raw=data,
        )
