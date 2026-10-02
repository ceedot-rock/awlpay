#!/usr/bin/env python3
"""awLPay x402 payment verification — REAL on-chain USDC checks.

PORTED from the lab's proven rider-x402 service (~/workspace/rider-x402,
the Fly app behind https://rider-x402.fly.dev). What was carried over:

  * the X-PAYMENT header parse shape (base64url JSON, scheme / network /
    payload.{txHash, payerSig})
  * the EVM verification law: receipt exists + status ok, then USDC
    Transfer-log summation to payTo >= price — read-only, no state changes
  * the payer rule: the payer is the TOKEN SENDER from the Transfer logs,
    NOT receipt["from"] (with ERC-4337 accounts tx.from is the bundler)
  * the front-running binding: EIP-191 personal_sign by the paying address
    over binding_message(txHash, resource), with the ERC-1271
    smart-account fallback (undeployed ERC-6492 accounts get a specific
    refusal, never a silent accept)
  * the namespaced payment-hash replay key "<network>:<txhash-lower>"
  * the Solana SPL-USDC balance-delta check (jsonParsed getTransaction)

The cryptography (keccak256, secp256k1 ecrecover) is vendored UNTOUCHED
in server/ethsig.py — it is NOT reimplemented here.

What awLPay changes vs rider-x402:

  * verification is READ-ONLY: verify_payment() never mutates the used
    set. The caller (server/app.py) consumes the payment hash atomically
    only after the quote validates, so a malformed body or a refused
    quote does not burn the caller's 25c.
  * the binding message text is awLPay's own ("awlpay payment proof"),
    so a proof signed for rider-x402 cannot be replayed here and
    vice versa.
  * Base Sepolia (eip155:84532) is a first-class rail entry — testnet
    verification is a config addition, not a code change.
  * pay_to is a parameter, not a module global, so tests can point the
    verifier at any address.

HARD RULES for this module:

  * NO funds ever move here. This module never signs, never sends
    transactions, never holds keys. It only READS chain state.
  * Mainnet vs testnet is CONFIG (AWL_RPC_* / AWL_USDC_*), never a code
    branch. Nothing here points at mainnet on its own.

Env (all read at call time unless noted):
    AWL_PAY_TO            EVM address receiving USDC (default 0x0...0;
                          server warns loudly when unset)
    AWL_PAY_TO_SOL        base58 Solana address receiving SPL USDC;
                          unset disables the Solana rail
    AWL_RPC_BASE          comma-separated Base JSON-RPC URLs (required
                          for real Base verification — no silent fallback)
    AWL_RPC_BASE_SEPOLIA  comma-separated Base Sepolia JSON-RPC URLs
                          (falls back to https://sepolia.base.org)
    AWL_RPC_POLYGON / AWL_RPC_ARBITRUM / AWL_RPC_OPTIMISM
                          per-rail URLs; fall back to public endpoints
                          (documented trust assumption, same as rider-x402)
    AWL_RPC_SOLANA        comma-separated Solana JSON-RPC URLs
                          (falls back to https://solana-rpc.publicnode.com)
    AWL_USDC_BASE / AWL_USDC_BASE_SEPOLIA / AWL_USDC_POLYGON /
    AWL_USDC_ARBITRUM / AWL_USDC_OPTIMISM / AWL_USDC_SOL_MINT
                          asset-address overrides (e.g. to point a rail at
                          a testnet USDC); defaults are the canonical
                          Circle addresses baked into EVM_RAILS
    AWL_DEFAULT_NETWORK   CAIP-2 id assumed when X-PAYMENT omits network
                          (default "eip155:8453")
    AWL_X402_STATE        optional path to a JSON file persisting used
                          payment hashes across restarts (default:
                          in-memory only — a restart forgets replays)
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
import time
import urllib.request

from . import ethsig
from . import xrpl as xrplmod
from . import tron as tronmod
from . import stellar as stellarmod
from . import lightning as lnmod
from . import bitcoin as btcmod

# --------------------------------------------------------------------------
# constants (ported)
# --------------------------------------------------------------------------

# Canonical native USDC per chain, 6 decimals (Circle docs; rider-x402
# verified 2026-09-24/25). Base Sepolia USDC verified against Circle's
# testnet registry 2026-09-28. Override any of them via AWL_USDC_* env.
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a"
                  "4df523b3ef")
USDC_DECIMALS = 6
UNITS_PER_CENT = 10 ** (USDC_DECIMALS - 2)  # 10_000 units = 1 cent

X402_VERSION = 2
# Advertised scheme. In x402 "exact" nominally means an EIP-3009 signed
# authorization; we verify a txHash proof instead and say so in
# extra.paymentProof + extra.howto (same honesty call rider-x402 made in
# reverse: it advertised "txHash" because its clients were standard x402
# signers). awLPay's callers are told up front to send a txHash, so we
# advertise the scheme name they asked for ("exact") and ALSO accept
# "txHash" from rider-x402-style tooling.
PAY_SCHEME = "exact"
_ACCEPTED_SCHEMES = (None, "exact", "txHash")

BASE_MAINNET = "eip155:8453"
BASE_SEPOLIA = "eip155:84532"
SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
SOL_USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"  # 6 decimals
SOL_SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{87,88}$")

# XRPL rail (CAIP-2). v1 = XRP-native; testnet always on, mainnet gated on
# AWL_RPC_XRPL_MAINNET being set. RLUSD/IOU assets are refused by the
# verifier (v2).
XRPL_TESTNET = xrplmod.XRPL_TESTNET
XRPL_MAINNET = xrplmod.XRPL_MAINNET
XRPL_NETWORKS = (XRPL_TESTNET, XRPL_MAINNET)

# Tron rail: TRC-20 USDT (6 decimals, $1-pegged — priced like USDC).
TRON_MAINNET = tronmod.TRON_MAINNET
TRON_NILE = tronmod.TRON_NILE
TRON_NETWORKS = (TRON_NILE, TRON_MAINNET)

# Stellar rail: native XLM (7-decimal stroops).
STELLAR_TESTNET = stellarmod.STELLAR_TESTNET
STELLAR_PUBNET = stellarmod.STELLAR_PUBNET
STELLAR_NETWORKS = (STELLAR_TESTNET, STELLAR_PUBNET)

# Lightning rail: BOLT11 invoices via ZBD, preimage-proof verification.
LIGHTNING_MAINNET = lnmod.LIGHTNING_MAINNET
LIGHTNING_TESTNET = lnmod.LIGHTNING_TESTNET
LIGHTNING_NETWORKS = (LIGHTNING_MAINNET, LIGHTNING_TESTNET)

# Bitcoin rail: on-chain BTC (satoshis).
BTC_MAINNET = btcmod.BTC_MAINNET
BTC_SIGNET = btcmod.BTC_SIGNET
BTC_NETWORKS = (BTC_SIGNET, BTC_MAINNET)

# Multi-rail table: adding a rail = one entry here + its AWL_RPC_* env.
# Solana is deliberately NOT in this table — it is not EVM and its
# verification shape differs (balance deltas, no payerSig binding);
# see _verify_solana.
EVM_RAILS = {
    BASE_MAINNET: {
        "label": "Base",
        "rpc_env": "AWL_RPC_BASE",
        # No silent public fallback on the money rail: the operator must
        # configure an RPC endpoint explicitly.
        "fallback": [],
        "usdc_env": "AWL_USDC_BASE",
        "usdc": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "testnet": False,
    },
    BASE_SEPOLIA: {
        "label": "Base Sepolia",
        "rpc_env": "AWL_RPC_BASE_SEPOLIA",
        "fallback": ["https://sepolia.base.org"],
        "usdc_env": "AWL_USDC_BASE_SEPOLIA",
        "usdc": "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
        "testnet": True,
    },
    "eip155:137": {
        "label": "Polygon",
        "rpc_env": "AWL_RPC_POLYGON",
        "fallback": ["https://1rpc.io/matic",
                     "https://rpc.ankr.com/polygon"],
        "usdc_env": "AWL_USDC_POLYGON",
        "usdc": "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359",
        "testnet": False,
    },
    "eip155:42161": {
        "label": "Arbitrum One",
        "rpc_env": "AWL_RPC_ARBITRUM",
        "fallback": ["https://arb1.arbitrum.io/rpc"],
        "usdc_env": "AWL_USDC_ARBITRUM",
        "usdc": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
        "testnet": False,
    },
    "eip155:10": {
        "label": "Optimism",
        "rpc_env": "AWL_RPC_OPTIMISM",
        "fallback": ["https://1rpc.io/op",
                     "https://rpc.ankr.com/optimism"],
        "usdc_env": "AWL_USDC_OPTIMISM",
        "usdc": "0x0b2C639c533813f4Aa9D7837CAf62653d097Ff85",
        "testnet": False,
    },
    "eip155:56": {
        "label": "BNB Smart Chain",
        "rpc_env": "AWL_RPC_BSC",
        "fallback": ["https://bsc-dataseed.binance.org",
                     "https://1rpc.io/bnb"],
        "usdc_env": "AWL_USDC_BSC",
        # USDT BEP-20 (18 decimals, not 6 — see "decimals" below).
        "usdc": "0x55d398326f99059fF775485246999027B3197955",
        "token": "USDT",
        "decimals": 18,
        "testnet": False,
    },
}

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


# --------------------------------------------------------------------------
# config helpers (env read at call time so tests can repoint rails)
# --------------------------------------------------------------------------

def evm_pay_to() -> str:
    return os.environ.get("AWL_PAY_TO", "").strip() or ZERO_ADDRESS


def sol_pay_to() -> str:
    return os.environ.get("AWL_PAY_TO_SOL", "").strip()


def default_network() -> str:
    return os.environ.get("AWL_DEFAULT_NETWORK", "").strip() or BASE_MAINNET


def rail_usdc(network: str) -> str:
    """Canonical USDC for a rail, or the AWL_USDC_* override."""
    rail = EVM_RAILS[network]
    return os.environ.get(rail["usdc_env"], "").strip() or rail["usdc"]


def rail_token(network: str) -> str:
    """Token symbol for user-facing messages (default USDC)."""
    return EVM_RAILS[network].get("token", "USDC")


def rail_decimals(network: str) -> int:
    """Token decimals for a rail (default 6, e.g. BSC USDT uses 18)."""
    return int(EVM_RAILS[network].get("decimals", USDC_DECIMALS))


def units_per_cent_for(network: str) -> int:
    """Base units per cent for a rail's token (10_000 for 6-decimal)."""
    return 10 ** (rail_decimals(network) - 2)


