#!/usr/bin/env python3
"""AwLPay toll settlement — internal endpoints for Agent Rider Gates 3 & 6.

Real-USDC settlement on Base mainnet (chain id 8453). Agent Rider calls
these; browsers never do. Every endpoint requires the
X-Toll-Service-Secret header (constant-time compare against
TOLL_SERVICE_SECRET) and TOLL_MAINNET_AUTHORIZED=1.

Endpoints:
    POST /internal/toll/deposit/verify   READ-ONLY. Verify a payer's real
        on-chain USDC transfer into the escrow or bond wallet, using the
        vendored x402 verification law (receipt ok, canonical USDC
        Transfer-log sum >= expected, payer bound by EIP-191 signature,
        replay-shaped). Returns the confirmed amount and payer address.
        Replay protection for deposits lives in Rider's DB
        (deposit_tx_hash UNIQUE); this endpoint never mutates state.
    POST /internal/toll/send   MOVES REAL MONEY. Sign + broadcast a toll
        USDC transfer from the slot wallet. Body: {"slot", "to_address",
        "amount_uusdc", "idempotency_key", "purpose"}. Guarded by the toll
        law: per-tx cap, daily cap, idempotency (no double-send), the
        dedicated toll-mainnet broadcast path. Amounts and recipients are
        Rider's policy — this endpoint enforces the law, not the deal.

Body: {"slot": "escrow"|"bonds", "tx_hash": "0x…", "payer_sig": "0x…",
       "min_uusdc": 10000, "ref": "job:<job_id>", "network"?: "mainnet"|"sepolia"}

network="sepolia" selects the Base Sepolia testnet path (chain 84532,
Circle testnet USDC, separate wallets/keys/state) and requires
TOLL_TESTNET_SEPOLIA=1. The default is mainnet. The two networks share
nothing — a testnet proof or transfer can never be mistaken for mainnet.

The payer signs binding_message(tx_hash, "toll:<slot>:<ref>") with the
wallet that sent the USDC — the same EIP-191 law as /api/pay/execute,
so a deposit proof signed for one service cannot be replayed at the
other, and nobody can claim somebody else's deposit by copying a tx
hash off the mempool.

Canonical-USDC-only: the toll path refuses to run when the AWL_USDC_BASE
override env is set — the vendored default (native Base USDC
0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913) must be in force.

RPC comes from TOLL_RPC_BASE only (no public fallback on the money
path). Outbound signing/broadcast lives behind a separate, dedicated
toll-mainnet broadcast path — never the generic EvmAdapter.broadcast().
"""

from __future__ import annotations

import hmac
import json
import os
import re
import urllib.request

import anyio
from starlette.responses import JSONResponse

from . import x402 as x402mod
from .chains import assert_toll_mainnet, assert_toll_testnet

BASE_MAINNET = "eip155:8453"
BASE_SEPOLIA = "eip155:84532"
CHAIN_ID = 8453
CANONICAL_USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

# Per-network toll identity. The two networks share nothing: separate
# chain, separate USDC contract, separate wallets, separate RPC env.
# "sepolia" additionally requires TOLL_TESTNET_SEPOLIA=1; "mainnet"
# requires TOLL_MAINNET_AUTHORIZED=1. Unknown network values are refused.
_NETWORKS = {
    "mainnet": {
        "rail": BASE_MAINNET,
        "chain_id": 8453,
        "usdc": CANONICAL_USDC_BASE,
        "wallets": {"escrow": "TOLL_ESCROW_WALLET",
                    "bonds": "TOLL_BONDS_WALLET"},
        "rpc_env": "TOLL_RPC_BASE",
        "forbid_usdc_override": True,
    },
    "sepolia": {
        "rail": BASE_SEPOLIA,
        "chain_id": 84532,
        "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",  # Circle testnet USDC
        "wallets": {"escrow": "TOLL_TESTNET_ESCROW_WALLET",
                    "bonds": "TOLL_TESTNET_BONDS_WALLET"},
        "rpc_env": "TOLL_RPC_SEPOLIA",
        "forbid_usdc_override": False,
    },
}

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_SLOTS = ("escrow", "bonds")
_MAX_BODY = 64 * 1024


def _network_of(body: dict):
    network = body.get("network", "mainnet")
    if network not in _NETWORKS:
        return None
    return network


def _service_secret() -> str:
    return os.environ.get("TOLL_SERVICE_SECRET", "")


def _authed(request) -> bool:
    secret = _service_secret()
    if not secret:
        return False  # fail closed: no secret configured => no access
    presented = request.headers.get("x-toll-service-secret", "")
    return hmac.compare_digest(presented, secret)


def _slot_wallet(slot: str, network: str):
    raw = os.environ.get(_NETWORKS[network]["wallets"][slot], "").strip()
    if not _ADDR_RE.match(raw):
        return None
    return raw


def _toll_rpc_urls(network: str) -> list[str]:
    env = _NETWORKS[network]["rpc_env"]
    return [u.strip() for u in os.environ.get(env, "").split(",")
            if u.strip()]


