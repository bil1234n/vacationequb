"""
Provably-fair draw primitives (commit / reveal).

1. When a draw is scheduled the server generates a secret ``server_seed`` and
   publishes only ``commitment = sha256(server_seed)``.
2. At draw time the winner index is derived deterministically from
   ``HMAC-SHA256(server_seed, public_input)`` where ``public_input`` is a
   canonical, ordered list of eligible participants (+ round identifier).
3. After the draw the seed is revealed. Anyone can recompute the commitment and
   the winner index and confirm the operator did not manipulate the result.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Sequence


def new_seed() -> str:
    return secrets.token_hex(32)


def commitment_for(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def build_public_input(context: str, participants: Sequence[str | int]) -> str:
    return f"{context}|" + ",".join(str(p) for p in participants)


def _digest_int(seed: str, message: str, counter: int) -> int:
    mac = hmac.new(seed.encode(), f"{message}#{counter}".encode(), hashlib.sha256).hexdigest()
    return int(mac, 16)


def pick_index(seed: str, public_input: str, n: int, counter: int = 0) -> int:
    """Return an unbiased index in ``[0, n)`` using rejection sampling."""
    if n <= 0:
        raise ValueError("Cannot draw from an empty pool")
    space = 2**256
    limit = space - (space % n)
    while True:
        value = _digest_int(seed, public_input, counter)
        if value < limit:
            return value % n
        counter += 1_000_000  # extremely unlikely; move to an independent stream


def pick_many(seed: str, public_input: str, n: int, k: int) -> list[int]:
    """Pick ``k`` distinct indices out of ``n`` (k <= n), deterministic."""
    k = min(k, n)
    chosen: list[int] = []
    counter = 0
    while len(chosen) < k:
        idx = pick_index(seed, public_input, n, counter)
        if idx not in chosen:
            chosen.append(idx)
        counter += 1
    return chosen


def pick_weighted(seed: str, public_input: str, weights: Sequence[float], k: int) -> list[int]:
    """
    Pick ``k`` distinct indices where index ``i`` is ``weights[i]`` times as likely as a weight-1 entry
    (Efraimidis–Spirakis weighted sampling). Each entry gets ``u_i`` from HMAC-SHA256(server_seed, public_input#w<i>)
    and the key ``ln(u_i) / weight_i``; the ``k`` largest keys win. Deterministic, so anyone holding the seed,
    the public input and the published weights can recompute the result.
    """
    import math

    n = len(weights)
    k = min(k, n)
    keys = []
    for i, w in enumerate(weights):
        u = (_digest_int(seed, public_input, 10_000_000 + i) + 1) / (2**256 + 1)  # 0 < u < 1
        keys.append((math.log(u) / float(w) if w > 0 else float("-inf"), -i))
    order = sorted(range(n), key=lambda i: keys[i], reverse=True)
    return order[:k]


@dataclass
class VerificationResult:
    commitment_ok: bool
    recomputed_indices: list[int]
    matches: bool

    def as_dict(self):
        return {
            "commitment_ok": self.commitment_ok,
            "recomputed_indices": self.recomputed_indices,
            "matches": self.matches,
        }


def verify(seed: str, commitment: str, public_input: str, n: int, expected: Sequence[int]) -> VerificationResult:
    ok = hmac.compare_digest(commitment_for(seed), commitment)
    indices = pick_many(seed, public_input, n, len(expected)) if n else []
    return VerificationResult(ok, indices, ok and list(indices) == list(expected))


def verify_weighted(seed: str, commitment: str, public_input: str, weights: Sequence[float],
                    expected: Sequence[int]) -> VerificationResult:
    ok = hmac.compare_digest(commitment_for(seed), commitment)
    indices = pick_weighted(seed, public_input, weights, len(expected)) if weights else []
    return VerificationResult(ok, indices, ok and sorted(indices) == sorted(expected))
