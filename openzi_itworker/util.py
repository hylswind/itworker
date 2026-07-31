"""Small shared utilities."""

from __future__ import annotations

import secrets

_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_LOWER = "abcdefghijklmnopqrstuvwxyz"
_DIGIT = "0123456789"
_SYMBOL = "!@#$%^&*()_+-="


def generate_password(length: int = 24) -> str:
    """A strong random password guaranteed to contain at least one upper, lower,
    digit, and symbol."""
    if length < 4:
        raise ValueError("length must be >= 4")
    rng = secrets.SystemRandom()
    pool = _UPPER + _LOWER + _DIGIT + _SYMBOL
    chars = [rng.choice(_UPPER), rng.choice(_LOWER), rng.choice(_DIGIT), rng.choice(_SYMBOL)]
    chars += [rng.choice(pool) for _ in range(length - 4)]
    rng.shuffle(chars)
    return "".join(chars)
