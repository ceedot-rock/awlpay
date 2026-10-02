#!/usr/bin/env python3
"""awLPay Stellar rail — receive-side helpers for x402 payments in XLM.

HARD RULES for this module:

  * TESTNET FIRST. Verification is READ-ONLY and works against any
    Horizon endpoint the operator configures; the shipped defaults
    point at the public Stellar testnet Horizon. A mainnet (pubnet)
    receive flow is a deliberate operator decision (AWL_PAY_TO_STELLAR
    + AWL_STELLAR_MEMO_SALT set, amounts reviewed) — never automatic.
  * NO funds ever move here. This module never signs, never sends
    transactions, never holds keys. It only READS Horizon state
    (plus StrKey/stroops math, which is pure). The E2E test script
    (tests/manual/) is the only place a test wallet signs, and it uses
    throwaway testnet wallets.
  * Amounts are integer stroops end-to-end (1 XLM = 10_000_000
    stroops). Never floats: Horizon quotes decimal strings; we
    multiply with Decimal and truncate to int.

RPC goes through stdlib urllib (GET against Horizon's REST API),
proxy-safe, sync — matching the xrpl.py style. No stellar-sdk
dependency: StrKey (G-address) validation is ~40 lines of stdlib
(base32 + CRC16-XModem).

Env (read at call time):
    AWL_PAY_TO_STELLAR    G-address receiving XLM (unset disables the
                          Stellar rail in x402.payment_terms)
    AWL_RPC_STELLAR       comma-separated Horizon REST base URLs for
                          pubnet (default https://horizon.stellar.org)
    AWL_RPC_STELLAR_TESTNET
                          comma-separated Horizon REST base URLs for
                          testnet (default
                          https://horizon-testnet.stellar.org)
    AWL_STELLAR_MEMO_SALT salt for the memo-hash challenge binding
                          (unset -> loud warning + fixed default; set
                          it in production so memo hashes can't be forged)
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

STELLAR_PUBNET = "stellar:pubnet"
STELLAR_TESTNET = "stellar:testnet"

STROOPS_PER_XLM = 10_000_000

PUBNET_HORIZON_DEFAULT = ["https://horizon.stellar.org"]
TESTNET_HORIZON_DEFAULT = ["https://horizon-testnet.stellar.org"]

# G-address: G + 55 base32 chars (uppercase, no 0/1/8/9), 56 chars total.
G_ADDRESS_RE = re.compile(r"^G[2-7A-Z]{55}$")

# Stellar tx hashes are 64 hex chars, no 0x prefix.
TX_HASH_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

# Horizon amount strings: up to 7 fractional digits, e.g. "10.0000000".
AMOUNT_RE = re.compile(r"^[0-9]+(\.[0-9]{1,7})?$")

# StrKey version byte for an ed25519 public key ("G" addresses): 6 << 3.
STRKEY_VERSION_G = 48

# A tx must sit this many ledgers deep before we call it final.
FINALITY_LEDGERS = 2


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def stellar_pay_to() -> str:
    return os.environ.get("AWL_PAY_TO_STELLAR", "").strip()


def _split_urls(raw: str) -> list[str]:
    return [u.strip().rstrip("/") for u in raw.split(",") if u.strip()]


def horizon_urls_for(network: str) -> list[str]:
    if network == STELLAR_PUBNET:
        urls = _split_urls(os.environ.get("AWL_RPC_STELLAR", ""))
        return urls or list(PUBNET_HORIZON_DEFAULT)
    urls = _split_urls(os.environ.get("AWL_RPC_STELLAR_TESTNET", ""))
    return urls or list(TESTNET_HORIZON_DEFAULT)


def memo_salt() -> str:
    s = os.environ.get("AWL_STELLAR_MEMO_SALT", "").strip()
    if not s:
        # Loud default: fine for testnet, must be set for production.
        print("WARNING: AWL_STELLAR_MEMO_SALT unset — using fixed testnet "
              "default. Set a secret salt before any production use.")
        return "awlpay-stellar-testnet-default-salt"
    return s


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def _crc16_xmodem(data: bytes) -> int:
    """CRC-16/XModem (poly 0x1021, init 0x0000) — the StrKey checksum."""
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def valid_g_address(addr) -> bool:
    """StrKey G-address validation, stdlib only: base32 decode, version
    byte 48 ('G'), 32-byte ed25519 key, CRC16-XModem checksum."""
    if not isinstance(addr, str) or not G_ADDRESS_RE.fullmatch(addr):
        return False
    try:
        raw = base64.b32decode(addr)
    except (binascii.Error, ValueError):
        return False
    if len(raw) != 35:
        return False
    if raw[0] != STRKEY_VERSION_G:
        return False
    checksum = raw[33] | (raw[34] << 8)  # little-endian on the wire
    return _crc16_xmodem(raw[:33]) == checksum


def valid_tx_hash(h) -> bool:
    return bool(isinstance(h, str) and TX_HASH_RE.fullmatch(h.strip()))


def xlm_to_stroops_str(xlm: str) -> str:
    """'1.5' -> '15000000'. String in, string out, no floats."""
    s = str(xlm).strip()
    if not AMOUNT_RE.fullmatch(s):
        raise ValueError("bad XLM amount string: %r" % (xlm,))
    return str(int(Decimal(s) * STROOPS_PER_XLM))


def stroops_to_int(stroops) -> int:
    """Strict stroops -> int. Raises on anything non-integer."""
    s = str(stroops).strip()
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError("not an integer stroops value: %r" % (stroops,))
    return int(s)


def memo_id(pay_to: str, amount_stroops: str, network: str,
            salt: str | None = None, hour: int | None = None) -> str:
    """Challenge binding: 64-hex memo hash the payer must set as the
    tx's memo (memo_type=hash). Binds (payTo, amount, network, hour,
    salt) — the salt means only someone who saw a real 402 challenge
    can mint a matching memo. The verifier accepts the current and
    previous hour to absorb clock skew. This is Stellar's analogue of
    the XRPL rail's InvoiceID."""
    if hour is None:
        hour = int(time.time()) // 3600
    s = salt if salt is not None else memo_salt()
    msg = "awlpay-stellar-memo|%s|%s|%s|%d|%s" % (
        pay_to, amount_stroops, network, hour, s)
    return hashlib.sha256(msg.encode()).hexdigest()


