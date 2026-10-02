#!/usr/bin/env python3
"""awLPay Tron rail — receive-side helpers for x402 payments in TRC-20 USDT.

HARD RULES for this module:

  * NILE FIRST. The default RPCs below serve Tron mainnet
    (tron:0) and the Nile testnet (tron:1). TronGrid's public
    endpoints are rate-limited and unauthenticated — operators should
    set AWL_RPC_TRON / AWL_RPC_TRON_NILE to their own nodes or API
    keys before any production volume.
  * NO funds ever move here. This module never signs, never sends
    transactions, never holds keys. It only READS ledger state via
    the public HTTP API (plus address math and signature recovery,
    which are pure). Any E2E sending lives in a throwaway manual
    script, never here.
  * USDT amounts are integer units end-to-end (1 USDT = 1_000_000
    units, 6 decimals). Never floats. Integer math only.
  * Every proof MUST carry a payerSig: the token sender signs
    binding_message(tx_hash, resource) (see below). Without the
    binding signature the proof is refused — this stops a watcher
    from replaying someone else's tx at a different endpoint.

TRON MESSAGE-SIGNING PREFIX (verified from documentation, NOT a guess):

    digest = keccak256(b"\\x19TRON Signed Message:\\n" + len(msg) + msg)

  Sources: OpenZeppelin tron-contracts changeset (renames
  MessageHashUtils.toEthSignedMessageHash to toTronSignedMessageHash,
  switches the ERC-191 prefix "\\x19Ethereum Signed Message:\\n" to the
  TRON one per TIP-191, "matches the digest produced by native TRON
  wallet tooling (TronWeb signMessage / signMessageV2, TronLink)");
  agntn/keys docs ("signMessage frames the message the TIP-191 way,
  ... hashes it with Keccak-256 and signs with secp256k1. That's the
  digest TronWeb's signMessageV2 and TronLink sign"); ranjbar-dev
  hd-wallet ("Tron TIP-191 message signing ... 65-byte R|S|V,
  V in {27,28}. Matches TronWeb trx.signMessageV2; uses
  keccak256(\"\\x19TRON Signed Message:\\n32\" || keccak256(msg))").

  Signatures made with the EIP-191 Ethereum prefix will NOT verify
  here — that is correct behavior, not a bug: a Tron wallet and an
  Ethereum wallet signing the same message produce different
  digests, and each chain's tooling rejects the other's.

  A 64-byte (r||s, no v) signature is refused — without v the
  recovered key is ambiguous (two candidates), so it cannot bind
  safely. 65-byte (r||s||v, what TronWeb signMessageV2 returns) is
  the expected form. v must be 27/28 or 0/1.

RPC goes through urllib (stdlib), not a Tron SDK: no tronpy
dependency is wanted here (stdlib base58check + keccak256 from
server.ethsig cover everything).

Env (read at call time):
    AWL_PAY_TO_TRON    T-address receiving USDT (unset disables the
                       Tron rail in x402.payment_terms)
    AWL_RPC_TRON       comma-separated Tron mainnet HTTP API URLs
                       (default https://api.trongrid.io)
    AWL_RPC_TRON_NILE  comma-separated Nile testnet HTTP API URLs
                       (default https://nile.trongrid.io)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import urllib.request

from . import ethsig

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

TRON_MAINNET = "tron:0"
TRON_NILE = "tron:1"

USDT_DECIMALS = 6
UNITS_PER_USDT = 1_000_000

MAINNET_RPC_DEFAULT = ["https://api.trongrid.io"]
NILE_RPC_DEFAULT = ["https://nile.trongrid.io"]

# USDT (TRC-20) contract addresses, hex form (0x41 prefix + 20 bytes).
# Mainnet: the canonical TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t.
USDT_CONTRACT_MAINNET = "41a614f803b6fd780986a42c78ec9c7f77e6ded13c"
# Nile testnet: TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf — CONFIRMED
# 2026-10-02: listed as the USDT TRC-20 on the official Nile faucet
# page (nileex.io/join/getJoinPage, "Get 1000 USDT test tokens"),
# and /wallet/getcontract on nile.trongrid.io returns a live
# "TetherToken" contract at this address (bytecode + full ABI).
USDT_CONTRACT_NILE = "41eca9bc828a3005b9a3b909f2cc5c2a54794de05f"

# ERC-20 Transfer event topic (TRC-20 uses the same ABI):
# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = ("ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628"
                  "f55a4df523b3ef")

# Tron message-signing prefix (TIP-191). See module docstring.
TRON_SIGNED_MESSAGE_PREFIX = b"\x19TRON Signed Message:\n"

# Tron txids: 64 hex chars, no 0x prefix.
TX_HASH_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

# base58 alphabet (Bitcoin/Tron), hand-rolled — no dependency.
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def tron_pay_to() -> str:
    return os.environ.get("AWL_PAY_TO_TRON", "").strip()


def tron_rpcs() -> list[str]:
    urls = [u.strip() for u in os.environ.get("AWL_RPC_TRON", "").split(",")
            if u.strip()]
    return urls or list(MAINNET_RPC_DEFAULT)


def nile_rpcs() -> list[str]:
    urls = [u.strip()
            for u in os.environ.get("AWL_RPC_TRON_NILE", "").split(",")
            if u.strip()]
    return urls or list(NILE_RPC_DEFAULT)


def rpc_urls_for(network: str) -> list[str]:
    if network == TRON_MAINNET:
        return tron_rpcs()
    return nile_rpcs()


def usdt_contract_for(network: str) -> str:
    """USDT contract hex (with 41 prefix) for the network."""
    if network == TRON_MAINNET:
        return USDT_CONTRACT_MAINNET
    return USDT_CONTRACT_NILE


# --------------------------------------------------------------------------
# base58check (stdlib only: sha256 + hand-rolled base58)
# --------------------------------------------------------------------------

def _b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    # leading zero bytes -> leading '1's
    pad = 0
    for b in data:
        if b == 0:
            pad += 1
        else:
            break
    return "1" * pad + (out or "1")


def _b58decode(s: str) -> bytes:
    if not isinstance(s, str) or not s:
        raise ValueError("empty base58 input")
    n = 0
    for ch in s:
        d = _B58_ALPHABET.find(ch)
        if d < 0:
            raise ValueError("bad base58 char %r" % (ch,))
        n = n * 58 + d
    raw = n.to_bytes((n.bit_length() + 7) // 8 or 1, "big")
    pad = 0
    for ch in s:
        if ch == "1":
            pad += 1
        else:
            break
    return b"\x00" * pad + raw


def _check(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()[:4]


def b58check_encode(payload: bytes) -> str:
    """payload -> base58check string (Tron T-addresses are this)."""
    return _b58encode(payload + _check(payload))


def b58check_decode(s: str) -> bytes:
    """base58check string -> payload. Raises on bad checksum/charset."""
    raw = _b58decode(s)
    if len(raw) < 5:
        raise ValueError("too short for base58check")
    payload, cksum = raw[:-4], raw[-4:]
    if _check(payload) != cksum:
        raise ValueError("base58check checksum mismatch")
    return payload


# --------------------------------------------------------------------------
# pure helpers: Tron addresses
# --------------------------------------------------------------------------

def hex_to_t_address(addr_hex: str) -> str:
    """'41' + 20-byte hex -> T-address. Raises on malformed input."""
    h = (addr_hex or "").strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    raw = bytes.fromhex(h)
    if len(raw) != 21 or raw[0] != 0x41:
        raise ValueError("want 21 bytes with 0x41 prefix, got %r"
                         % (addr_hex,))
    return b58check_encode(raw)


def t_address_to_hex(addr: str) -> str:
    """T-address -> '41' + 20-byte hex. Raises on malformed input."""
    payload = b58check_decode((addr or "").strip())
    if len(payload) != 21 or payload[0] != 0x41:
        raise ValueError("not a Tron T-address: %r" % (addr,))
    return payload.hex()


def valid_t_address(addr) -> bool:
    """True iff addr is a well-formed Tron T-address (21 bytes,
    0x41 prefix, valid base58check checksum)."""
    if not isinstance(addr, str):
        return False
    try:
        t_address_to_hex(addr)
        return True
    except (ValueError, TypeError):
        return False


def valid_tx_hash(h) -> bool:
    return bool(isinstance(h, str) and TX_HASH_RE.fullmatch(h.strip()))


def pubkey_point_to_t_address(point) -> str:
    """secp256k1 (x, y) point -> Tron T-address.

    Tron address = 0x41 ++ keccak256(uncompressed_pubkey)[12:],
    the same 20-byte payload Ethereum derives, with Tron's 0x41
    version byte. Verified against the known pair
    TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t =
    41a614f803b6fd780986a42c78ec9c7f77e6ded13c (mainnet USDT).
    """
    x, y = point
    payload = ethsig.keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[12:]
    return hex_to_t_address("41" + payload.hex())


def binding_message(tx_hash: str, resource: str) -> bytes:
    """Canonical message the payer signs to bind a txHash proof to
    their address and to the exact endpoint being called.

    DUPLICATE of x402.binding_message — copied (not imported) to
    avoid a circular import (x402 will import this module for the
    Tron rail). Keep the two byte-identical; the "awlpay" prefix
    domain-separates these signatures from rider-x402's.
    """
    return ("awlpay payment proof\n"
            "txHash: %s\n"
            "resource: %s" % (tx_hash.strip().lower(),
                              resource.strip())).encode()


# --------------------------------------------------------------------------
# Tron TIP-191 signature verification (pure Python, via server.ethsig)
# --------------------------------------------------------------------------

def tron_prefixed_hash(message: bytes) -> bytes:
    """keccak256(b"\\x19TRON Signed Message:\\n" + len(message) + message).

    Matches the digest TronWeb trx.signMessage / signMessageV2 and
    TronLink sign (TIP-191)."""
    return ethsig.keccak256(TRON_SIGNED_MESSAGE_PREFIX
                            + str(len(message)).encode() + message)


def verify_tron_sig(message: bytes, sig_hex: str):
    """Recover the signer of a Tron TIP-191 message signature.

    Returns the signer's T-address string on success, None on
    failure. sig_hex: 65-byte r||s||v (v in {27,28} or {0,1}, what
    TronWeb signMessageV2 returns); 0x prefix optional. Low-s
    enforced, matching what Tron wallets produce. 64-byte (v-less)
    signatures are refused — without v the recovered key is
    ambiguous (two candidates), so they cannot bind safely.
    """
    try:
        s = (sig_hex or "").strip()
        if s.startswith(("0x", "0X")):
            s = s[2:]
        raw = bytes.fromhex(s)
        if len(raw) != 65:
            return None
        r = int.from_bytes(raw[0:32], "big")
        sv = int.from_bytes(raw[32:64], "big")
        v = raw[64]
        recids = ((v - 27,) if v >= 27 else (v,))
        if not all(rid in (0, 1) for rid in recids):
            return None
        if not (1 <= r < ethsig._N) or not (1 <= sv < ethsig._N):
            return None
        # malleability guard: wallets emit low-s
        if sv > ethsig._N // 2:
            return None
        digest = tron_prefixed_hash(message)
        q = ethsig.ecrecover(digest, recids[0], r, sv)
        if q is None:
            return None
        return pubkey_point_to_t_address(q)
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------
# test seam: injectable RPC (mirrors x402.set_test_rpc)
# --------------------------------------------------------------------------

_test_rpc = None
_test_rpc_lock = threading.Lock()


def set_test_rpc(fn) -> None:
    global _test_rpc
    with _test_rpc_lock:
        _test_rpc = fn


def clear_test_rpc() -> None:
    global _test_rpc
    with _test_rpc_lock:
        _test_rpc = None


def get_test_rpc():
    with _test_rpc_lock:
        return _test_rpc


# --------------------------------------------------------------------------
# HTTP API (stdlib urllib — proxy-safe, sync)
# --------------------------------------------------------------------------

def _post(path: str, body: dict, urls: list[str], timeout: int = 25) -> dict:
    """POST a JSON body to a Tron HTTP API path, trying each URL in
    turn. Test seam: set_test_rpc(fn) replaces the network; fn gets
    (path, body) and returns the parsed JSON dict."""
    test_rpc = get_test_rpc()
    if test_rpc is not None:
        return test_rpc(path, body)
    data = json.dumps(body).encode()
    last = None
    for u in urls:
        url = u.rstrip("/") + path
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json",
                     "User-Agent": "awlpay-tron/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001 - try next endpoint
            last = e
    raise last if last else RuntimeError("no tron rpc urls configured")


def fetch_transaction_info(tx_hash: str, urls: list[str] | None = None,
                            timeout: int = 25) -> dict | None:
    """wallet/gettransactioninfobyid. Returns the parsed JSON, or None
    if the node returns an empty object ({} with HTTP 200 = tx not
    found / not yet indexed)."""
    resp = _post("/wallet/gettransactioninfobyid",
                 {"value": tx_hash.lower()}, urls or [])
    if not isinstance(resp, dict) or not resp:
        return None
    return resp


def fetch_head_block_number(urls: list[str] | None = None,
                            timeout: int = 25) -> int | None:
    """wallet/getnowblock -> head block number, or None."""
    resp = _post("/wallet/getnowblock", {}, urls or [])
    try:
        return int(resp["block_header"]["raw_data"]["number"])
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# verification (the checklist)
# --------------------------------------------------------------------------

def _clean_fail(reason: str, **kw) -> tuple[bool, dict]:
    info = {"reason": reason}
    info.update(kw)
    return False, info


def _topic_addr(topic: str) -> str:
    """32-byte (64-hex) log topic -> T-address (last 20 bytes)."""
    t = (topic or "").strip().lower()
    if t.startswith("0x"):
        t = t[2:]
    if not re.fullmatch(r"[0-9a-f]{64}", t):
        raise ValueError("bad topic %r" % (topic,))
    return hex_to_t_address("41" + t[24:])


def verify_tron_payment(tx_hash: str, network: str, min_units: int,
                        pay_to: str, used_set: set,
                        rpc=None, payer_sig: str | None = None,
                        resource: str = "",
                        min_confirmations: int = 0) -> tuple[bool, dict]:
    """Verify a Tron TRC-20 USDT payment. Returns (ok, info).
    READ-ONLY: never mutates used_set — the caller consumes the
    replay key after success.

    tx_hash: 64-hex Tron txid (no 0x).
    network: "tron:0" (mainnet) or "tron:1" (Nile testnet).
    min_units: minimum acceptable amount, integer USDT units
        (1 USDT = 1_000_000 units). Integer math only — never floats.
    pay_to: the challenged destination T-address.
    used_set: replay store (checked read-only).
    rpc: optional (path, body) -> dict callable (tests).
    payer_sig: REQUIRED. The token sender's Tron TIP-191 signature
        (65-byte r||s||v hex, 0x optional) over
        binding_message(tx_hash, resource). None -> clean refusal.
    resource: the endpoint string bound into the signature.
    min_confirmations: head_block - tx_block must be >= this when > 0.
        Default 0: small x402 payments are accepted on a SUCCESS
        receipt without waiting (Nile/mainnet Tron finality is ~19
        SR confirmations per block; pass 20 here for large amounts
        that warrant settlement-grade certainty).
    """
    h = (tx_hash or "").strip().lower()
    if network not in (TRON_MAINNET, TRON_NILE):
        return _clean_fail("unsupported network %r (tron rail takes "
                           "tron:0 mainnet or tron:1 nile testnet)"
                           % (network,))
    if not valid_tx_hash(h):
        return _clean_fail("bad tx hash format (want 64 hex chars, no 0x)")
    key = network + ":" + h
    if key in used_set:
        return _clean_fail("replay: payment already used", replay_key=key)
    if not valid_t_address(pay_to):
        return _clean_fail("bad payTo T-address %r" % (pay_to,))
    try:
        min_units = int(min_units)
    except (ValueError, TypeError):
        return _clean_fail("bad min_units %r" % (min_units,))
    if min_units <= 0:
        return _clean_fail("min_units must be positive")

    # --- payer binding is mandatory: refuse before any RPC work ---
    if not payer_sig:
        return _clean_fail("missing payerSig: bind the proof with a Tron "
                           "TIP-191 signature over binding_message(tx_hash, "
                           "resource) from the token sender's address",
                           replay_key=key)

    # --- fetch ---
    urls = rpc_urls_for(network)
    try:
        if rpc is not None:
            info_resp = rpc("/wallet/gettransactioninfobyid",
                            {"value": h})
        else:
            info_resp = fetch_transaction_info(h, urls=urls)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return _clean_fail("tron rpc unreachable: %s" % str(e)[:120],
                           replay_key=key)
    # Tron returns {} with HTTP 200 for unknown txids.
    if not info_resp:
        return _clean_fail("tx not found / not indexed yet",
                           replay_key=key)

    # --- receipt ---
    receipt = info_resp.get("receipt") or {}
    if receipt.get("result") != "SUCCESS":
        return _clean_fail("tx failed on-chain (receipt.result=%r)"
                           % (receipt.get("result"),),
                           replay_key=key)

    # --- scan logs for the USDT Transfer to pay_to ---
    usdt_hex = usdt_contract_for(network).lower()
    paid_units = None
    payer = None
    for log in info_resp.get("log") or []:
        try:
            log_addr = (log.get("address") or "").strip().lower()
            if log_addr.startswith("0x"):
                log_addr = log_addr[2:]
            # log.address is the 20-byte contract address WITHOUT the
            # 41 prefix — prepend it for comparison.
            if "41" + log_addr != usdt_hex:
                continue
            topics = log.get("topics") or []
            if len(topics) != 3:
                continue
            t0 = (topics[0] or "").strip().lower().removeprefix("0x")
            if t0 != TRANSFER_TOPIC:
                continue
            to_addr = _topic_addr(topics[2])
            if to_addr != pay_to:
                continue
            data = (log.get("data") or "").strip().lower()
            if data.startswith("0x"):
                data = data[2:]
            if not re.fullmatch(r"[0-9a-f]+", data):
                continue
            paid_units = int(data, 16)  # uint256, 6-decimal USDT
            payer = _topic_addr(topics[1])
            break
        except (ValueError, TypeError, AttributeError):
            continue
    if paid_units is None:
        return _clean_fail("no matching USDT Transfer log to payTo in tx",
                           replay_key=key)
    if paid_units < min_units:
        return _clean_fail("underpaid: got %d units, need %d"
                           % (paid_units, min_units),
                           paid_units=paid_units, payer=payer,
                           replay_key=key)

    # --- confirmations (only when the caller demands them) ---
    block_number = info_resp.get("blockNumber")
    if min_confirmations and min_confirmations > 0:
        try:
            if rpc is not None:
                head_resp = rpc("/wallet/getnowblock", {})
            else:
                head_resp = _post("/wallet/getnowblock", {}, urls)
            head = int(head_resp["block_header"]["raw_data"]["number"])
            confs = head - int(block_number)
        except (KeyError, TypeError, ValueError):
            return _clean_fail("could not determine confirmations "
                               "(missing blockNumber/head)",
                               replay_key=key)
        if confs < min_confirmations:
            return _clean_fail("insufficient confirmations: %d < %d"
                               % (confs, min_confirmations),
                               confirmations=confs, replay_key=key)

    # --- payer signature binding ---
    msg = binding_message(h, resource or "")
    signer = verify_tron_sig(msg, payer_sig)
    if signer is None:
        return _clean_fail("payerSig invalid: not a valid Tron TIP-191 "
                           "signature over the binding message",
                           replay_key=key)
    if signer != payer:
        return _clean_fail("payerSig signer mismatch: signature is from "
                           "%s but the USDT sender is %s"
                           % (signer, payer),
                           signer=signer, payer=payer, replay_key=key)

    return True, {"paid_units": paid_units, "tx": h, "network": network,
                  "payer": payer, "replay_key": key,
                  "block_number": block_number}


# --------------------------------------------------------------------------
# x402 wiring helpers
# --------------------------------------------------------------------------

def tron_payment_terms() -> dict | None:
    """PaymentTerms entry for the 402 body when AWL_PAY_TO_TRON is set,
    else None (rail disabled). The parent wires this into
    x402.payment_terms(); the amount fields are filled there."""
    pay_to = tron_pay_to()
    if not pay_to or not valid_t_address(pay_to):
        return None
    return {
        "asset": "USDT",
        "network": TRON_MAINNET,
        "payTo": pay_to,
        "scheme": "tron-trc20",
        "extra": {
            "contract": usdt_contract_for(TRON_MAINNET),
            "decimals": USDT_DECIMALS,
            "payerSig": "required: Tron TIP-191 signature (r||s||v hex) "
                        "over binding_message(tx_hash, resource) from "
                        "the USDT sender's address",
        },
    }


def usdt_to_units_str(usdt: str) -> str:
    """'1.5' -> '1500000'. String in, string out, no floats."""
    from decimal import Decimal, InvalidOperation
    try:
        d = Decimal(usdt.strip())
    except InvalidOperation:
        raise ValueError("bad USDT amount %r" % (usdt,))
    if d.is_nan() or d < 0:
        raise ValueError("bad USDT amount %r" % (usdt,))
    return str(int(d * UNITS_PER_USDT))
