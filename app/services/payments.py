"""Razorpay integration.

The webhook is the source of truth for "paid". The fake provider is used in
development and tests so nothing calls Razorpay.
"""

import hashlib
import hmac
import secrets

import httpx

from app.config import get_settings

RAZORPAY_API = "https://api.razorpay.com/v1"


class PaymentsProvider:
    def create_order(self, amount_paise: int, receipt: str, notes: dict) -> str:
        raise NotImplementedError

    def refund(self, payment_id: str, amount_paise: int, notes: dict) -> str:
        raise NotImplementedError


class FakeProvider(PaymentsProvider):
    def create_order(self, amount_paise: int, receipt: str, notes: dict) -> str:
        return f"order_fake_{secrets.token_hex(6)}"

    def refund(self, payment_id: str, amount_paise: int, notes: dict) -> str:
        return f"rfnd_fake_{secrets.token_hex(6)}"


class RazorpayProvider(PaymentsProvider):
    def __init__(self) -> None:
        s = get_settings()
        self._auth = (s.razorpay_key_id, s.razorpay_key_secret)

    def create_order(self, amount_paise: int, receipt: str, notes: dict) -> str:
        r = httpx.post(
            f"{RAZORPAY_API}/orders",
            auth=self._auth,
            json={"amount": amount_paise, "currency": "INR", "receipt": receipt, "notes": notes},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["id"]

    def refund(self, payment_id: str, amount_paise: int, notes: dict) -> str:
        r = httpx.post(
            f"{RAZORPAY_API}/payments/{payment_id}/refund",
            auth=self._auth,
            json={"amount": amount_paise, "speed": "normal", "notes": notes},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()["id"]


_provider: PaymentsProvider | None = None


def provider() -> PaymentsProvider:
    global _provider
    if _provider is None:
        _provider = RazorpayProvider() if get_settings().payments_provider == "razorpay" else FakeProvider()
    return _provider


def verify_webhook_signature(body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    secret = get_settings().razorpay_webhook_secret.encode()
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
