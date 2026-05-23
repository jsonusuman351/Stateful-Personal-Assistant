"""Unit tests for input sanitisation (T-006)."""

from __future__ import annotations

import pytest

from src.utils.sanitise import sanitise_string


class TestNullByteRaises:
    def test_null_byte_raises(self) -> None:
        """sanitise_string() must raise ValueError for any string containing \\x00."""
        with pytest.raises(ValueError, match="null byte"):
            sanitise_string("hello\x00world")

    def test_null_byte_at_start(self) -> None:
        """Null byte at the start must raise."""
        with pytest.raises(ValueError, match="null byte"):
            sanitise_string("\x00hello")

    def test_null_byte_at_end(self) -> None:
        """Null byte at the end must raise."""
        with pytest.raises(ValueError, match="null byte"):
            sanitise_string("hello\x00")


class TestUnicodeNfcNormalisation:
    def test_unicode_nfc_normalisation(self) -> None:
        """Unicode strings must be normalised to NFC form.

        Tests that precomposed and decomposed forms of 'é' both normalise to the same value.
        """
        # Precomposed form: U+00E9 (single character)
        precomposed = "café"

        # Decomposed form: U+0065 U+0301 (base character + combining accent)
        decomposed = "café"

        # Both should normalise to the same NFC string
        assert sanitise_string(precomposed) == sanitise_string(decomposed)
        assert sanitise_string(precomposed) == "café"

    def test_nfc_idempotent(self) -> None:
        """Applying NFC normalisation twice should give the same result."""
        test_string = "café"
        once = sanitise_string(test_string)
        twice = sanitise_string(once)
        assert once == twice


class TestWhitespaceStripped:
    def test_whitespace_stripped(self) -> None:
        """sanitise_string() must strip leading and trailing whitespace."""
        assert sanitise_string("  hello  ") == "hello"
        assert sanitise_string("\thello\t") == "hello"
        assert sanitise_string("\nhello\n") == "hello"
        assert sanitise_string("  \t\nhello\n\t  ") == "hello"

    def test_internal_whitespace_preserved(self) -> None:
        """Internal whitespace must not be altered."""
        assert sanitise_string("hello   world") == "hello   world"
        assert sanitise_string("hello\nworld") == "hello\nworld"
        assert sanitise_string("hello\tworld") == "hello\tworld"

    def test_only_whitespace(self) -> None:
        """A string of only whitespace should become empty."""
        assert sanitise_string("   ") == ""
        assert sanitise_string("\t\n") == ""


class TestCleanStringUnchanged:
    def test_clean_string_unchanged(self) -> None:
        """sanitise_string() must return the string unchanged if it is already clean."""
        clean_string = "hello world"
        assert sanitise_string(clean_string) == clean_string

    def test_mixed_operations(self) -> None:
        """Test the full pipeline together."""
        # Decomposed unicode + whitespace
        messy = "  café  "
        clean = sanitise_string(messy)
        assert clean == "café"

        # Already clean should remain unchanged
        assert sanitise_string("café") == "café"
