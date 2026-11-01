from django.conf import settings

from .base import GatewayError, PaymentGateway
from .chapa import ChapaGateway
from .mock import MockGateway
from .telebirr import TelebirrGateway

GATEWAY_CLASSES: dict[str, type[PaymentGateway]] = {
    "telebirr": TelebirrGateway,
    "chapa": ChapaGateway,
    "mock": MockGateway,
}


def get_gateway(name: str) -> PaymentGateway:
    if name == "mock" and not settings.RIHLA["ENABLE_MOCK_GATEWAY"]:
        raise GatewayError("The sandbox gateway is disabled in this environment")
    try:
        cls = GATEWAY_CLASSES[name]
    except KeyError as exc:
        raise GatewayError(f"Unknown payment provider '{name}'") from exc
    return cls(settings.PAYMENT_GATEWAYS.get(name, {}))


__all__ = ["get_gateway", "GatewayError", "PaymentGateway", "GATEWAY_CLASSES"]