def min_units_for(network: str, price_cents: int) -> int:
    """Minimum base-unit charge for a network.

    EVM rails price in their own token's base units (USDC 6dp on most,
    USDT 18dp on BSC). Solana SPL-USDC is 6dp. XRPL is handled separately
    via drops (see xrpl_min_drops) — never call this for XRPL networks.
    """
    if network in EVM_RAILS:
        return price_cents * units_per_cent_for(network)
    return price_cents * UNITS_PER_CENT


def sol_usdc_mint() -> str:
    return os.environ.get("AWL_USDC_SOL_MINT", "").strip() or SOL_USDC_MINT


def rail_rpcs(network: str) -> list[str]:
    """Configured RPC URLs for a rail (env first, then public fallback)."""
    rail = EVM_RAILS[network]
    urls = [u.strip() for u in os.environ.get(rail["rpc_env"], "").split(",")
            if u.strip()]
    return urls or rail["fallback"]


def solana_rpcs() -> list[str]:
    urls = [u.strip() for u in os.environ.get("AWL_RPC_SOLANA", "").split(",")
            if u.strip()]
    return urls or ["https://solana-rpc.publicnode.com"]


def xrpl_pay_to() -> str:
    """Classic r-address receiving XRP. Unset disables the XRPL rail."""
    return xrplmod.xrpl_pay_to()


def xrpl_enabled() -> bool:
    return bool(xrpl_pay_to()) and bool(xrplmod.valid_r_address(
        xrpl_pay_to()))


def tron_pay_to() -> str:
    """Base58check T-address receiving USDT. Unset disables the Tron rail."""
    return tronmod.tron_pay_to()


def tron_enabled() -> bool:
    return bool(tron_pay_to()) and bool(tronmod.valid_t_address(
        tron_pay_to()))


def stellar_pay_to() -> str:
    """Stellar G-address receiving XLM. Unset disables the Stellar rail."""
    return stellarmod.stellar_pay_to()


def stellar_enabled() -> bool:
    return bool(stellar_pay_to()) and bool(stellarmod.valid_g_address(
        stellar_pay_to()))


def lightning_enabled() -> bool:
    """Lightning rail is live iff the ZBD API key is configured."""
    return lnmod.lightning_enabled()


def btc_pay_to() -> str:
    """Bitcoin address receiving BTC. Unset disables the Bitcoin rail."""
    return btcmod.btc_pay_to()


def btc_enabled() -> bool:
    addr = btc_pay_to()
    return bool(addr) and (btcmod.valid_btc_address(addr, btcmod.BTC_MAINNET)
                           or btcmod.valid_btc_address(addr, btcmod.BTC_SIGNET))


# --------------------------------------------------------------------------
# Lightning invoice registry (payment_hash -> invoice record).
# --------------------------------------------------------------------------

_ln_invoices: dict[str, dict] = {}
_ln_invoices_lock = threading.Lock()
_ln_invoice_by_amount: dict[int, str] = {}


def _ln_prune_locked(now: float) -> None:
    dead = [h for h, rec in _ln_invoices.items()
            if rec["expires_at"] <= now]
    for h in dead:
        _ln_invoices.pop(h, None)
        for amt, hh in list(_ln_invoice_by_amount.items()):
            if hh == h:
                _ln_invoice_by_amount.pop(amt, None)


def ln_get_or_mint_invoice(amount_msats: int, description: str):
    """Return (bolt11, payment_hash_hex, expires_at), reusing a live
    invoice for the same amount when one exists."""
    now = time.time()
    with _ln_invoices_lock:
        _ln_prune_locked(now)
        h = _ln_invoice_by_amount.get(amount_msats)
        if h and h in _ln_invoices:
            rec = _ln_invoices[h]
            return rec["bolt11"], h, rec["expires_at"]
    bolt11, phash, expires_at = lnmod.create_invoice(
        amount_msats, description)
    if not bolt11:
        return None, None, None
    with _ln_invoices_lock:
        _ln_invoices[phash] = {"bolt11": bolt11,
                               "amount_msats": amount_msats,
                               "expires_at": expires_at}
        _ln_invoice_by_amount[amount_msats] = phash
    return bolt11, phash, expires_at


def ln_lookup_invoice(payment_hash_hex: str):
    """Return the invoice record for a payment hash, or None."""
    h = (payment_hash_hex or "").strip().lower()
    with _ln_invoices_lock:
        _ln_prune_locked(time.time())
        return _ln_invoices.get(h)


