"""Account deletion: request (app + web), suspended sign-in, staff approve / restore."""
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from . import services
from .models import User

PHONE, PASSWORD = "+251911000111", "secret-pass-1"


class AccountDeletionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(phone=PHONE, password=PASSWORD, full_name="Abebe Kebede",
                                             email="abebe@example.com", passport_number="EP1234567")
        self.staff = User.objects.create_user(phone="+251911000222", password="staff-pass-1", full_name="Staff",
                                              is_staff=True)
        self.api = APIClient()

    def login(self, password=PASSWORD):
        return self.api.post("/api/auth/login/", {"phone": PHONE, "password": password}, format="json")

    def test_request_needs_a_reason(self):
        self.api.force_authenticate(self.user)
        res = self.api.post("/api/auth/delete-account/", {"reason": ""}, format="json")
        self.assertEqual(res.status_code, 400)
        self.user.refresh_from_db()
        self.assertEqual(self.user.deletion_status, "")

    def test_request_suspends_and_blocks_sign_in(self):
        self.api.force_authenticate(self.user)
        res = self.api.post("/api/auth/delete-account/", {"reason": "I don't use the app"}, format="json")
        self.assertEqual(res.status_code, 202)
        self.user.refresh_from_db()
        self.assertEqual(self.user.deletion_status, User.DeletionStatus.PENDING)
        self.assertFalse(self.user.is_active)
        self.assertEqual(self.user.deletion_reason, "I don't use the app")

        self.api.force_authenticate(None)
        res = self.login()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "deletion_pending")
        self.assertEqual(self.login("wrong-password").status_code, 401)   # no hint without the password

    def test_staff_cannot_delete_themselves_from_the_app(self):
        self.api.force_authenticate(self.staff)
        res = self.api.post("/api/auth/delete-account/", {"reason": "testing it"}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_restore_lets_them_sign_in_again(self):
        services.request_deletion(self.user, "changed my phone")
        services.restore_account(self.user, by=self.staff, note="Called – keeps account")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.user.deletion_status, "")
        self.assertEqual(self.login().status_code, 200)

    def test_approve_wipes_personal_data_and_frees_the_phone(self):
        services.request_deletion(self.user, "privacy")
        services.approve_deletion(self.user, by=self.staff)
        self.user.refresh_from_db()
        self.assertEqual(self.user.deletion_status, User.DeletionStatus.DELETED)
        self.assertEqual(self.user.full_name, "Deleted user")
        self.assertEqual(self.user.email, "")
        self.assertEqual(self.user.passport_number, "")
        self.assertNotEqual(self.user.phone, PHONE)
        self.assertFalse(self.user.has_usable_password())
        self.assertEqual(self.login().status_code, 401)
        # the number can register again
        res = self.api.post("/api/auth/register/", {"phone": PHONE, "full_name": "New Person", "password": "583920"},
                            format="json")
        self.assertEqual(res.status_code, 201)

    def test_web_page_request(self):
        res = self.client.post(reverse("delete-account-web"), {"phone": "0911000111", "password": PASSWORD,
                                                               "reason": "Not using it", "confirm": "on"})
        self.assertEqual(res.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(self.user.deletion_status, User.DeletionStatus.PENDING)

    def test_web_page_rejects_wrong_password(self):
        res = self.client.post(reverse("delete-account-web"), {"phone": PHONE, "password": "nope", "reason": "Not using it",
                                                               "confirm": "on"})
        self.assertContains(res, "incorrect")
        self.user.refresh_from_db()
        self.assertEqual(self.user.deletion_status, "")

    def test_console_approve(self):
        services.request_deletion(self.user, "privacy")
        self.client.force_login(self.staff)
        res = self.client.post(reverse("console:deletion_decide", args=[self.user.pk]), {"action": "approve"})
        self.assertEqual(res.status_code, 302)
        self.user.refresh_from_db()
        self.assertEqual(self.user.deletion_status, User.DeletionStatus.DELETED)
        self.assertEqual(self.client.get(reverse("console:deletion_requests")).status_code, 200)

    def test_privacy_page(self):
        self.assertContains(self.client.get("/privacy/"), "Privacy policy")


class PinSignInTests(TestCase):
    def setUp(self):
        self.api = APIClient()

    def test_register_needs_a_six_digit_pin_and_check_phone(self):
        bad = self.api.post("/api/auth/register/", {"phone": "0911555001", "full_name": "Pin User", "password": "abc123"},
                            format="json")
        self.assertEqual(bad.status_code, 400)
        weak = self.api.post("/api/auth/register/", {"phone": "0911555001", "full_name": "Pin User", "password": "111111"},
                             format="json")
        self.assertEqual(weak.status_code, 400)
        ok = self.api.post("/api/auth/register/", {"phone": "0911555001", "full_name": "Pin User", "password": "482915"},
                           format="json")
        self.assertEqual(ok.status_code, 201, ok.data)
        r = self.api.post("/api/auth/check-phone/", {"phone": "0911555001"}, format="json").data
        self.assertEqual((r["exists"], r["has_pin"], r["first_name"]), (True, True, "Pin"))
        r = self.api.post("/api/auth/login/", {"phone": "0911555001", "password": "482915"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["needs_pin"])

    def test_old_password_account_sets_a_pin(self):
        User.objects.create_user("0911555002", "old-password", full_name="Old User")
        self.assertFalse(self.api.post("/api/auth/check-phone/", {"phone": "0911555002"}, format="json").data["has_pin"])
        r = self.api.post("/api/auth/login/", {"phone": "0911555002", "password": "old-password"}, format="json")
        self.assertTrue(r.data["needs_pin"])
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        r = self.api.post("/api/auth/set-pin/", {"current": "old-password", "pin": "739164"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(User.objects.get(phone="+251911555002").check_password("739164"))
