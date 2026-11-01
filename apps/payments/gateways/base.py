"""
Abstract payment gateway contract.

Every aggregator (telebirr, Chapa, CBE Birr, M-Pesa Ethiopia …) implements the
same four operations so that business code never depends on a provider SDK.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional


class GatewayError(Exception):
    """Raised when a provider call fails or returns an unexpected payload."""


@dataclass
class InitiationResult:
    checkout_url: str
    provider_reference: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    # Extra data the mobile app may need (e.g. telebirr in-app SDK receiveCode)
    client_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerificationResult:
    status: str  # one of PaymentTransaction.Status values
    provider_reference: str = ""
    amount: Optional[Decimal] = None
    raw: dict[str, Any] = field(default_factory=dict)
    failure_reason: str = ""


@dataclass
class WebhookResult:
    merchant_reference: str  # our PaymentTransaction.reference
    status: str
    provider_reference: str = ""
    amount: Optional[Decimal] = None
    raw: dict[str, Any] = field(default_factory=dict)


class PaymentGateway(abc.ABC):
    name: str = "base"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abc.abstractmethod
    def initiate(self, txn, *, return_url: str, notify_url: str) -> InitiationResult:
        """Create the order at the provider and return a checkout URL."""

    @abc.abstractmethod
    def verify(self, txn) -> VerificationResult:
        """Query the provider for the authoritative status of ``txn``."""

    @abc.abstractmethod
    def parse_webhook(self, body: bytes, headers: dict) -> WebhookResult:
        """Validate the signature of an inbound callback and normalise it.

        Must raise ``GatewayError`` if the signature is invalid.
        """

    def refund(self, txn, amount: Decimal | None = None, reason: str = "") -> VerificationResult:  # pragma: no cover
        raise GatewayError(f"Refunds are not supported by {self.name}")
