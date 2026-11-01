import secrets
import string

_PNR_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no O/0/I/1 ambiguity


def generate_code(length: int = 6, alphabet: str = _PNR_ALPHABET) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def generate_numeric(length: int) -> str:
    return "".join(secrets.choice(string.digits) for _ in range(length))