def configured_rails() -> list[tuple[str, dict]]:
    """Rails that can actually be verified right now (have RPC URLs)."""
    out = [(net, rail) for net, rail in EVM_RAILS.items()
           if rail_rpcs(net)]
    if sol_pay_to():
        out.append((SOLANA_NETWORK, {"label": "Solana", "testnet": False}))
    if xrpl_enabled():
        out.append((XRPL_TESTNET, {"label": "XRPL Testnet",
                                   "testnet": True}))
        if xrplmod.mainnet_urls():
            out.append((XRPL_MAINNET, {"label": "XRPL",
                                       "testnet": False}))
    if tron_enabled():
        out.append((TRON_NILE, {"label": "Tron Nile Testnet",
                                "testnet": True}))
        out.append((TRON_MAINNET, {"label": "Tron",
                                   "testnet": False}))
    if stellar_enabled():
        out.append((STELLAR_TESTNET, {"label": "Stellar Testnet",
                                      "testnet": True}))
        out.append((STELLAR_PUBNET, {"label": "Stellar",
                                     "testnet": False}))
    if lightning_enabled():
        out.append((LIGHTNING_MAINNET, {"label": "Lightning",
                                        "testnet": False}))
    if btc_enabled():
        out.append((BTC_SIGNET, {"label": "Bitcoin Signet",
                                 "testnet": True}))
        out.append((BTC_MAINNET, {"label": "Bitcoin",
                                  "testnet": False}))
    return out


# --------------------------------------------------------------------------
# binding message (awLPay's own text — NOT rider-x402's)
# --------------------------------------------------------------------------

def binding_message(tx_hash: str, resource: str) -> bytes:
    """Canonical message the payer signs to bind a txHash proof to their
    address and to the exact endpoint being called. The "awlpay" prefix
    domain-separates these signatures from rider-x402's: a proof signed
    for one service cannot be replayed at the other."""
    return ("awlpay payment proof\n"
            "txHash: %s\n"
            "resource: %s" % (tx_hash.strip().lower(),
                              resource.strip())).encode()


# --------------------------------------------------------------------------
# test seam: injectable chain reader (tests set this; production leaves None)
# --------------------------------------------------------------------------

_test_rpc = None
_test_rpc_lock = threading.Lock()


def set_test_rpc(fn) -> None:
    """Install a fake (method, params) -> response callable used INSTEAD of
    live RPC. Test-only; production must never call this."""
    global _test_rpc
    with _test_rpc_lock:
        _test_rpc = fn


def clear_test_rpc() -> None:
    global _test_rpc
    with _test_rpc_lock:
        _test_rpc = None


def get_rpc():
    with _test_rpc_lock:
        return _test_rpc


# --------------------------------------------------------------------------
# payment-hash replay store (in-memory; optional JSON persistence)
# --------------------------------------------------------------------------

_used_lock = threading.RLock()  # RLock: mark_payment_used() nests via _load_used()
_used_payments: set[str] | None = None


def _state_path() -> str:
    return os.environ.get("AWL_X402_STATE", "").strip()


def _load_used() -> set[str]:
    global _used_payments
    with _used_lock:
        if _used_payments is None:
            _used_payments = set()
            path = _state_path()
            if path:
                try:
                    with open(path) as f:
                        _used_payments = set(json.load(f).get("used", []))
                except (OSError, ValueError):
                    pass
        return _used_payments


def _save_used() -> None:
    path = _state_path()
    if not path or _used_payments is None:
        return
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"used": sorted(_used_payments)[-20000:]}, f)
        os.replace(tmp, path)
    except OSError:
        pass


def replay_key(network: str, proof: str) -> str:
    """Namespaced replay key: "<network>:<proof-lower>" (txHash) or
    "sol:<signature>" (Solana)."""
    if network == SOLANA_NETWORK:
        return "sol:" + (proof or "").strip()
    return "%s:%s" % (network, (proof or "").strip().lower())


def is_payment_used(key: str) -> bool:
    return key in _load_used()


def used_set() -> set:
    """Read accessor for the replay set, for verify_payment's read-only
    replay check. Do not mutate the returned set — consume via
    mark_payment_used()."""
    return _load_used()


def mark_payment_used(key: str) -> None:
    """Record a payment hash as consumed. Call ONLY after the payment has
    bought a successful execution (server/app.py does this atomically with
    the idempotency-key consume)."""
    with _used_lock:
        used = _load_used()
        used.add(key)
        if len(used) > 20000:
            # keep the set bounded; drop oldest-ish (sorted order is stable)
            for old in sorted(used)[:len(used) - 20000]:
                used.discard(old)
    _save_used()


def reset_used_for_tests() -> None:
    """Test-only: clear the in-memory replay set."""
    global _used_payments
    with _used_lock:
        _used_payments = set()


# --------------------------------------------------------------------------
# JSON-RPC (ported)
# --------------------------------------------------------------------------

def valid_txhash(h) -> bool:
    return bool(re.fullmatch(r"0x[0-9a-fA-F]{64}", (h or "").strip()))


def _rpc(url, method, params, timeout=25):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "awlpay-x402/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _rpc_any(method, params, urls):
    last = None
    for u in urls:
        try:
            return _rpc(u, method, params)
        except Exception as e:  # noqa: BLE001 - try next endpoint
            last = e
    raise last if last else RuntimeError("no rpc urls configured")


# --------------------------------------------------------------------------
# EVM verification (ported; READ-ONLY — never touches the used set)
# --------------------------------------------------------------------------

def _transfer_senders_to(receipt, pay_to, usdc_contract):
    """Distinct token-sender addresses on USDC Transfer logs paying pay_to.

    The payer is the address that sent the tokens, NOT receipt["from"]:
    with ERC-4337 smart accounts tx.from is the bundler, so receipt.from
    would bind the proof to the wrong address.
    """
    senders = set()
    pay_to_lc = pay_to.lower()
    for log in receipt.get("logs", []) or []:
        if not isinstance(log, dict):
            continue
        if (log.get("address") or "").lower() != usdc_contract.lower():
            continue
        topics = log.get("topics", []) or []
        if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_TOPIC.lower():
            continue
        to_addr = "0x" + (topics[2] or "")[-40:]
        if to_addr.lower() != pay_to_lc:
            continue
        from_addr = "0x" + (topics[1] or "")[-40:]
        if from_addr.startswith("0x") and len(from_addr) == 42:
            senders.add(from_addr.lower())
    return senders


_ERC1271_SELECTOR = ethsig.keccak256(b"isValidSignature(bytes32,bytes)")[:4]
_ERC1271_MAGIC_WORD = "0x1626ba7e" + "00" * 28  # bytes4 magic, ABI-padded
_ERC6492_MAGIC_SUFFIX = bytes.fromhex(
    "6492649264926492649264926492649264926492649264926492649264926492")


def _sig_raw_bytes(payer_sig):
    try:
        s = (payer_sig or "").strip()
        if s[:2] in ("0x", "0X"):
            s = s[2:]
        raw = bytes.fromhex(s)
        return raw or None
    except (ValueError, TypeError, AttributeError):
        return None


