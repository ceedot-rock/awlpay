"""awLPay settlement: MOCK execution of a validated quote.

MOCK — coins never move here. execute_quote() runs the fee law, computes
net = amount - fee, and signs a receipt attesting to the law that WOULD
execute. If net <= 0 the value is eaten by fees and the quote is refused
("refused_dust_eaten_by_fees") instead of producing a receipt.

MAINNET: the AWL_MAINNET_ENABLED env defaults to "0". If it is "1",
execute_quote() raises loudly — mainnet execution is NOT implemented in
v1 and no real funds may touch this code path.
"""

from __future__ import annotations

import os
import uuid

from .chamber import sign_attestation, load_relayer_keys
from .fees import calculate_fees

MODE = "mock"

_REFUSAL_REASONS = {"refused_dust_eaten_by_fees", "refused_negative_amount"}


def _mainnet_guard() -> None:
    if os.environ.get("AWL_MAINNET_ENABLED", "0") == "1":
        raise RuntimeError(
            "AWL_MAINNET_ENABLED=1 but mainnet execution is NOT implemented "
            "in awLPay v1. Refusing: no real funds, no mainnet, local only.")


def execute_quote(quote: dict, tier_usage: dict,
                  signing_key=None) -> dict:
    """quote: {from_chain, from_token, to_chain, to_token, amount_cents,
               tier, path}
    tier_usage: {volume_used_cents, txs_used}

    Returns a signed attestation envelope:
        {"attestation": {"alg","kid","payload","sig"}}
    where payload is the canonical JSON of the receipt:
        {receipt_id, from_chain, from_token, to_chain, to_token,
         amount_cents, fee_cents, net_cents, tier, status, path,
         mode: "mock"}
    On fee-refusal the receipt carries refused:true and the refusal
    reason as status; it is still signed (the attestation is to the
    refusal law, not to a movement of funds).
    """
    _mainnet_guard()

    amount = quote["amount_cents"]
    tier = quote["tier"]
    volume_used = int(tier_usage.get("volume_used_cents", 0))
    txs_used = int(tier_usage.get("txs_used", 0))

    fee_cents, status = calculate_fees(amount, tier, volume_used, txs_used)

    if status in _REFUSAL_REASONS or fee_cents >= amount:
        refused_status = (status if status in _REFUSAL_REASONS
                          else "refused_dust_eaten_by_fees")
        receipt = {
            "receipt_id": uuid.uuid4().hex,
            "refused": True,
            "from_chain": quote["from_chain"],
            "from_token": quote["from_token"],
            "to_chain": quote["to_chain"],
            "to_token": quote["to_token"],
            "amount_cents": amount,
            "fee_cents": fee_cents,
            "net_cents": max(amount - fee_cents, 0),
            "tier": tier,
            "status": refused_status,
            "path": quote.get("path"),
            "mode": MODE,
        }
    else:
        receipt = {
            "receipt_id": uuid.uuid4().hex,
            "from_chain": quote["from_chain"],
            "from_token": quote["from_token"],
            "to_chain": quote["to_chain"],
            "to_token": quote["to_token"],
            "amount_cents": amount,
            "fee_cents": fee_cents,
            "net_cents": amount - fee_cents,
            "tier": tier,
            "status": status,
            "path": quote.get("path"),
            "mode": MODE,
        }

    if signing_key is None:
        signing_key, _vk, _src = load_relayer_keys()
    return {"attestation": sign_attestation(receipt, signing_key)}