def _rpc_call(urls):
    def call(method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "method": method, "params": params}).encode()
        last = None
        for url in urls:
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json",
                         "User-Agent": "awlpay-toll/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=25) as r:
                    return json.load(r)
            except Exception as e:  # noqa: BLE001 - try next endpoint
                last = e
        raise last if last is not None else RuntimeError("no toll RPC urls")
    return call


def _parse_body(raw: bytes):
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, {"verified": False, "reason": "body is not JSON"}
    if not isinstance(body, dict):
        return None, {"verified": False, "reason": "body must be a JSON object"}
    slot = body.get("slot")
    if slot not in _SLOTS:
        return None, {"verified": False,
                      "reason": "slot must be 'escrow' or 'bonds'"}
    tx_hash = body.get("tx_hash")
    if not x402mod.valid_txhash(tx_hash):
        return None, {"verified": False,
                      "reason": "bad tx_hash format (want 0x + 64 hex)"}
    payer_sig = body.get("payer_sig")
    if not isinstance(payer_sig, str) or not payer_sig.strip():
        return None, {"verified": False, "reason": "missing payer_sig: the "
                      "depositing wallet must EIP-191 personal_sign "
                      "binding_message(tx_hash, 'toll:<slot>:<ref>')"}
    try:
        min_uusdc = int(body.get("min_uusdc"))
    except (TypeError, ValueError):
        return None, {"verified": False,
                      "reason": "min_uusdc must be an integer micro-USDC amount"}
    if min_uusdc <= 0:
        return None, {"verified": False, "reason": "min_uusdc must be > 0"}
    ref = body.get("ref")
    if not isinstance(ref, str) or not ref.strip() or len(ref) > 128:
        return None, {"verified": False,
                      "reason": "ref must be a non-empty string (<=128 chars) "
                                "identifying the toll intent, e.g. a job id"}
    network = _network_of(body)
    if network is None:
        return None, {"verified": False,
                      "reason": "network must be 'mainnet' or 'sepolia'"}
    return {"slot": slot, "tx_hash": tx_hash.strip(), "payer_sig": payer_sig,
            "min_uusdc": min_uusdc, "ref": ref.strip(),
            "network": network}, None


def verify_deposit(slot: str, tx_hash: str, payer_sig: str, min_uusdc: int,
                   ref: str, network: str = "mainnet") -> dict:
    """READ-ONLY deposit verification. Returns a verified:true/false dict.

    network="mainnet": Base 8453, canonical native USDC, requires
        TOLL_MAINNET_AUTHORIZED=1.
    network="sepolia": Base Sepolia 84532, Circle testnet USDC, requires
        TOLL_TESTNET_SEPOLIA=1. Test funds only.

    Raises TollRefused for configuration/authorisation failures (the HTTP
    layer turns these into 403s); verification failures come back as
    verified:false, never as exceptions.
    """
    if network not in _NETWORKS:
        raise TollRefused("network must be 'mainnet' or 'sepolia'")
    net = _NETWORKS[network]
    # 1. Per-network gate: mainnet needs TOLL_MAINNET_AUTHORIZED=1,
    #    sepolia needs TOLL_TESTNET_SEPOLIA=1. Refuse loudly otherwise.
    try:
        if network == "sepolia":
            assert_toll_testnet()
        else:
            assert_toll_mainnet()
    except RuntimeError as e:
        raise TollRefused(str(e))
    # 2. Canonical USDC only on mainnet: the toll path forbids the
    #    AWL_USDC_BASE override — the vendored canonical default must be
    #    in force. (Sepolia uses its own pinned testnet contract.)
    if net["forbid_usdc_override"] and os.environ.get("AWL_USDC_BASE",
                                                      "").strip():
        raise TollRefused("AWL_USDC_BASE override is set — the toll path "
                          "requires canonical native Base USDC")
    # 3. Slot wallet configured and well-formed.
    wallet = _slot_wallet(slot, network)
    if wallet is None:
        raise TollRefused("%s is not set to a valid 0x address"
                          % net["wallets"][slot])
    # 4. Chain reader: the x402 test seam wins when installed (tests);
    #    otherwise the per-network RPC env only — no public fallback on
    #    the money path.
    rpc = getattr(x402mod, "_test_rpc", None)
    if rpc is None:
        urls = _toll_rpc_urls(network)
        if not urls:
            raise TollRefused("%s is not configured" % net["rpc_env"])
        rpc = _rpc_call(urls)
    rail = dict(x402mod.EVM_RAILS[net["rail"]])
    rail["label"] = "Base (toll)" if network == "mainnet" \
        else "Base Sepolia (toll test)"
    rail["usdc"] = net["usdc"]  # pin the toll contract; ignore overrides
    ok, info = x402mod._verify_evm(
        tx_hash, net["rail"], rail, min_uusdc, wallet,
        used_set=set(),  # read-only: Rider's UNIQUE deposit_tx_hash is the
                         # replay guard; this endpoint mutates nothing.
        rpc=rpc,
        payer_sig=payer_sig,
        resource="toll:%s:%s" % (slot, ref),
    )
    if not ok:
        out = {"verified": False, "reason": info.get("reason", "refused")}
        for k in ("replay_key", "payer", "paid_units"):
            if k in info:
                out[k] = info[k]
        return out
    return {"verified": True, "paid_uusdc": info["paid_units"],
            "payer": info["payer"], "tx_hash": info["tx"],
            "chain_id": net["chain_id"], "token": net["usdc"],
            "wallet": wallet, "replay_key": info["replay_key"],
            "network": network}


