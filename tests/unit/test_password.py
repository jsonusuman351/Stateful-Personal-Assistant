"""Unit tests for Argon2id password hashing (T-013)."""

from __future__ import annotations

from src.auth.password import dummy_verify, hash_password, verify_password


class TestHashPassword:
    def test_hash_not_equal_plaintext(self) -> None:
        """hash_password must not return the plaintext unchanged."""
        plaintext = "supersecret"
        assert hash_password(plaintext) != plaintext

    def test_hash_uses_argon2id(self) -> None:
        """hash_password output must start with the Argon2id identifier."""
        result = hash_password("any-password")
        assert result.startswith("$argon2id$")


class TestVerifyPassword:
    def test_verify_correct_password(self) -> None:
        """verify_password must return True for the original plaintext."""
        plaintext = "correct-horse-battery-staple"
        hashed = hash_password(plaintext)
        assert verify_password(plaintext, hashed) is True

    def test_verify_wrong_password(self) -> None:
        """verify_password must return False for a wrong password."""
        hashed = hash_password("correct-password")
        assert verify_password("wrong-password", hashed) is False


class TestDummyVerify:
    def test_dummy_verify_returns_false(self) -> None:
        """dummy_verify must return False without raising an exception."""
        assert dummy_verify() is False
