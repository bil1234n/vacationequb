import base64
import json
from decimal import Decimal
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.core.testutils import client_for, make_package, make_user, passenger

from .gateways.telebirr import TelebirrGateway, build_sign_string
from .models import PaymentTransaction, WebhookEvent


def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    pub = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return key, priv, pub


class TelebirrTests(TestCase):
    def setUp(self):
        self.key, priv, pub = keypair()
        self.cfg = {"BASE_URL": "https://telebirr.test/gw", "WEB_CHECKOUT_URL": "https://telebirr.test/pay?",
                    "FABRIC_APP_ID": "fabric", "APP_SECRET": "secret", "MERCHANT_APP_ID": "app", "MERCHANT_CODE": "123456",
                    "PRIVATE_KEY_PEM": priv, "PUBLIC_KEY_PEM": pub}
        self.gw = TelebirrGateway(self.cfg)

    def test_sign_string_flattens_and_sorts(self):
        s = build_sign_string({"b": "2", "a": "1", "sign": "x", "biz_content": {"c": "3", "empty": ""}})
        self.assertEqual(s, "a=1&b=2&c=3")

    def test_sign_verify_roundtrip(self):
        payload = {"merch_order_id": "abc", "trade_status": "Completed", "total_amount": "10.00"}
        sig = self.gw.sign(payload)
        self.assertTrue(self.gw.verify_signature(payload, sig))
        self.assertFalse(self.gw.verify_signature({**payload, "total_amount": "1.00"}, sig))

    def test_initiate_builds_checkout_url(self):
        user = make_user()
        pkg = make_package()
        txn = PaymentTransaction.objects.create(user=user, provider="telebirr", purpose="BOOKING", amount=Decimal("10"),
                                                content_type_id=1, object_id=pkg.id)
        responses = [{"token": "Bearer tok"}, {"result": "SUCCESS", "biz_content": {"prepay_id": "PP123"}}]
        with mock.patch.object(TelebirrGateway, "_post", side_effect=responses) as post:
            res = self.gw.initiate(txn, return_url="https://r", notify_url="https://n")
        self.assertEqual(res.provider_reference, "PP123")
        self.assertTrue(res.checkout_url.startswith("https://telebirr.test/pay?appid=app&merch_code=123456"))
        sent = post.call_args_list[1].args[1]
        self.assertEqual(sent["biz_content"]["total_amount"], "10.00")
        self.assertTrue(self.gw.verify_signature(sent, sent["sign"]))

    def test_webhook_rejects_bad_signature_and_accepts_valid(self):
        user = make_user()
        api = client_for(user)
        pkg = make_package()
        ref = api.post("/api/travel/bookings/", {"package": pkg.id, "contact_phone": user.phone, "passengers": [passenger()]},
                       format="json").data["reference"]
        r = api.post(f"/api/travel/bookings/{ref}/pay/", {"provider": "mock"}, format="json")
        txn = PaymentTransaction.objects.get(reference=r.data["reference"])
        txn.provider = "telebirr"
        txn.save()

        bad = {"merch_order_id": txn.reference.hex, "trade_status": "Completed", "total_amount": "185000.00", "sign": "Zm9v"}
        with override_settings(PAYMENT_GATEWAYS={"telebirr": self.cfg}):
            r = APIClient().post("/api/payments/webhooks/telebirr/", bad, format="json")
            self.assertEqual(r.status_code, 400)
            good = {k: v for k, v in bad.items() if k != "sign"}
            good["payment_order_id"] = "TB999"
            good["sign"] = self.gw.sign(good)
            verified = {"biz_content": {"order_status": "Completed", "total_amount": "185000.00", "payment_order_id": "TB999"}}
            with mock.patch.object(TelebirrGateway, "_post", side_effect=[{"token": "t"}, verified]):
                r = APIClient().post("/api/payments/webhooks/telebirr/", good, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        txn.refresh_from_db()
        self.assertEqual(txn.status, "SUCCESS")
        self.assertTrue(txn.fulfilled)
        self.assertEqual(txn.target.status, "CONFIRMED")
        self.assertEqual(WebhookEvent.objects.filter(signature_valid=True).count(), 1)

    def test_underpayment_fails(self):
        from .services import apply_status

        user = make_user()
        pkg = make_package()
        api = client_for(user)
        ref = api.post("/api/travel/bookings/", {"package": pkg.id, "contact_phone": user.phone, "passengers": [passenger()]},
                       format="json").data["reference"]
        r = api.post(f"/api/travel/bookings/{ref}/pay/", {"provider": "mock"}, format="json")
        txn = PaymentTransaction.objects.get(reference=r.data["reference"])
        txn = apply_status(txn.pk, "SUCCESS", amount=Decimal("100"))
        self.assertEqual(txn.status, "FAILED")
        self.assertEqual(txn.target.status, "PENDING_PAYMENT")


class PublicURLTests(TestCase):
    def test_checkout_links_follow_the_address_the_app_used(self):
        """Behind ngrok, checkout/return links must use the https ngrok host, not PUBLIC_BASE_URL."""
        user = make_user()
        pkg = make_package()
        api = client_for(user)
        ref = api.post("/api/travel/bookings/", {"package": pkg.id, "contact_phone": user.phone, "passengers": [passenger()]},
                       format="json").data["reference"]
        r = api.post(f"/api/travel/bookings/{ref}/pay/", {"provider": "mock"}, format="json",
                     HTTP_HOST="de58-196-188-161-28.ngrok-free.app", HTTP_X_FORWARDED_PROTO="https")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertTrue(r.data["checkout_url"].startswith("https://de58-196-188-161-28.ngrok-free.app/api/payments/mock/checkout/"),
                        r.data["checkout_url"])
        # and the same flow on the LAN uses the LAN address
        ref2 = api.post("/api/travel/bookings/", {"package": pkg.id, "contact_phone": user.phone, "passengers": [passenger()]},
                        format="json").data["reference"]
        r = api.post(f"/api/travel/bookings/{ref2}/pay/", {"provider": "mock"}, format="json", HTTP_HOST="10.194.60.210:8000")
        self.assertTrue(r.data["checkout_url"].startswith("http://10.194.60.210:8000/"), r.data["checkout_url"])
