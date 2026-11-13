"""
Draw eligibility form ("KYC"): photo, national ID and monthly income.

An Equb asks for it after ``kyc_after_payments`` paid rounds (set by staff when the Equb is created, 0 = off).
Members must have sent it (and staff must not have rejected it) to enter that Equb's draws.
Images are stored in the database – never as public files – and only staff can open them.
"""
from __future__ import annotations

import base64
import binascii
from decimal import Decimal, InvalidOperation

from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.notifications.models import Notification
from apps.notifications.services import notify

from .models import MemberKYC

MAX_BYTES = 4 * 1024 * 1024
TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic"}


def for_user(user) -> MemberKYC | None:
    return MemberKYC.objects.filter(user=user).defer("photo", "id_front", "id_back").first()


def public(k: MemberKYC | None) -> dict:
    if not k:
        return {"status": None, "submitted": False}
    return {"status": k.status, "status_display": k.get_status_display(), "submitted": True,
            "monthly_income": str(k.monthly_income), "national_id_number": k.national_id_number,
            "income_source": k.income_source, "review_note": k.review_note, "updated_at": k.updated_at}


def _image(data, files, name: str, required: bool = True) -> tuple[bytes | None, str]:
    f = files.get(name) if files else None
    if f is not None:
        raw, ctype = f.read(), (getattr(f, "content_type", "") or "image/jpeg").lower()
    else:
        v = (data.get(name) or "").strip() if hasattr(data, "get") else ""
        if not v:
            if required:
                raise BusinessRuleError(_missing(name))
            return None, ""
        ctype = "image/jpeg"
        if v.startswith("data:"):
            head, _, v = v.partition(",")
            ctype = head[5:].split(";")[0].lower() or ctype
        try:
            raw = base64.b64decode(v, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise BusinessRuleError("That picture could not be read. Please take it again.") from exc
    if not raw:
        raise BusinessRuleError(_missing(name))
    if len(raw) > MAX_BYTES:
        raise BusinessRuleError("The picture is too large (4 MB max). Please take it again.")
    if ctype not in TYPES:
        raise BusinessRuleError("Send a JPG or PNG picture.")
    return raw, ("image/jpeg" if ctype == "image/jpg" else ctype)


def _missing(name: str) -> str:
    return {"photo": "Add a clear photo of yourself.", "id_front": "Add a photo of your national ID (front)."}.get(
        name, "Add the picture.")


def submit(user, data, files=None) -> MemberKYC:
    photo, photo_type = _image(data, files, "photo")
    front, front_type = _image(data, files, "id_front")
    back, back_type = _image(data, files, "id_back", required=False)
    try:
        income = Decimal(str(data.get("monthly_income", "")).replace(",", "").strip())
    except (InvalidOperation, ValueError) as exc:
        raise BusinessRuleError("Enter your monthly income in ETB.") from exc
    if income < 0:
        raise BusinessRuleError("Enter your monthly income in ETB.")
    k = MemberKYC.objects.filter(user=user).first() or MemberKYC(user=user)
    k.photo, k.photo_type, k.id_front, k.id_front_type = photo, photo_type, front, front_type
    k.id_back, k.id_back_type = back, back_type
    k.monthly_income = income
    k.national_id_number = (data.get("national_id_number") or "").strip()[:40]
    k.income_source = (data.get("income_source") or "").strip()[:120]
    k.status, k.reviewed_by, k.reviewed_at, k.review_note = MemberKYC.Status.SUBMITTED, None, None, ""
    k.save()
    return k


def review(k: MemberKYC, staff, *, approve: bool, note: str = "") -> MemberKYC:
    k.status = MemberKYC.Status.APPROVED if approve else MemberKYC.Status.REJECTED
    k.reviewed_by, k.reviewed_at, k.review_note = staff, timezone.now(), (note or "").strip()[:255]
    k.save(update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"])
    if approve:
        notify(k.user, Notification.Kind.EQUB_REMINDER, "Eligibility form approved ✅",
               "Your details were checked. You can enter the draws of your Equbs.", {"kyc": "approved"})
    else:
        notify(k.user, Notification.Kind.EQUB_REMINDER, "Please send your eligibility form again",
               (k.review_note or "Some details could not be checked.") + " Open your Equb to send it again.",
               {"kyc": "rejected"})
    return k