def _verify_payer_sig_contract(payer, msg, payer_sig, call):
    """ERC-1271 fallback when ecrecover did not match the token sender.

    Smart-account payers sign with personal_sign too, but the signature is
    an ERC-1271 contract signature, so ecrecover cannot recover it. Ask the
    payer contract itself via isValidSignature. Returns (ok, reason).
    """
    raw = _sig_raw_bytes(payer_sig)
    if raw is None:
        return False, "payerSig invalid: not a valid EIP-191 personal_sign signature"
    try:
        code = ((call("eth_getCode", [payer, "latest"]) or {}).get("result") or "")
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return False, "payerSig check failed: could not read payer code (%s)" % str(e)[:80]
    if code.lower() not in ("", "0x", "0x0"):
        digest = ethsig.eth_personal_message(msg)
        data = ("0x" + _ERC1271_SELECTOR.hex()
                + digest.hex()
                + (64).to_bytes(32, "big").hex()
                + len(raw).to_bytes(32, "big").hex()
                + raw.hex() + "00" * ((-len(raw)) % 32))
        try:
            resp = call("eth_call", [{"to": payer, "data": data}, "latest"]) or {}
        except Exception as e:  # noqa: BLE001 - surfaced as clean failure
            return False, "payerSig check failed: ERC-1271 call failed (%s)" % str(e)[:80]
        if ((resp.get("result") or "").lower()) == _ERC1271_MAGIC_WORD:
            return True, ""
        return False, ("payerSig rejected by the payer smart account: ERC-1271 "
                       "isValidSignature did not return the magic value")
    if raw.endswith(_ERC6492_MAGIC_SUFFIX):
        return False, ("payer is an undeployed smart account (ERC-6492): deploy "
                       "the account first, then pay from it")
    return False, ("payerSig signer does not match the paying (token-sender) "
                   "address %s; sign with the address that sent the USDC" % payer)


def _sum_usdc_to(receipt, pay_to, usdc_contract):
    total = 0
    pay_to_lc = pay_to.lower()
    for log in receipt.get("logs", []) or []:
        if not isinstance(log, dict):
            continue
        if (log.get("address") or "").lower() != usdc_contract.lower():
            continue
        topics = log.get("topics", []) or []
        if len(topics) < 3 or (topics[0] or "").lower() != TRANSFER_TOPIC.lower():
            continue
        to_addr = "0x" + (topics[2] or "")[-40:]
        if to_addr.lower() != pay_to_lc:
            continue
        try:
            total += int(log.get("data", "0x0"), 16)
        except (ValueError, TypeError):
            continue
    return total


def _verify_evm(tx_hash, network, rail, min_units, pay_to, used_set,
                rpc=None, payer_sig=None, resource=None):
    """The Base check, generalized per-chain. READ-ONLY: reports replay via
    the returned reason but never mutates used_set — the caller consumes.

    payer_sig binds the proof to the caller: an EIP-191 personal_sign by
    the token sender's address over binding_message(tx_hash, resource).
    Smart-account payers verify via ERC-1271 isValidSignature; undeployed
    (ERC-6492) accounts are refused with a specific reason. Without the
    binding anyone watching payTo could front-run someone else's txHash.
    """
    h = (tx_hash or "").strip()
    key = replay_key(network, h)
    if not valid_txhash(h):
        return False, {"reason": "bad txhash format (want 0x + 64 hex)"}
    if key in used_set:
        return False, {"reason": "replay: payment already used",
                       "replay_key": key}
    urls = rail_rpcs(network)
    call = rpc if rpc else (lambda m, p: _rpc_any(m, p, urls))
    if rpc is None and not urls:
        return False, {"reason": "no RPC configured for %s (%s); set %s"
                                 % (rail["label"], network, rail["rpc_env"])}
    try:
        resp = call("eth_getTransactionReceipt", [h])
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return False, {"reason": "rpc unreachable: %s" % str(e)[:120]}
    receipt = resp.get("result") if isinstance(resp, dict) else None
    if not receipt:
        return False, {"reason": "tx not found / not mined yet"}
    if receipt.get("status") not in ("0x1", 1):
        return False, {"reason": "tx reverted (status != ok)"}
    # Payer = the token sender from the Transfer logs, NOT receipt["from"].
    senders = _transfer_senders_to(receipt, pay_to, rail_usdc(network))
    if len(senders) > 1:
        return False, {"reason": "ambiguous payer: %d distinct token senders "
                                 "in one tx; pay from a single address"
                       % len(senders)}
    if not senders:
        return False, {"reason": "no %s transfer to %s in tx logs"
                                 % (rail_token(network), pay_to)}
    payer = next(iter(senders))
    if not payer_sig:
        return False, {"reason": "missing payerSig: bind the proof with an "
                                 "EIP-191 personal_sign by the paying address "
                                 "(see the 402 body extra.howto)",
                       "replay_key": key, "payer": payer}
    msg = binding_message(h, resource or "")
    sig_ok = False
    signer = ethsig.verify_personal_sign(msg, payer_sig)
    if signer is not None and ("0x" + signer.hex()).lower() == payer:
        sig_ok = True  # plain EOA personal_sign
    if not sig_ok:
        sig_ok, sig_reason = _verify_payer_sig_contract(payer, msg,
                                                        payer_sig, call)
    if not sig_ok:
        return False, {"reason": sig_reason, "replay_key": key,
                       "payer": payer}
    paid = _sum_usdc_to(receipt, pay_to, rail_usdc(network))
    if paid < min_units:
        dec = rail_decimals(network)
        tok = rail_token(network)
        return False, {"reason": "underpaid: got %.*f %s, need %.*f"
                                 % (dec, paid / 10 ** dec, tok,
                                    dec, min_units / 10 ** dec),
                       "paid_units": paid, "replay_key": key, "payer": payer}
    return True, {"paid_units": paid, "tx": h, "network": network,
                  "payer": payer, "replay_key": key}


# --------------------------------------------------------------------------
# Solana verification (ported; SPL USDC via jsonParsed balance deltas)
# --------------------------------------------------------------------------

def _sol_get_tx(call, sig):
    """getTransaction, retrying at a higher maxSupportedTransactionVersion
    if the node reports a newer tx version than we asked for."""
    params = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0}
    resp = call("getTransaction", [sig, params])
    if isinstance(resp, dict) and isinstance(resp.get("error"), dict) \
            and "maxSupportedTransactionVersion" in str(
                resp["error"].get("message", "")):
        params = {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 10}
        resp = call("getTransaction", [sig, params])
    return resp


def _sum_sol_usdc_to(meta, pay_to):
    """Sum positive SPL-USDC balance deltas to pay_to in a jsonParsed tx."""
    total = 0
    mint = sol_usdc_mint()
    pre = {}
    for b in meta.get("preTokenBalances") or []:
        if isinstance(b, dict) and b.get("accountIndex") is not None:
            pre[b["accountIndex"]] = b
    for b in meta.get("postTokenBalances") or []:
        if not isinstance(b, dict):
            continue
        if b.get("mint") != mint:
            continue
        if (b.get("owner") or "") != pay_to:
            continue
        pre_b = pre.get(b.get("accountIndex"), {})
        try:
            post_amt = int((b.get("uiTokenAmount") or {}).get("amount") or 0)
            pre_amt = int((pre_b.get("uiTokenAmount") or {}).get("amount")
                          or 0)
        except (ValueError, TypeError):
            continue
        if post_amt > pre_amt:
            total += post_amt - pre_amt
    return total


