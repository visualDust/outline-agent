from __future__ import annotations

import hashlib
import hmac

from outline_agent.utils.signature import verify_outline_signature

SECRET = "top-secret"
BODY = b'{"hello":"world"}'


def _signature(timestamp: str) -> str:
    return hmac.new(SECRET.encode("utf-8"), f"{timestamp}.".encode("utf-8") + BODY, hashlib.sha256).hexdigest()


def test_verify_outline_signature_accepts_s_field() -> None:
    timestamp = "1234567890"
    header = f"t={timestamp},s={_signature(timestamp)}"
    verified, status = verify_outline_signature(SECRET, header, BODY)
    assert verified is True
    assert status == "verified"


def test_verify_outline_signature_accepts_v1_field() -> None:
    timestamp = "1234567890"
    header = f"t={timestamp},v1={_signature(timestamp)}"
    verified, status = verify_outline_signature(SECRET, header, BODY)
    assert verified is True
    assert status == "verified"
