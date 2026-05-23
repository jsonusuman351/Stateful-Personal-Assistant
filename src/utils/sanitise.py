"""Input sanitisation: null bytes, Unicode NFC, and whitespace (T-006)."""

from __future__ import annotations

import unicodedata


def sanitise_string(s: str) -> str:
    """Sanitise a string by rejecting null bytes, normalizing to NFC, and stripping whitespace.

    Implements the three-step pipeline from DESIGN §9.6:
    1. Reject any string containing \\x00 (null byte).
    2. Normalise to Unicode NFC form.
    3. Strip leading and trailing whitespace.

    Args:
        s: Input string to sanitise.

    Returns:
        Sanitised string.

    Raises:
        ValueError: If the string contains a null byte (\\x00).
    """
    # Step 1: Reject null bytes
    if "\x00" in s:
        raise ValueError("String contains null byte (\\x00)")

    # Step 2: Normalise to NFC
    nfc_str = unicodedata.normalize("NFC", s)

    # Step 3: Strip leading/trailing whitespace
    return nfc_str.strip()
