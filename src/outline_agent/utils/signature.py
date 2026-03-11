from __future__ import annotations

import hashlib
import hmac
from typing import Tuple

SignatureResult = Tuple[bool | None, str]


def verify_outline_signature(secret: str | None, header_value: str | None, body: bytes) -> SignatureResult:
    if not secret:
        return None, "no-signing-secret-configured"
    if not header_value:
        return False, "missing-signature-header"

    try:
        parts = dict(part.strip().split("=", 1) for part in header_value.split(","))
        timestamp = parts["t"]
        signature = parts.get("s") or parts.get("v1")
        if not signature:
            return False, "missing-signature-value"
    except Exception:
        return False, "malformed-signature-header"

    expected = hmac.new(
        secret.encode("utf-8"),
        f"{timestamp}.".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    is_valid = hmac.compare_digest(expected, signature)
    return is_valid, "verified" if is_valid else "signature-mismatch"