def memo_id_valid(claimed_b64: str, pay_to: str, amount_stroops: str,
                  network: str, salt: str | None = None) -> bool:
    """Accept the memo hash if it matches this hour or the last. The
    tx carries the 32 raw bytes as base64; we compare against
    memo_id()'s 64-hex."""
    try:
        claimed = base64.b64decode((claimed_b64 or "").strip(), validate=True)
    except (binascii.Error, ValueError):
        return False
    if len(claimed) != 32:
        return False
    now_hour = int(time.time()) // 3600
    for h in (now_hour, now_hour - 1):
        if bytes.fromhex(memo_id(pay_to, amount_stroops, network, salt,
                                 h)) == claimed:
            return True
    return False


# --------------------------------------------------------------------------
# test seam: injectable RPC (mirrors xrpl.set_test_rpc)
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
# Horizon REST (stdlib urllib — proxy-safe, sync, matches xrpl.py style)
# --------------------------------------------------------------------------

def _horizon_get(url: str, timeout: int = 25):
    """GET a Horizon URL. Returns the parsed JSON dict, or None on
    HTTP 404 (resource unknown). Raises on other errors."""
    req = urllib.request.Request(
        url, headers={"Accept": "application/json",
                      "User-Agent": "awlpay-stellar/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _get_any(path: str, query: str, urls: list[str], timeout: int = 25):
    """GET path+query against each Horizon base in turn (failover)."""
    last = None
    for base in urls:
        try:
            return _horizon_get(base + path + query, timeout)
        except Exception as e:  # noqa: BLE001 - try next endpoint
            last = e
    raise last if last else RuntimeError("no stellar horizon urls configured")


def _api(method: str, params: dict, urls: list[str] | None,
         network: str, timeout: int = 25):
    """Logical RPC with test seam. methods: 'transaction', 'operations',
    'latest_ledger'. Test seam: set_test_rpc(fn) with fn(method, params)."""
    test_rpc = get_test_rpc()
    if test_rpc is not None:
        return test_rpc(method, params)
    urls = urls or horizon_urls_for(network)
    if method == "transaction":
        return _get_any("/transactions/%s" % params["hash"], "", urls,
                        timeout)
    if method == "operations":
        return _get_any("/transactions/%s/operations" % params["hash"],
                        "?limit=100", urls, timeout)
    if method == "latest_ledger":
        return _get_any("/ledgers", "?order=desc&limit=1", urls, timeout)
    raise ValueError("unknown stellar rpc method %r" % (method,))


def fetch_transaction(tx_hash: str, network: str,
                      urls: list[str] | None = None,
                      timeout: int = 25, retries: int = 3) -> dict | None:
    """Fetch a transaction by hash. Returns the Horizon tx resource, or
    None if Horizon 404s (after retries — propagation takes seconds)."""
    h = (tx_hash or "").strip().lower()
    last = None
    for _ in range(max(1, retries)):
        try:
            return _api("transaction", {"hash": h}, urls, network, timeout)
        except Exception as e:  # noqa: BLE001 - retry, then give up
            last = e
            time.sleep(2)
    if last:
        raise last
    return None


# --------------------------------------------------------------------------
# verification (XLM-native only in v1)
# --------------------------------------------------------------------------

def _clean_fail(reason: str, **kw) -> tuple[bool, dict]:
    info = {"reason": reason}
    info.update(kw)
    return False, info


def _find_pay_op(records: list, pay_to: str):
    """Find the qualifying XLM credit op. Accepts:

    * type == "payment" with asset_type == "native" and to == pay_to
      (exact match), transaction_successful == true.
    * type == "create_account" crediting pay_to — DOCUMENTED DECISION:
      create_account *is* an XLM payment: it moves starting_balance
      XLM from the funder to a brand-new account. Refusing it would
      strand a legitimate first payment to a fresh payTo address
      (Stellar accounts must be created with a minimum balance, so a
      brand-new payTo can ONLY be paid via create_account). Horizon
      names the funded account "account" on create_account ops
      ("funder" is the source); some SDKs surface "to" instead, so we
      accept either. The credited amount is starting_balance.

    Returns (op, amount_str, op_kind) or (None, None, None)."""
    for op in records:
        if not isinstance(op, dict):
            continue
        if op.get("transaction_successful") is not True:
            continue
        otype = op.get("type")
        if otype == "payment":
            if op.get("asset_type") != "native":
                continue
            if (op.get("to") or "") != pay_to:
                continue
            return op, op.get("amount"), "payment"
        if otype == "create_account":
            dest = op.get("to") or op.get("account")
            if (dest or "") != pay_to:
                continue
            return op, op.get("starting_balance"), "create_account"
    return None, None, None


def verify_stellar_payment(tx_hash: str, network: str, min_stroops: int,
                           pay_to: str, used_set: set,
                           rpc=None, memo_salt_val: str | None = None,
                           max_age_s: int = 3600) -> tuple[bool, dict]:
    """Verify a Stellar XLM payment. Returns (ok, info). READ-ONLY: never
    mutates used_set — the caller consumes the replay key after success.

    min_stroops: minimum acceptable amount, integer stroops.
    pay_to: the challenged destination G-address.
    used_set: replay store (checked read-only).
    rpc: optional (method, params) -> result callable (tests); methods
         are 'transaction', 'operations', 'latest_ledger'.
    """
    h = (tx_hash or "").strip().lower()
    if network not in (STELLAR_PUBNET, STELLAR_TESTNET):
        return _clean_fail("unsupported network %r (stellar rail takes "
                           "stellar:pubnet or stellar:testnet)"
                           % (network,))
    if not valid_tx_hash(h):
        return _clean_fail("bad tx hash format (want 64 hex chars)")
    key = network + ":" + h
    if key in used_set:
        return _clean_fail("replay: payment already used", replay_key=key)
    if not valid_g_address(pay_to):
        return _clean_fail("bad payTo G-address %r" % (pay_to,))
    try:
        min_stroops = int(min_stroops)
    except (ValueError, TypeError):
        return _clean_fail("bad min_stroops %r" % (min_stroops,))
    if min_stroops <= 0:
        return _clean_fail("min_stroops must be positive")

    # --- fetch tx ---
    try:
        if rpc is not None:
            tx = rpc("transaction", {"hash": h})
        else:
            tx = fetch_transaction(h, network)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return _clean_fail("stellar horizon unreachable: %s" % str(e)[:120],
                           replay_key=key)
    if not tx:
        return _clean_fail("tx not found / not ingested yet",
                           replay_key=key)
    if tx.get("successful") is not True:
        # Failed Stellar txs ARE on-ledger but moved nothing.
        return _clean_fail("tx on ledger but not successful (moved "
                           "nothing)", replay_key=key)

    # --- finality: tx must sit FINALITY_LEDGERS deep ---
    tx_ledger = tx.get("ledger")
    try:
        if rpc is not None:
            latest = rpc("latest_ledger", {})
        else:
            latest = _api("latest_ledger", {}, None, network)
        records = (latest.get("_embedded") or {}).get("records") or []
        latest_seq = int(records[0]["sequence"]) if records else None
    except Exception as e:  # noqa: BLE001 - clean fail below
        return _clean_fail("could not read latest ledger: %s" % str(e)[:80],
                           replay_key=key)
    if latest_seq is None or not isinstance(tx_ledger, int):
        return _clean_fail("ledger data missing; cannot confirm finality",
                           replay_key=key)
    if latest_seq - tx_ledger < FINALITY_LEDGERS:
        return _clean_fail("tx too fresh: ledger %d, latest %d (need %d "
                           "confirmations)" % (tx_ledger, latest_seq,
                                               FINALITY_LEDGERS),
                           replay_key=key)

    # --- find the qualifying payment op ---
    try:
        if rpc is not None:
            ops = rpc("operations", {"hash": h})
        else:
            ops = _api("operations", {"hash": h}, None, network)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return _clean_fail("could not fetch tx operations: %s" % str(e)[:80],
                           replay_key=key)
    records = (ops.get("_embedded") or {}).get("records") or []
    op, amount_str, op_kind = _find_pay_op(records, pay_to)
    if op is None:
        return _clean_fail("no qualifying XLM payment op to %r in this tx "
                           "(need type=payment asset=native, or "
                           "create_account crediting payTo)" % (pay_to,),
                           replay_key=key)

    # --- amount: integer stroops, never float ---
    if not isinstance(amount_str, str) or not AMOUNT_RE.fullmatch(
            amount_str.strip()):
        return _clean_fail("bad op amount %r" % (amount_str,),
                           replay_key=key)
    try:
        paid_stroops = int(Decimal(amount_str.strip()) * STROOPS_PER_XLM)
    except InvalidOperation:
        return _clean_fail("bad op amount %r" % (amount_str,),
                           replay_key=key)
    if paid_stroops < min_stroops:
        return _clean_fail("underpaid: got %d stroops, need %d"
                           % (paid_stroops, min_stroops),
                           paid_stroops=paid_stroops, replay_key=key)

    # --- memo-hash binding (mandatory on the push path) ---
    if tx.get("memo_type") != "hash":
        return _clean_fail("memo_type is not 'hash': payment is not bound "
                           "to this challenge (set extra.memoHash from the "
                           "402 body as the tx memo)", replay_key=key)
    salt = (memo_salt_val if memo_salt_val is not None else memo_salt())
    if not memo_id_valid(tx.get("memo") or "", pay_to, str(min_stroops),
                         network, salt):
        return _clean_fail("memo hash mismatch: payment is not bound to "
                           "this challenge", replay_key=key)

    # --- age gate: reject txs older than the challenge could be ---
    created_at = tx.get("created_at") or ""
    try:
        # "2026-10-02T16:00:00Z" -> epoch seconds.
        ts = time.mktime(time.strptime(created_at[:19], "%Y-%m-%dT%H:%M:%S"))
        age = time.time() - ts
    except (ValueError, TypeError):
        age = 0
    if age and age > max_age_s:
        return _clean_fail("tx too old (settled before this challenge)",
                           replay_key=key)

    return True, {"paid_stroops": paid_stroops, "tx": h, "network": network,
                  "payer": op.get("from") or op.get("funder")
                           or op.get("source_account"),
                  "op_kind": op_kind, "replay_key": key,
                  "ledger": tx_ledger}