def _verify_solana(sig, min_units, pay_to, used_set, rpc=None):
    sig = (sig or "").strip()
    key = replay_key(SOLANA_NETWORK, sig)
    if not SOL_SIG_RE.fullmatch(sig):
        return False, {"reason": "bad signature format (want base58, 87-88 chars)"}
    if key in used_set:
        return False, {"reason": "replay: payment already used",
                       "replay_key": key}
    if not pay_to:
        return False, {"reason": "Solana rail not enabled (AWL_PAY_TO_SOL unset)"}
    call = rpc if rpc else (lambda m, p: _rpc_any(m, p, solana_rpcs()))
    try:
        resp = _sol_get_tx(call, sig)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return False, {"reason": "rpc unreachable: %s" % str(e)[:120]}
    resp = resp if isinstance(resp, dict) else {}
    tx = resp.get("result")
    if not tx:
        rpc_err = resp.get("error") or {}
        msg = rpc_err.get("message") if isinstance(rpc_err, dict) else None
        if msg:
            return False, {"reason": "rpc error: %s" % str(msg)[:120]}
        return False, {"reason": "tx not found / not finalized yet"}
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return False, {"reason": "tx failed on-chain: %s" % str(meta.get("err"))[:80]}
    paid = _sum_sol_usdc_to(meta, pay_to)
    if paid < min_units:
        return False, {"reason": "underpaid: got %.6f USDC, need %.6f"
                                 % (paid / 10 ** USDC_DECIMALS,
                                    min_units / 10 ** USDC_DECIMALS),
                       "paid_units": paid, "replay_key": key}
    return True, {"paid_units": paid, "tx": sig, "network": SOLANA_NETWORK,
                  "replay_key": key}


def verify_payment(proof, network, min_units, pay_to, used_set, rpc=None,
                   payer_sig=None, resource=None, oracle=None):
    """Verify a payment proof. Returns (ok, info).

    READ-ONLY: never mutates used_set. On success info carries
    "replay_key" — the caller consumes it via mark_payment_used() only
    after the paid execution succeeds. On failure info carries "reason"
    (and "replay_key" when the proof itself was well-formed).

    min_units is in the rail's native base unit for EVM/Solana/Tron (USDC/
    USDT base units, 6 decimals). For XRPL/Stellar/Bitcoin/Lightning it is
    converted to drops/stroops/sats/msats via the oracle (pass oracle=;
    without a price the rail refuses).
    """
    if network == SOLANA_NETWORK:
        return _verify_solana(proof, min_units, pay_to, used_set, rpc)
    if network in XRPL_NETWORKS:
        return _verify_xrpl(proof, network, min_units, pay_to, used_set,
                            rpc=rpc, oracle=oracle)
    if network in TRON_NETWORKS:
        return _verify_tron(proof, network, min_units, pay_to, used_set,
                            rpc=rpc, payer_sig=payer_sig, resource=resource)
    if network in STELLAR_NETWORKS:
        return _verify_stellar(proof, network, min_units, pay_to, used_set,
                               rpc=rpc, oracle=oracle)
    if network in LIGHTNING_NETWORKS:
        return _verify_lightning(proof, network, min_units, pay_to,
                                 used_set, oracle=oracle)
    if network in BTC_NETWORKS:
        return _verify_btc(proof, network, min_units, pay_to, used_set,
                           rpc=rpc, payer_sig=payer_sig, resource=resource,
                           oracle=oracle)
    rail = EVM_RAILS.get(network)
    if not rail:
        return False, {"reason": "unsupported network %r (supported: %s)"
                                 % (network,
                                    ", ".join(list(EVM_RAILS)
                                              + [SOLANA_NETWORK]
                                              + list(XRPL_NETWORKS)
                                              + list(TRON_NETWORKS)
                                              + list(STELLAR_NETWORKS)
                                              + list(LIGHTNING_NETWORKS)
                                              + list(BTC_NETWORKS)))}
    return _verify_evm(proof, network, rail, min_units, pay_to, used_set,
                       rpc, payer_sig=payer_sig, resource=resource)


# --------------------------------------------------------------------------
# XRPL verification (XRP-native, testnet only in v1)
# --------------------------------------------------------------------------

