"""Doctor payouts through RazorpayX.

A payout row is queued in `payouts` when a consult is completed or the patient
didn't show (see appointments._queue_payout). The worker's payout job sends
each one:

  queued → sending → sent      transfer accepted by the provider
                   → failed    rejected, or still erroring after MAX_ATTEMPTS
                   → queued    transient error; retried with backoff

A payout is never sent twice:
  * a row is claimed atomically (queued → sending), so only one worker has it;
  * every attempt for a row uses the same idempotency key, stored on the row
    before the first call, so a retry after a lost response is deduplicated
    by the provider instead of paying again;
  * `sent` and `failed` rows are never claimed again.

SUPPAI_PAYOUTS_PROVIDER picks the provider: "fake" (default; dev and tests,
never calls out) or "razorpayx".
"""

import secrets
import uuid
from dataclasses import dataclass

import httpx

from app.config import get_settings

RAZORPAYX_API = "https://api.razorpay.com/v1"


@dataclass(frozen=True)
class Beneficiary:
    doctor_id: str
    name: str
    email: str | None
    phone: str | None
    account_number: str
    ifsc: str


@dataclass(frozen=True)
class TransferResult:
    provider_payout_id: str
    provider_status: str


class PayoutRejected(Exception):
    """The provider refused the payout. Retrying the same request won't help."""


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


class PayoutsProvider:
    def transfer(self, *, beneficiary: Beneficiary, amount_paise: int, reference_id: str,
                 idempotency_key: str, notes: dict) -> TransferResult:
        """Sends money to the beneficiary. Same idempotency_key → same payout, never a second one."""
        raise NotImplementedError


class FakePayoutsProvider(PayoutsProvider):
    def __init__(self) -> None:
        self.payouts: dict[str, dict] = {}  # idempotency_key -> payout
        self.calls = 0

    def transfer(self, *, beneficiary: Beneficiary, amount_paise: int, reference_id: str,
                 idempotency_key: str, notes: dict) -> TransferResult:
        self.calls += 1
        if idempotency_key not in self.payouts:
            self.payouts[idempotency_key] = {
                "id": f"pout_fake_{secrets.token_hex(6)}", "status": "processing", "amount": amount_paise,
                "reference_id": reference_id, "account_number": beneficiary.account_number,
                "ifsc": beneficiary.ifsc, "notes": notes,
            }
        p = self.payouts[idempotency_key]
        return TransferResult(p["id"], p["status"])


class RazorpayXProvider(PayoutsProvider):
    """Bank transfer with the RazorpayX composite payout API (contact and fund account inline).

    Not exercised against the live API by the test suite. Check the request
    against the current RazorpayX docs and run it in RazorpayX test mode first.
    """

    FAILED_STATUSES = {"rejected", "failed", "cancelled", "reversed"}

    def __init__(self) -> None:
        s = get_settings()
        if not s.razorpayx_account_number:
            raise RuntimeError("Set SUPPAI_RAZORPAYX_ACCOUNT_NUMBER to send payouts through RazorpayX")
        self._auth = (s.razorpay_key_id, s.razorpay_key_secret)
        self._account_number = s.razorpayx_account_number
        self._mode = s.razorpayx_payout_mode

    def transfer(self, *, beneficiary: Beneficiary, amount_paise: int, reference_id: str,
                 idempotency_key: str, notes: dict) -> TransferResult:
        contact = {"name": beneficiary.name, "type": "vendor", "reference_id": beneficiary.doctor_id}
        if beneficiary.email:
            contact["email"] = beneficiary.email
        if beneficiary.phone:
            contact["contact"] = beneficiary.phone
        r = httpx.post(
            f"{RAZORPAYX_API}/payouts",
            auth=self._auth,
            headers={"X-Payout-Idempotency": idempotency_key},
            json={
                "account_number": self._account_number,
                "amount": amount_paise,
                "currency": "INR",
                "mode": self._mode,
                "purpose": "payout",
                "queue_if_low_balance": True,
                "reference_id": reference_id,
                "narration": "SuppAI consult payout",
                "notes": notes,
                "fund_account": {
                    "account_type": "bank_account",
                    "bank_account": {"name": beneficiary.name, "ifsc": beneficiary.ifsc,
                                     "account_number": beneficiary.account_number},
                    "contact": contact,
                },
            },
            timeout=20,
        )
        if r.status_code in (400, 401, 403, 422):
            raise PayoutRejected(f"RazorpayX {r.status_code}: {r.text[:500]}")
        r.raise_for_status()  # 409 (same key still processing), 429 and 5xx are retried
        body = r.json()
        if body.get("status") in self.FAILED_STATUSES:
            raise PayoutRejected(f"RazorpayX payout {body.get('id')} is {body.get('status')}")
        return TransferResult(body["id"], body.get("status", ""))


_provider: PayoutsProvider | None = None


def provider() -> PayoutsProvider:
    global _provider
    if _provider is None:
        name = get_settings().payouts_provider
        if name == "razorpayx":
            _provider = RazorpayXProvider()
        elif name == "fake":
            _provider = FakePayoutsProvider()
        else:
            raise ValueError(f"Unknown SUPPAI_PAYOUTS_PROVIDER {name!r}")
    return _provider
