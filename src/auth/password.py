"""Argon2id password hashing and verification via argon2-cffi."""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ph = PasswordHasher(
    time_cost=2,
    memory_cost=65536,
    parallelism=2,
    hash_len=32,
    salt_len=16,
)

# Pre-computed once at module load so dummy_verify() never calls ph.hash() at runtime.
_DUMMY_HASH: str = ph.hash("__dummy_constant_time_guard__")


def hash_password(plaintext: str) -> str:
    """Return an Argon2id hash of *plaintext*.

    Parameters match DESIGN §9.4: time_cost=2, memory_cost=65536, parallelism=2.
    Output always begins with ``$argon2id$``.
    """
    return ph.hash(plaintext)


def verify_password(plaintext: str, hashed: str) -> bool:
    """Return True if *plaintext* matches *hashed*, False otherwise.

    Catches only ``VerifyMismatchError`` (wrong password); all other argon2
    exceptions (e.g. malformed hash) propagate to the caller.
    """
    try:
        return ph.verify(hashed, plaintext)
    except VerifyMismatchError:
        return False


def dummy_verify() -> bool:
    """Perform a constant-time dummy verification; always returns False.

    Called on the email-not-found code path to prevent timing oracles: argon2
    performs the same memory-hard work as a real verify (DESIGN §9.4).
    """
    try:
        ph.verify(_DUMMY_HASH, "__not_the_dummy_password__")
    except VerifyMismatchError:
        pass
    return False
