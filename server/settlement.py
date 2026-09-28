"""awLPay settlement: executes a validated quote against real chains.

MODES (AWL_EXECUTION_MODE; default "mock"):
    mock      — v1 baseline: coins never move; the receipt attests to the
                fee law that WOULD execute. Path is attached, not run.
    dryrun    — REAL transaction construction: each transfer hop of the
                router path is built, signed with a throwaway/env settler
                key, and simulated (eth_call / simulateTransaction)
                against the configured TESTNET. Nothing is broadcast.
    broadcast — dryrun + real broadcast of each transfer hop. GATED:
                AWL_BROADCAST=1 AND testnet-only chains AND an explicit
                recipient (quote to_address or AWL_SETTLER_RECIPIENT).
                Without the flag this RAISES instead of downgrading.

Dust/refusal law is EXACTLY the CuNi spec's (spec/SettlementEngine.cuni):
net <= 0 -> refuse, unknown token -> refuse, no-conversion-path ->
refuse (those two are enforced at quote time in app.build_quote and
re-checked per hop by the executor). Executor-layer refusals
(refused_unwired_hop, refused_no_recipient) honor the spec's
"refused_*" status contract. Integer-cents floor-division agreement
with the CuNi spec is unchanged.

MAINNET: AWL_MAINNET_ENABLED=1 -> loud refusal, before anything else.
Additionally server/chains.assert_testnet() hard-refuses mainnet chain
ids / clusters with no env override. The first real mainnet transaction
waits for Corey's explicit per-charge approval; nothing here grants it.
"""

from __future__ import annotations

import os
import uuid

from .chains import EvmAdapter, SolanaAdapter
from .chamber import sign_attestation, load_relayer_keys
from .executor import MODES as EXEC_MODES, PathExecutor
from .fees import calculate_fees
from .oracle import default_mock_oracle

MOCK_MODE = "mock"

_REFUSAL_REASONS = {"refused_dust_eaten_by_fees", "refused_negative_amount"}


def _mainnet_guard() -> None:
    if os.environ.get("AWL_MAINNET_ENABLED", "0") == "1":
        raise RuntimeError(
            "AWL_MAINNET_ENABLED=1 but mainnet execution is NOT implemented "
            "in awLPay v1. Refusing: no real funds, no mainnet, local only.")


def _default_adapters() -> dict:
    return {
        "ethereum": EvmAdapter("ethereum"),
        "base": EvmAdapter("base"),
        "polygon": EvmAdapter("polygon"),
        "arbitrum": EvmAdapter("arbitrum"),
        "solana": SolanaAdapter("solana"),
    }


def execute_quote(quote: dict, tier_usage: dict,
                  signing_key=None, oracle=None, adapters=None) -> dict:
    """quote: {from_chain, from_token, to_chain, to_token, amount_cents,
               tier, path, to_address?}
    tier_usage: {volume_used_cents, txs_used}
    oracle: PriceOracle for hop amounts (defaults to the mock oracle in
            real modes; app.py passes the configured oracle).
    adapters: {chain: adapter} override (tests / custom wiring).

    Returns a signed attestation envelope:
        {"attestation": {"alg","kid","payload","sig"}}
    where payload is the canonical JSON of the receipt:
        {receipt_id, from_chain, from_token, to_chain, to_token,
         amount_cents, fee_cents, net_cents, tier, status, path,
         mode, hops?, to_address?, ...}
    On fee-refusal the receipt carries refused:true and the refusal
    reason as status; it is still signed (the attestation is to the
    refusal law, not to a movement of funds).
    """
    _mainnet_guard()

    mode = os.environ.get("AWL_EXECUTION_MODE", MOCK_MODE)
    if mode not in (MOCK_MODE,) + EXEC_MODES:
        raise ValueError("AWL_EXECUTION_MODE must be one of %s, got %r"
                         % ((MOCK_MODE,) + EXEC_MODES, mode))

    amount = quote["amount_cents"]
    tier = quote["tier"]
    volume_used = int(tier_usage.get("volume_used_cents", 0))
    txs_used = int(tier_usage.get("txs_used", 0))

    fee_cents, status = calculate_fees(amount, tier, volume_used, txs_used)

    receipt: dict = {
        "receipt_id": uuid.uuid4().hex,
        "from_chain": quote["from_chain"],
        "from_token": quote["from_token"],
        "to_chain": quote["to_chain"],
        "to_token": quote["to_token"],
        "amount_cents": amount,
        "fee_cents": fee_cents,
        "tier": tier,
        "path": quote.get("path"),
        "mode": mode,
    }

    if status in _REFUSAL_REASONS or fee_cents >= amount:
        refused_status = (status if status in _REFUSAL_REASONS
                          else "refused_dust_eaten_by_fees")
        receipt.update({
            "refused": True,
            "net_cents": max(amount - fee_cents, 0),
            "status": refused_status,
        })
    elif mode == MOCK_MODE:
        receipt.update({
            "net_cents": amount - fee_cents,
            "status": status,
        })
    else:
        # Real path: execute the router path hop-by-hop. The executor
        # validates EVERYTHING (wired hops, prices, dust, recipient,
        # broadcast gate) before executing anything — atomic.
        net_cents = amount - fee_cents
        executor = PathExecutor(
            adapters if adapters is not None else _default_adapters(),
            mode=mode)
        hop_oracle = oracle if oracle is not None else default_mock_oracle()
        outcome = executor.execute(quote.get("path") or [], net_cents,
                                   hop_oracle,
                                   to_address=quote.get("to_address"))
        receipt.update({
            "refused": outcome["refused"],
            "net_cents": net_cents,
            "status": outcome["status"],
            "hops": outcome["hops"],
        })
        for key in ("detail", "to_address", "throwaway_recipient",
                    "key_source"):
            if key in outcome:
                receipt[key] = outcome[key]

    if signing_key is None:
        signing_key, _vk, _src = load_relayer_keys()
    return {"attestation": sign_attestation(receipt, signing_key)}
