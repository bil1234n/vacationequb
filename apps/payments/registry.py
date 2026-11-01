"""
Fulfilment registry: decouples the payments app from the business apps.

Each business app registers what should happen when a payment for its purpose
succeeds or fails, e.g. ``register("BOOKING", on_success=confirm_booking)``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

Handler = Callable[["PaymentTransaction"], None]  # noqa: F821


@dataclass
class Fulfilment:
    on_success: Handler
    on_failure: Optional[Handler] = None


_registry: dict[str, Fulfilment] = {}


def register(purpose: str, *, on_success: Handler, on_failure: Optional[Handler] = None) -> None:
    _registry[purpose] = Fulfilment(on_success, on_failure)


def get(purpose: str) -> Fulfilment | None:
    return _registry.get(purpose)