def xrpl_min_drops(min_usdc_units: int, oracle) -> int | None:
    """Convert a USDC-base-unit price to XRP drops via the oracle.

    1 cent = 10_000 USDC base units. Returns integer drops, or None if
    XRP has no verifiable price (then the rail refuses — never guesses).
    """
    try:
        price = oracle.get_price_usd("xrpl", "XRP") if oracle else None
    except Exception:
        price = None
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    # min_usdc_units (1e6/unit=$1) -> USD -> XRP -> drops, integer math.
    # drops = units / 1e6 ($) * 1e6 (drops/XRP) / price = units / price.
    drops = int(min_usdc_units // price)
    # Never quote zero: a sub-drop price still costs 1 drop.
    return max(drops, 1)


def _verify_xrpl(tx_hash, network, min_usdc_units, pay_to, used_set,
                 rpc=None, oracle=None):
    """XRPL leg of verify_payment. min_usdc_units is converted to drops
    via the oracle; the InvoiceID binding (not a payer signature) stops
    front-running, per the push-mode design."""
    min_drops = xrpl_min_drops(min_usdc_units, oracle)
    if min_drops is None:
        return False, {"reason": "xrpl rail: no verifiable XRP/USD price; "
                                 "cannot price the charge"}
    ok, info = xrplmod.verify_xrpl_payment(
        tx_hash, network, min_drops, pay_to, used_set,
        rpc=rpc)
    if ok:
        info["paid_units"] = info.pop("paid_drops")
        info["asset"] = "XRP"
    return ok, info


# --------------------------------------------------------------------------
# Tron verification (TRC-20 USDT, 6 decimals — priced like USDC)
# --------------------------------------------------------------------------

def _verify_tron(tx_hash, network, min_usdc_units, pay_to, used_set,
                rpc=None, payer_sig=None, resource=None):
    """Tron leg of verify_payment. USDT is 6-decimal and $1-pegged, so
    min_usdc_units passes straight through (no oracle conversion).

    The payerSig is a TIP-191 personal-message signature over the
    binding message ("awlpay payment proof\\ntxHash: <txid>\\nresource: ...")
    by the payer's Tron address, verified on-chain address recovery —
    signature over the same binding_message text, stopping front-running
    exactly like the EVM payerSig.
    """
    ok, info = tronmod.verify_tron_payment(
        tx_hash, network, min_usdc_units, pay_to, used_set,
        rpc=rpc, payer_sig=payer_sig, resource=resource or "")
    if ok:
        info["asset"] = "USDT"
    return ok, info


# --------------------------------------------------------------------------
# Stellar verification (native XLM, stroops via the oracle)
# --------------------------------------------------------------------------

def stellar_min_stroops(min_usdc_units: int, oracle) -> int | None:
    """Convert a USDC-base-unit price to XLM stroops via the oracle.

    1 cent = 10_000 USDC base units. stroops = units / 1e6 ($) * 1e7
    (stroops/XLM) / price = units * 10 / price. Integer math, never zero.
    Returns None if XLM has no verifiable price (rail refuses).
    """
    try:
        price = oracle.get_price_usd("stellar", "XLM") if oracle else None
    except Exception:
        price = None
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    stroops = int(min_usdc_units * 10 // price)
    return max(stroops, 1)


def _verify_stellar(tx_hash, network, min_usdc_units, pay_to, used_set,
                    rpc=None, oracle=None):
    """Stellar leg of verify_payment. min_usdc_units is converted to
    stroops via the oracle; the memo-hash binding (not a payer signature)
    stops front-running, like the XRPL InvoiceID."""
    min_stroops = stellar_min_stroops(min_usdc_units, oracle)
    if min_stroops is None:
        return False, {"reason": "stellar rail: no verifiable XLM/USD price; "
                                 "cannot price the charge"}
    ok, info = stellarmod.verify_stellar_payment(
        tx_hash, network, min_stroops, pay_to, used_set,
        rpc=rpc)
    if ok:
        info["paid_units"] = info.pop("paid_stroops")
        info["asset"] = "XLM"
    return ok, info


# --------------------------------------------------------------------------
# Lightning verification (BOLT11, preimage proof)
# --------------------------------------------------------------------------

def lightning_min_msats(min_usdc_units: int, oracle) -> int | None:
    """Convert a USDC-base-unit price to millisatoshis via the oracle BTC
    price. 1 cent = 10_000 units. msats = units/1e6 * 1e11 / price
    = units * 100_000 / price. Integer math, never zero. Returns None if
    BTC has no verifiable price (rail refuses)."""
    try:
        price = oracle.get_price_usd("bitcoin", "BTC") if oracle else None
    except Exception:
        price = None
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    msats = int(min_usdc_units * 100_000 // price)
    return max(msats, 1)


def _verify_lightning(proof, network, min_usdc_units, pay_to,
                      used_set, oracle=None):
    """Lightning leg of verify_payment.

    proof is the 64-hex preimage; the payment_hash comes from the
    X-PAYMENT payload (parsed by parse_x_payment into proof context).
    Here proof is (preimage_hex, payment_hash_hex) — see parse_x_payment.
    The invoice record (amount, expiry) is looked up from the registry
    populated by payment_terms.
    """
    if not isinstance(proof, (tuple, list)) or len(proof) != 2:
        return False, {"reason": "lightning proof must be "
                                 "(preimage, paymentHash)"}
    preimage_hex, payment_hash_hex = proof
    rec = ln_lookup_invoice(payment_hash_hex)
    if rec is None:
        return False, {"reason": "unknown or expired lightning invoice "
                                 "(pay the BOLT11 from a fresh 402)"}
    min_msats = lightning_min_msats(min_usdc_units, oracle)
    if min_msats is None:
        return False, {"reason": "lightning rail: no verifiable BTC/USD "
                                 "price; cannot price the charge"}
    ok, info = lnmod.verify_lightning_payment(
        preimage_hex, payment_hash_hex, rec["amount_msats"], min_msats,
        rec["expires_at"], used_set)
    if ok:
        info["paid_units"] = info.pop("amount_msats")
        info["asset"] = "BTC"
    return ok, info


# --------------------------------------------------------------------------
# Bitcoin verification (on-chain BTC, sats via the oracle)
# --------------------------------------------------------------------------

def btc_min_sats(min_usdc_units: int, oracle) -> int | None:
    """Convert a USDC-base-unit price to satoshis via the oracle BTC price.

    1 cent = 10_000 units. sats = units / 1e6 ($) * 1e8 (sats/BTC) / price
    = units * 100 / price. Integer math, never zero. Returns None if BTC
    has no verifiable price (rail refuses).
    """
    try:
        price = oracle.get_price_usd("bitcoin", "BTC") if oracle else None
    except Exception:
        price = None
    if not isinstance(price, (int, float)) or price <= 0:
        return None
    sats = int(min_usdc_units * 100 // price)
    return max(sats, 1)


def _verify_btc(tx_hash, network, min_usdc_units, pay_to, used_set,
               rpc=None, payer_sig=None, resource=None, oracle=None):
    """Bitcoin leg of verify_payment. min_usdc_units is converted to sats
    via the oracle; the payerSig binding is a Bitcoin message signature
    ("Bitcoin Signed Message") by one of the tx's input addresses,
    stopping front-running like the EVM payerSig."""
    min_sats = btc_min_sats(min_usdc_units, oracle)
    if min_sats is None:
        return False, {"reason": "bitcoin rail: no verifiable BTC/USD price; "
                                 "cannot price the charge"}
    ok, info = btcmod.verify_btc_payment(
        tx_hash, network, min_sats, pay_to, used_set,
        rpc=rpc, payer_sig=payer_sig, resource=resource or "")
    if ok:
        info["paid_units"] = info.pop("paid_sats")
        info["asset"] = "BTC"
    return ok, info


# --------------------------------------------------------------------------
# X-PAYMENT header parsing (ported)
# --------------------------------------------------------------------------

def parse_x_payment(header_value):
    """Extract (proof, network, payer_sig) from the X-PAYMENT header.

    Returns (proof, network, payer_sig, error). proof is a txHash on EVM
    rails and a base58 signature on Solana. payer_sig is the EIP-191
    binding signature on EVM rails (None on Solana). Missing network
    falls back to AWL_DEFAULT_NETWORK (Base mainnet).
    """
    if not header_value:
        return None, None, None, "missing X-PAYMENT header"
    try:
        padded = header_value.strip() + "=" * (-len(header_value.strip()) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    except (binascii.Error, ValueError, UnicodeDecodeError) as e:
        return None, None, None, \
            "X-PAYMENT is not valid base64url JSON: %s" % str(e)[:80]
    if not isinstance(payload, dict):
        return None, None, None, "X-PAYMENT payload must be a JSON object"
    scheme = payload.get("scheme")
    if scheme not in _ACCEPTED_SCHEMES:
        return None, None, None, (
            "unsupported scheme %r: send \"exact\" (or \"txHash\") with a "
            "txHash proof — see the 402 body extra.howto" % (scheme,))
    net = payload.get("network") or default_network()
    if net == "base":
        net = BASE_MAINNET
    inner = payload.get("payload") if isinstance(payload.get("payload"), dict) \
        else payload
    if net in EVM_RAILS:
        txh = inner.get("txHash") or inner.get("tx_hash")
        if not txh:
            return None, None, None, "X-PAYMENT payload needs payload.txHash"
        # Front-running protection: the proof must be bound to the payer.
        psig = inner.get("payerSig") or inner.get("payer_sig")
        if not psig:
            return None, None, None, (
                "X-PAYMENT payload needs payload.payerSig: EIP-191 "
                "personal_sign by the paying address over "
                "\"awlpay payment proof\\ntxHash: <0x... lowercase>\\n"
                "resource: <the https URL you are calling>\" "
                "(see the 402 body extra.howto). This stops anyone "
                "replaying your txHash ahead of you.")
        return txh, net, psig, None
    if net == SOLANA_NETWORK:
        if not sol_pay_to():
            return None, None, None, "Solana rail not enabled on this server"
        sig = inner.get("signature")
        if not sig:
            return None, None, None, "X-PAYMENT payload needs payload.signature"
        return sig, net, None, None
    if net in XRPL_NETWORKS:
        if not xrpl_enabled():
            return None, None, None, "XRPL rail not enabled on this server"
        txh = inner.get("txHash") or inner.get("tx_hash")
        if not txh:
            return None, None, None, "X-PAYMENT payload needs payload.txHash"
        # No payerSig on XRPL: the push-mode binding is the InvoiceID on
        # the Payment transaction (must equal extra.invoiceId from the
        # 402 body). A bare txHash is replayable otherwise.
        return txh, net, None, None
    if net in TRON_NETWORKS:
        if not tron_enabled():
            return None, None, None, "Tron rail not enabled on this server"
        txh = inner.get("txHash") or inner.get("tx_hash")
        if not txh:
            return None, None, None, "X-PAYMENT payload needs payload.txHash"
        # TIP-191 payerSig (TronWeb signMessageV2) binds the proof to the
        # payer, exactly like the EVM payerSig.
        psig = inner.get("payerSig") or inner.get("payer_sig")
        if not psig:
            return None, None, None, (
                "X-PAYMENT payload needs payload.payerSig: TIP-191 "
                "personal-message signature by the paying Tron address "
                "over \"awlpay payment proof\\ntxHash: <64-hex txid>\\n"
                "resource: <the https URL you are calling>\" "
                "(see the 402 body extra.howto). This stops anyone "
                "replaying your txid ahead of you.")
        return txh, net, psig, None
    if net in STELLAR_NETWORKS:
        if not stellar_enabled():
            return None, None, None, "Stellar rail not enabled on this server"
        txh = inner.get("txHash") or inner.get("tx_hash")
        if not txh:
            return None, None, None, "X-PAYMENT payload needs payload.txHash"
        # No payerSig on Stellar: the push-mode binding is the memo hash on
        # the payment (must equal extra.memoHash from the 402 body). A bare
        # txHash is replayable otherwise.
        return txh, net, None, None
    if net in LIGHTNING_NETWORKS:
        if not lightning_enabled():
            return None, None, None, "Lightning rail not enabled on this server"
        preimage = inner.get("preimage")
        phash = inner.get("paymentHash") or inner.get("payment_hash")
        if not preimage or not phash:
            return None, None, None, (
                "X-PAYMENT payload needs payload.preimage and "
                "payload.paymentHash: pay the BOLT11 invoice from the 402 "
                "body, then submit the 32-byte preimage (64 hex) your "
                "wallet reveals on settlement.")
        # Proof is the (preimage, paymentHash) pair; _verify_lightning
        # checks SHA256(preimage) == paymentHash against the registry.
        return (preimage, phash), net, None, None
    if net in BTC_NETWORKS:
        if not btc_enabled():
            return None, None, None, "Bitcoin rail not enabled on this server"
        txh = inner.get("txHash") or inner.get("tx_hash")
        if not txh:
            return None, None, None, "X-PAYMENT payload needs payload.txHash"
        # Bitcoin message-signature payerSig binds the proof to one of the
        # tx's input addresses, exactly like the EVM payerSig.
        psig = inner.get("payerSig") or inner.get("payer_sig")
        if not psig:
            return None, None, None, (
                "X-PAYMENT payload needs payload.payerSig: Bitcoin message "
                "signature (\"Bitcoin Signed Message\") by one of the "
                "paying transaction's input addresses over "
                "\"awlpay payment proof\\ntxHash: <64-hex lowercase>\\n"
                "resource: <the https URL you are calling>\" "
                "(see the 402 body extra.howto). This stops anyone "
                "replaying your txid ahead of you.")
        return txh, net, psig, None
    return None, None, None, ("unsupported network %r (supported: %s)"
                              % (net, ", ".join(list(EVM_RAILS)
                                                + [SOLANA_NETWORK]
                                                + list(XRPL_NETWORKS)
                                                + list(TRON_NETWORKS)
                                                + list(STELLAR_NETWORKS)
                                                + list(LIGHTNING_NETWORKS)
                                                + list(BTC_NETWORKS))))


# --------------------------------------------------------------------------
# 402 payment terms (machine-readable)
# --------------------------------------------------------------------------

def _evm_howto(price_cents, rail, network, pay_to, resource):
    units = price_cents * units_per_cent_for(network)
    tok = rail.get("token", "USDC")
    return ("1) transfer >= %d %s base units (%d cents) of native %s on "
            "%s to %s  "
            "2) sign this EXACT text with the paying address "
            "(EIP-191 personal_sign, e.g. ethers signMessage / "
            "MetaMask personal_sign):\n"
            "awlpay payment proof\\n"
            "txHash: <your 0x txhash, lowercase>\\n"
            "resource: %s  "
            "3) retry the request with header "
            "X-PAYMENT: base64url(JSON({\"x402Version\":2,"
            "\"scheme\":\"exact\",\"network\":%s,"
            "\"payload\":{\"txHash\":\"0x...\",\"payerSig\":\"0x...\"}})). "
            "The payerSig binds the proof to you so nobody can "
            "front-run your txHash. Sign with the address that sent the "
            "%s (for ERC-4337 smart accounts that is the token sender, "
            "not the bundler); contract wallets verify via ERC-1271."
            % (units, tok, price_cents, tok, rail["label"], pay_to, resource,
               json.dumps(network), tok))


def payment_terms(host: str, price_cents: int, reason: str | None = None,
                  oracle=None) -> dict:
    """Machine-readable 402 body: x402 PaymentRequirements listing every
    rail that can actually be verified right now."""
    units = price_cents * UNITS_PER_CENT
    resource = "https://%s/api/pay/execute" % host
    accepts = []
    pay_to = evm_pay_to()
    for network, rail in configured_rails():
        if network == SOLANA_NETWORK:
            accepts.append({
                "scheme": PAY_SCHEME,
                "network": network,
                "amount": str(units),
                "asset": sol_usdc_mint(),
                "payTo": sol_pay_to(),
                "resource": resource,
                "description": ("awLPay v1 /api/pay/execute — %d cents SPL "
                                "USDC on Solana per execution"
                                % price_cents),
                "mimeType": "application/json",
                "maxTimeoutSeconds": 300,
                "extra": {
                    "paymentProof": "signature",
                    "howto": ("1) transfer >= %d USDC base units SPL USDC "
                              "to %s  2) retry with header X-PAYMENT: "
                              "base64url(JSON({\"x402Version\":2,"
                              "\"scheme\":\"exact\",\"network\":%s,"
                              "\"payload\":{\"signature\":\"<base58>\"}}))"
                              % (units, sol_pay_to(),
                                 json.dumps(SOLANA_NETWORK))),
                },
            })
            continue
        if network in XRPL_NETWORKS:
            xrp_pay_to = xrpl_pay_to()
            drops = (xrpl_min_drops(units, oracle)
                     if oracle else None)
            if drops is None:
                continue  # no XRP price -> rail stays unadvertised
            inv = xrplmod.invoice_id(xrp_pay_to, str(drops), network)
            accepts.append({
                "scheme": PAY_SCHEME,
                "network": network,
                "amount": str(drops),
                "asset": "XRP",
                "payTo": xrp_pay_to,
                "resource": resource,
                "description": ("awLPay v1 /api/pay/execute — %d cents in "
                                "XRP (%d drops) on XRPL testnet per "
                                "execution" % (price_cents, drops)),
                "mimeType": "application/json",
                "maxTimeoutSeconds": 300,
                "extra": {
                    "paymentProof": "txHash",
                    "invoiceId": inv,
                    "howto": ("1) send a Payment of >= %d drops XRP to %s "
                              "on the XRPL testnet with InvoiceID=%s  "
                              "2) retry with header X-PAYMENT: "
                              "base64url(JSON({\"x402Version\":2,"
                              "\"scheme\":\"exact\",\"network\":%s,"
                              "\"payload\":{\"txHash\":\"<64-hex, no 0x>\"}})). "
                              "The InvoiceID binds the payment to this "
                              "challenge so nobody can front-run your tx "
                              "hash. Testnet only."
                              % (drops, xrp_pay_to, inv,
                                 json.dumps(network))),
                },
            })
            continue
        if network in TRON_NETWORKS:
            tron_to = tron_pay_to()
            usdt_contract = tronmod.usdt_contract_for(network)
            tlabel = ("Tron Nile testnet" if network == TRON_NILE
                      else "Tron")
            accepts.append({
                "scheme": PAY_SCHEME,
                "network": network,
                "amount": str(units),
                "asset": usdt_contract,
                "payTo": tron_to,
                "resource": resource,
                "description": ("awLPay v1 /api/pay/execute — %d cents in "
                                "USDT (%d base units) on %s per execution"
                                % (price_cents, units, tlabel)),
                "mimeType": "application/json",
                "maxTimeoutSeconds": 300,
                "extra": {
                    "paymentProof": "txHash",
                    "howto": ("1) transfer >= %d USDT base units (%d cents) "
                              "TRC-20 USDT to %s on %s  "
                              "2) sign this EXACT text with the paying "
                              "address (TIP-191, TronWeb signMessageV2 / "
                              "TronLink):\n"
                              "awlpay payment proof\\n"
                              "txHash: <64-hex txid>\\n"
                              "resource: %s  "
                              "3) retry with header X-PAYMENT: "
                              "base64url(JSON({\"x402Version\":2,"
                              "\"scheme\":\"exact\",\"network\":%s,"
                              "\"payload\":{\"txHash\":\"<64-hex>\","
                              "\"payerSig\":\"<hex>\"}})). "
                              "The payerSig binds the proof to you so nobody "
                              "can front-run your txid."
                              % (units, price_cents, tron_to, tlabel,
                                 resource, json.dumps(network))),
                },
            })
            continue
        if network in STELLAR_NETWORKS:
            xlm_pay_to = stellar_pay_to()
            stroops = (stellar_min_stroops(units, oracle)
                       if oracle else None)
            if stroops is None:
                continue  # no XLM price -> rail stays unadvertised
            mid = stellarmod.memo_id(xlm_pay_to, str(stroops), network)
            memo_b64 = base64.b64encode(bytes.fromhex(mid)).decode()
            tlabel = ("Stellar testnet" if network == STELLAR_TESTNET
                      else "Stellar")
            accepts.append({
                "scheme": PAY_SCHEME,
                "network": network,
                "amount": str(stroops),
                "asset": "XLM",
                "payTo": xlm_pay_to,
                "resource": resource,
                "description": ("awLPay v1 /api/pay/execute — %d cents in "
                                "XLM (%d stroops) on %s per execution"
                                % (price_cents, stroops, tlabel)),
                "mimeType": "application/json",
                "maxTimeoutSeconds": 300,
                "extra": {
                    "paymentProof": "txHash",
                    "memoHash": memo_b64,
                    "howto": ("1) send a payment of >= %d stroops XLM to %s "
                              "on %s with a HASH memo of %s  "
                              "2) retry with header X-PAYMENT: "
                              "base64url(JSON({\"x402Version\":2,"
                              "\"scheme\":\"exact\",\"network\":%s,"
                              "\"payload\":{\"txHash\":\"<64-hex>\"}})). "
                              "The memo hash binds the payment to this "
                              "challenge so nobody can front-run your tx "
                              "hash."
                              % (stroops, xlm_pay_to, tlabel, memo_b64,
                                 json.dumps(network))),
                },
            })
            continue
        if network in LIGHTNING_NETWORKS:
            msats = (lightning_min_msats(units, oracle)
                     if oracle else None)
            if msats is None:
                continue  # no BTC price -> rail stays unadvertised
            bolt11, phash, expires_at = ln_get_or_mint_invoice(
                msats, "awLPay execute")
            if not bolt11:
                continue  # ZBD unreachable/key missing -> skip quietly
            accepts.append({
                "scheme": PAY_SCHEME,
                "network": network,
                "amount": str(msats),
                "asset": "BTC",
                "payTo": bolt11,
                "resource": resource,
                "description": ("awLPay v1 /api/pay/execute — %d cents in "
                                "BTC (%d msats) over Lightning per execution"
                                % (price_cents, msats)),
                "mimeType": "application/json",
                "maxTimeoutSeconds": 300,
                "extra": {
                    "paymentProof": "preimage",
                    "invoice": bolt11,
                    "paymentHash": phash,
                    "howto": ("1) pay this BOLT11 invoice (%d msats) with "
                              "any Lightning wallet  "
                              "2) take the 32-byte preimage your wallet "
                              "reveals on settlement and retry with header "
                              "X-PAYMENT: base64url(JSON({\"x402Version\":2,"
                              "\"scheme\":\"exact\",\"network\":%s,"
                              "\"payload\":{\"preimage\":\"<64-hex>\","
                              "\"paymentHash\":%s}})). "
                              "The preimage cryptographically proves you "
                              "paid; it cannot be replayed."
                              % (msats, json.dumps(network),
                                 json.dumps(phash))),
                },
            })
            continue
        if network in BTC_NETWORKS:
            btc_to = btc_pay_to()
            sats = (btc_min_sats(units, oracle)
                    if oracle else None)
            if sats is None:
                continue  # no BTC price -> rail stays unadvertised
            tlabel = ("Bitcoin signet" if network == BTC_SIGNET
                      else "Bitcoin")
            accepts.append({
                "scheme": PAY_SCHEME,
                "network": network,
                "amount": str(sats),
                "asset": "BTC",
                "payTo": btc_to,
                "resource": resource,
                "description": ("awLPay v1 /api/pay/execute — %d cents in "
                                "BTC (%d sats) on %s per execution"
                                % (price_cents, sats, tlabel)),
                "mimeType": "application/json",
                "maxTimeoutSeconds": 300,
                "extra": {
                    "paymentProof": "txHash",
                    "howto": ("1) send >= %d sats BTC to %s on %s  "
                              "2) sign this EXACT text with one of the "
                              "paying transaction's input addresses "
                              "(Bitcoin message signature):\n"
                              "awlpay payment proof\\n"
                              "txHash: <64-hex txid, lowercase>\\n"
                              "resource: %s  "
                              "3) retry with header X-PAYMENT: "
                              "base64url(JSON({\"x402Version\":2,"
                              "\"scheme\":\"exact\",\"network\":%s,"
                              "\"payload\":{\"txHash\":\"<64-hex>\","
                              "\"payerSig\":\"<base64>\"}})). "
                              "The payerSig binds the proof to you so nobody "
                              "can front-run your txid. 0-conf accepted "
                              "with RBF screen; 1 confirmation settles."
                              % (sats, btc_to, tlabel,
                                 resource, json.dumps(network))),
                },
            })
            continue
        evm_units = price_cents * units_per_cent_for(network)
        accepts.append({
            "scheme": PAY_SCHEME,
            "network": network,
            "amount": str(evm_units),
            "asset": rail_usdc(network),
            "payTo": pay_to,
            "resource": resource,
            "description": ("awLPay v1 /api/pay/execute — %d cents native "
                            "%s on %s per execution (route price)"
                            % (price_cents, rail_token(network),
                               rail["label"])),
            "mimeType": "application/json",
            "maxTimeoutSeconds": 300,
            "extra": {
                "paymentProof": "txHash",
                "howto": _evm_howto(price_cents, rail, network, pay_to,
                                    resource),
            },
        })
    if accepts:
        error = ("payment required: pay %d cents USDC on a supported rail, "
                 "then retry with X-PAYMENT" % price_cents)
    else:
        error = ("payment required, but this server has no payment rail "
                 "configured (operator: set AWL_RPC_BASE and AWL_PAY_TO)")
    body = {
        "x402Version": X402_VERSION,
        "error": error,
        "accepts": accepts,
    }
    if reason:
        body["reason"] = reason
    return body
