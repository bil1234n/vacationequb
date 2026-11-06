"""Payment orchestration: create → redirect → webhook/verify → fulfil (idempotent)."""
from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.utils import timezone

from apps.core.exceptions import BusinessRuleError
from apps.core.middleware import public_base_url

from . import registry
from .gateways import GatewayError, get_gateway
from .models import PaymentTransaction

log = logging.getLogger(__name__)
Status = PaymentTransaction.Status


def _urls(txn):
    base = public_base_url()
    # Chapa calls callback_url with an unsigned GET; its signed POST webhook is set in the Chapa dashboard.
    notify = (f"{base}/api/payments/chapa/callback/" if txn.provider == "chapa"
              else f"{base}/api/payments/webhooks/{txn.provider}/")
    return {"notify_url": notify, "return_url": f"{base}/api/payments/return/{txn.reference}/"}


def _attach_agency(txn: PaymentTransaction, target) -> None:
    """Mark agency sales so Chapa splits them and the commission is recorded."""
    from apps.agencies.services import agency_for_payment

    agency = agency_for_payment(target, txn.user)
    if agency is None:
        return
    txn.agency = agency
    txn.platform_fee = agency.commission_on(txn.amount)
    # Chapa pays the agency's share straight to its bank when it has a subaccount.
    txn.split_settled = txn.provider == "chapa" and agency.has_split


def initiate_payment(*, user, target, amount: Decimal, purpose: str, provider: str | None = None,
                     payer_phone: str = "") -> PaymentTransaction:
    """Create a transaction for ``target`` and obtain a checkout URL from the provider."""
    provider = provider or settings.VACATION["DEFAULT_PAYMENT_PROVIDER"]
    amount = Decimal(amount).quantize(Decimal("0.01"))
    if amount <= 0:
        raise BusinessRuleError("Payment amount must be positive.")
    try:
        gateway = get_gateway(provider)
    except GatewayError as exc:
        raise BusinessRuleError(str(exc)) from exc

    ct = ContentType.objects.get_for_model(target)
    # Re-use an in-flight attempt instead of creating duplicates on double taps.
    existing = PaymentTransaction.objects.filter(
        user=user, content_type=ct, object_id=target.pk, provider=provider, amount=amount,
        status__in=[Status.INITIATED, Status.PENDING],
    ).exclude(checkout_url="").first()
    if existing:
        return existing

    txn = PaymentTransaction(
        user=user, provider=provider, purpose=purpose, amount=amount, currency=settings.VACATION["CURRENCY"],
        content_type=ct, object_id=target.pk, payer_phone=payer_phone or user.phone,
    )
    _attach_agency(txn, target)
    txn.save()
    try:
        result = gateway.initiate(txn, **_urls(txn))
    except GatewayError as exc:
        txn.status = Status.FAILED
        txn.failure_reason = str(exc)[:255]
        txn.save(update_fields=["status", "failure_reason", "updated_at"])
        raise BusinessRuleError(f"Payment provider unavailable: {exc}") from exc

    txn.checkout_url = result.checkout_url
    txn.provider_reference = result.provider_reference
    txn.raw_request = {"client_payload": result.client_payload}
    txn.raw_response = result.raw
    txn.status = Status.PENDING
    txn.save()
    return txn


def apply_status(txn_id: int, new_status: str, *, provider_reference: str = "", amount: Decimal | None = None,
                 payload: dict | None = None, reason: str = "") -> PaymentTransaction:
    """Move a transaction to a new status and run fulfilment exactly once."""
    with transaction.atomic():
        txn = PaymentTransaction.objects.select_for_update().get(pk=txn_id)
        late_success = (new_status == Status.SUCCESS and txn.status in (Status.CANCELLED, Status.EXPIRED)
                        and txn.purpose == PaymentTransaction.Purpose.EQUB_CONTRIBUTION)
        if txn.is_final and not late_success:
            return txn  # idempotent: duplicates/late webhooks are ignored
        # A member who cancelled (or let time out) an Equb payment but still finished it on Chapa: the money
        # arrived, so it is credited instead of being lost.
        if new_status == Status.SUCCESS and amount is not None and Decimal(amount) < txn.amount:
            new_status, reason = Status.FAILED, f"Amount mismatch: paid {amount}, expected {txn.amount}"
        txn.status = new_status
        if provider_reference:
            txn.provider_reference = provider_reference
        if payload:
            txn.callback_payload = payload
        if reason:
            txn.failure_reason = reason[:255]
        if txn.is_final:
            txn.completed_at = timezone.now()
        txn.save()

        handler = registry.get(txn.purpose)
        if handler and new_status == Status.SUCCESS and not txn.fulfilled:
            handler.on_success(txn)
            txn.fulfilled = True
            txn.save(update_fields=["fulfilled", "updated_at"])
        elif handler and handler.on_failure and txn.is_final and new_status != Status.SUCCESS:
            handler.on_failure(txn)
        if new_status == Status.SUCCESS and txn.agency_id:
            from apps.agencies.services import record_commission

            record_commission(txn)
    return txn


def refresh_from_provider(txn: PaymentTransaction) -> PaymentTransaction:
    if txn.is_final:
        return txn
    try:
        result = get_gateway(txn.provider).verify(txn)
    except GatewayError as exc:
        log.warning("verify failed for %s: %s", txn.reference, exc)
        return txn
    if result.status != txn.status:
        return apply_status(txn.pk, result.status, provider_reference=result.provider_reference,
                            amount=result.amount, payload=result.raw, reason=result.failure_reason)
    return txn