class TollRefused(Exception):
    """Configuration/authorisation refusal (HTTP 403), not a bad deposit."""


async def deposit_verify(request):
    """POST /internal/toll/deposit/verify — see module docstring."""
    if request.method != "POST":
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _authed(request):
        return JSONResponse({"verified": False,
                             "reason": "forbidden: bad or missing service "
                                       "secret"}, status_code=403)
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        return JSONResponse({"error": "body too large"}, status_code=413)
    parsed, err = _parse_body(raw)
    if err is not None:
        return JSONResponse(err)
    try:
        result = await anyio.to_thread.run_sync(
            lambda: verify_deposit(parsed["slot"], parsed["tx_hash"],
                                   parsed["payer_sig"], parsed["min_uusdc"],
                                   parsed["ref"], parsed["network"]))
    except TollRefused as e:
        return JSONResponse({"verified": False, "reason": str(e)},
                            status_code=403)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return JSONResponse({"verified": False,
                             "reason": "verifier error: %s" % str(e)[:120]})
    return JSONResponse(result)


def _parse_send_body(raw: bytes):
    """Body: {"slot", "to_address", "amount_uusdc", "idempotency_key",
    "purpose", "network"?}. Amounts and recipients are Rider's policy —
    this endpoint only enforces the toll law (auth, caps, idempotency,
    per-network USDC). network defaults to "mainnet"."""
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, {"sent": False, "reason": "body is not JSON"}
    if not isinstance(body, dict):
        return None, {"sent": False, "reason": "body must be a JSON object"}
    slot = body.get("slot")
    if slot not in _SLOTS:
        return None, {"sent": False,
                      "reason": "slot must be 'escrow' or 'bonds'"}
    to_address = body.get("to_address")
    if not isinstance(to_address, str) or not _ADDR_RE.match(to_address):
        return None, {"sent": False,
                      "reason": "bad to_address (want 0x + 40 hex)"}
    try:
        amount_uusdc = int(body.get("amount_uusdc"))
    except (TypeError, ValueError):
        return None, {"sent": False,
                      "reason": "amount_uusdc must be an integer micro-USDC"}
    if amount_uusdc <= 0:
        return None, {"sent": False, "reason": "amount_uusdc must be > 0"}
    key = body.get("idempotency_key")
    if not isinstance(key, str) or not key.strip() or len(key) > 128:
        return None, {"sent": False, "reason": "idempotency_key must be a "
                      "non-empty string (<=128 chars)"}
    purpose = body.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip() \
            or len(purpose) > 64:
        return None, {"sent": False, "reason": "purpose must be a non-empty "
                      "string (<=64 chars), e.g. escrow_release"}
    network = _network_of(body)
    if network is None:
        return None, {"sent": False,
                      "reason": "network must be 'mainnet' or 'sepolia'"}
    return {"slot": slot, "to_address": to_address,
            "amount_uusdc": amount_uusdc, "idempotency_key": key.strip(),
            "purpose": purpose.strip(), "network": network}, None


async def toll_send(request):
    """POST /internal/toll/send — sign + broadcast a toll USDC transfer.

    MOVES REAL MONEY (or testnet funds on sepolia). Guarded by: service
    secret, per-network authorization (TOLL_MAINNET_AUTHORIZED for mainnet,
    TOLL_TESTNET_SEPOLIA for sepolia), per-tx cap, daily cap, idempotency
    key, per-network-USDC-only, dedicated per-network broadcast path.
    Refusals are 200 with sent:false; misconfiguration is 403.
    """
    if request.method != "POST":
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _authed(request):
        return JSONResponse({"sent": False,
                             "reason": "forbidden: bad or missing service "
                                       "secret"}, status_code=403)
    raw = await request.body()
    if len(raw) > _MAX_BODY:
        return JSONResponse({"error": "body too large"}, status_code=413)
    parsed, err = _parse_send_body(raw)
    if err is not None:
        return JSONResponse(err)
    from .chains import toll_send_usdc
    try:
        result = await anyio.to_thread.run_sync(
            lambda: toll_send_usdc(parsed["slot"], parsed["to_address"],
                                   parsed["amount_uusdc"],
                                   parsed["idempotency_key"],
                                   network=parsed["network"]))
    except (TollRefused, RuntimeError, ValueError) as e:
        msg = str(e)
        code = 403 if msg.startswith(("REFUSED", "TOLL_")) else 200
        return JSONResponse({"sent": False, "reason": msg[:200]},
                            status_code=code)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return JSONResponse({"sent": False,
                             "reason": "send error: %s" % str(e)[:120]})
    return JSONResponse({"sent": True, "purpose": parsed["purpose"], **result})
