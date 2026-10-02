#!/usr/bin/env python3
"""awLPay XRPL rail — receive-side helpers for x402 payments in XRP.

HARD RULES for this module:

  * TESTNET ONLY. The default RPC endpoints below are the XRPL *testnet*.
    Mainnet ("xrpl:0") is recognized as a network id so the verifier can
    name it in refusals, but no mainnet RPC is configured and
    mainnet_urls() returns [] unless the operator sets AWL_RPC_XRPL_MAINNET
    explicitly. A mainnet flip is a deliberate CODE change plus Corey's
    explicit per-charge approval, never a config flip.
  * NO funds ever move here. This module never signs, never sends
    transactions, never holds keys. It only READS ledger state
    (plus address/drops math, which is pure). The E2E test script
    (tests/manual/) is the only place a test wallet signs, and it uses
    throwaway testnet wallets.
  * Amounts are integer drops end-to-end (1 XRP = 1_000_000 drops).
    Never floats: the ledger's total supply exceeds 2**53, so float
    conversion silently corrupts large values.

RPC goes through urllib (stdlib), not xrpl-py's httpx client: httpx
misparses this runtime's proxy/no_proxy environment and direct
connections are blocked. xrpl-py is used for addresscodec (validation),
drops math, and offline transaction construction only.

Env (read at call time):
    AWL_PAY_TO_XRPL      classic r-address receiving XRP (unset disables
                         the XRPL rail in x402.payment_terms)
    AWL_RPC_XRPL         comma-separated XRPL testnet JSON-RPC URLs
                         (default https://s.altnet.rippletest.net:51234)
    AWL_XRPL_INVOICE_SALT  salt for the InvoiceID challenge binding
                         (unset -> loud warning + fixed default; set it
                         in production so invoice IDs can't be forged)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.request

from xrpl.core.addresscodec import is_valid_classic_address
from xrpl.utils import xrp_to_drops, drops_to_xrp

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

XRPL_MAINNET = "xrpl:0"
XRPL_TESTNET = "xrpl:1"

DROPS_PER_XRP = 1_000_000

TESTNET_RPC_DEFAULT = ["https://s.altnet.rippletest.net:51234"]

# Classic r-address: r + 25-34 base58 chars (no 0, O, I, l).
R_ADDRESS_RE = re.compile(r"^r[1-9A-HJ-NP-Za-km-z]{25,34}$")

# XRPL tx hashes are 64 uppercase hex chars, NO 0x prefix.
TX_HASH_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

# tfPartialPayment flag bit (ledger flag, not to be confused with the
# transaction flag of the same name on other tx types).
TF_PARTIAL_PAYMENT = 0x00020000

# RLUSD issuer (mainnet). RLUSD rail is NOT built in v1 — kept here so the
# verifier can name it in refusals and the v2 diff stays small.
RLUSD_ISSUER_MAINNET = "rMxCKbEDwqr76QuheSUMdEGf4B9xJ8m5De"
RLUSD_CURRENCY_HEX = "524C555344000000000000000000000000000000"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def xrpl_pay_to() -> str:
    return os.environ.get("AWL_PAY_TO_XRPL", "").strip()


def xrpl_rpcs() -> list[str]:
    urls = [u.strip() for u in os.environ.get("AWL_RPC_XRPL", "").split(",")
            if u.strip()]
    return urls or list(TESTNET_RPC_DEFAULT)


def invoice_salt() -> str:
    s = os.environ.get("AWL_XRPL_INVOICE_SALT", "").strip()
    if not s:
        # Loud default: fine for testnet, must be set for production.
        print("WARNING: AWL_XRPL_INVOICE_SALT unset — using fixed testnet "
              "default. Set a secret salt before any production use.")
        return "awlpay-xrpl-testnet-default-salt"
    return s


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def valid_r_address(addr) -> bool:
    """Classic r-address validation via xrpl-py's addresscodec."""
    if not isinstance(addr, str) or not R_ADDRESS_RE.fullmatch(addr):
        return False
    try:
        return bool(is_valid_classic_address(addr))
    except Exception:
        return False


def valid_tx_hash(h) -> bool:
    return bool(isinstance(h, str) and TX_HASH_RE.fullmatch(h.strip()))


def drops_to_int(drops) -> int:
    """Strict drops -> int. Raises on anything non-integer."""
    s = str(drops).strip()
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError("not an integer drops value: %r" % (drops,))
    return int(s)


def xrp_to_drops_str(xrp: str) -> str:
    """'1.5' -> '1500000'. String in, string out, no floats."""
    from decimal import Decimal
    return str(xrp_to_drops(Decimal(xrp)))


def invoice_id(pay_to: str, amount_drops: str, network: str,
               salt: str | None = None, hour: int | None = None) -> str:
    """Challenge binding: 64-hex InvoiceID the payer must set on the
    Payment transaction. Binds (payTo, amount, network, hour, salt) —
    the salt means only someone who saw a real 402 challenge can mint a
    matching invoice ID. The verifier accepts the current and previous
    hour to absorb clock skew."""
    if hour is None:
        hour = int(time.time()) // 3600
    s = salt if salt is not None else invoice_salt()
    msg = "awlpay-xrpl-invoice|%s|%s|%s|%d|%s" % (
        pay_to, amount_drops, network, hour, s)
    return hashlib.sha256(msg.encode()).hexdigest().upper()


def invoice_id_valid(claimed: str, pay_to: str, amount_drops: str,
                     network: str, salt: str | None = None) -> bool:
    """Accept the invoice ID if it matches this hour or the last."""
    claimed = (claimed or "").strip().upper()
    if not re.fullmatch(r"[0-9A-F]{64}", claimed):
        return False
    now_hour = int(time.time()) // 3600
    for h in (now_hour, now_hour - 1):
        if invoice_id(pay_to, amount_drops, network, salt, h) == claimed:
            return True
    return False


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
# JSON-RPC (stdlib urllib — proxy-safe, sync, matches x402.py style)
# --------------------------------------------------------------------------

def _rpc(url: str, method: str, params, timeout: int = 25) -> dict:
    body = json.dumps({"method": method, "params": [params]}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json",
                 "User-Agent": "awlpay-xrpl/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _rpc_any(method: str, params, urls: list[str], timeout: int = 25) -> dict:
    last = None
    for u in urls:
        try:
            return _rpc(u, method, params, timeout)
        except Exception as e:  # noqa: BLE001 - try next endpoint
            last = e
    raise last if last else RuntimeError("no xrpl rpc urls configured")


def fetch_transaction(tx_hash: str, urls: list[str] | None = None,
                      timeout: int = 25, retries: int = 3) -> dict | None:
    """Fetch a transaction by hash. Returns the parsed result dict, or
    None if the ledger doesn't know the hash (after retries — propagation
    takes seconds). Test seam: set_test_rpc(fn) replaces the network."""
    test_rpc = get_test_rpc()
    h = (tx_hash or "").strip().upper()
    if test_rpc is not None:
        return test_rpc("tx", {"transaction": h})
    urls = urls or xrpl_rpcs()
    last = None
    for _ in range(max(1, retries)):
        try:
            resp = _rpc_any("tx", {"transaction": h}, urls, timeout)
        except Exception as e:  # noqa: BLE001 - retry, then give up
            last = e
            time.sleep(2)
            continue
        if isinstance(resp, dict) and "result" in resp:
            return resp["result"]
        # Ledger-level "txnNotFound": not an error, just unknown (yet).
        return None
    if last:
        raise last
    return None


# --------------------------------------------------------------------------
# verification (the §2 checklist, XRP-native only in v1)
# --------------------------------------------------------------------------

def _clean_fail(reason: str, **kw) -> tuple[bool, dict]:
    info = {"reason": reason}
    info.update(kw)
    return False, info


def verify_xrpl_payment(tx_hash: str, network: str, min_drops: int,
                       pay_to: str, used_set: set,
                       rpc=None, invoice_salt_val: str | None = None,
                       max_age_s: int = 3600) -> tuple[bool, dict]:
    """Verify an XRPL XRP Payment. Returns (ok, info). READ-ONLY: never
    mutates used_set — the caller consumes the replay key after success.

    min_drops: minimum acceptable amount, integer drops.
    pay_to: the challenged destination r-address.
    used_set: replay store (checked read-only).
    rpc: optional (method, params) -> result callable (tests).
    """
    h = (tx_hash or "").strip().upper()
    if network == XRPL_MAINNET:
        return _clean_fail("xrpl mainnet not enabled on this server "
                           "(testnet only: xrpl:1)")
    if network != XRPL_TESTNET:
        return _clean_fail("unsupported network %r (xrpl rail takes "
                           "xrpl:1 testnet)" % (network,))
    if not valid_tx_hash(h):
        return _clean_fail("bad tx hash format (want 64 hex chars)")
    key = "xrpl:1:" + h.lower()
    if key in used_set:
        return _clean_fail("replay: payment already used", replay_key=key)
    if not valid_r_address(pay_to):
        return _clean_fail("bad payTo r-address %r" % (pay_to,))
    try:
        min_drops = int(min_drops)
    except (ValueError, TypeError):
        return _clean_fail("bad min_drops %r" % (min_drops,))
    if min_drops <= 0:
        return _clean_fail("min_drops must be positive")

    # --- fetch ---
    try:
        if rpc is not None:
            result = rpc("tx", {"transaction": h})
        else:
            result = fetch_transaction(h)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return _clean_fail("xrpl rpc unreachable: %s" % str(e)[:120],
                           replay_key=key)
    if not result:
        return _clean_fail("tx not found / not validated yet",
                           replay_key=key)
    if not result.get("validated"):
        return _clean_fail("tx not in a validated ledger yet",
                           replay_key=key)
    tx = result.get("tx") if isinstance(result.get("tx"), dict) else result

    # --- field checklist (against the ledger's record, never the claim) ---
    if tx.get("TransactionType") != "Payment":
        return _clean_fail("not a Payment transaction (got %r)"
                           % (tx.get("TransactionType"),),
                           replay_key=key)
    if (tx.get("Destination") or "") != pay_to:
        return _clean_fail("destination mismatch (got %r)"
                           % (tx.get("Destination"),),
                           replay_key=key)
    flags = int(tx.get("Flags") or 0)
    if flags & TF_PARTIAL_PAYMENT:
        return _clean_fail("tfPartialPayment set: delivered amount may be "
                           "less than Amount; refusing", replay_key=key)
    meta = result.get("meta") or {}
    if isinstance(meta, str):
        # binary metadata form — not parseable here; refuse loudly.
        return _clean_fail("binary metadata not supported; resubmit with "
                           "expanded metadata", replay_key=key)
    delivered = (meta.get("delivered_amount")
                 if isinstance(meta, dict) else None)
    if delivered is None:
        return _clean_fail("no delivered_amount in tx metadata",
                           replay_key=key)
    # XRP-native: delivered_amount is a drops string. Anything else
    # (IOU object) is a different asset — refuse (RLUSD is v2).
    if not isinstance(delivered, str):
        return _clean_fail("non-XRP asset delivered (IOU object); this "
                           "rail takes XRP only in v1", replay_key=key)
    try:
        paid_drops = drops_to_int(delivered)
    except ValueError:
        return _clean_fail("bad delivered_amount %r" % (delivered,),
                           replay_key=key)
    if paid_drops < min_drops:
        return _clean_fail("underpaid: got %d drops, need %d"
                           % (paid_drops, min_drops),
                           paid_drops=paid_drops, replay_key=key)

    # --- InvoiceID binding (mandatory on the push path) ---
    claimed_invoice = (tx.get("InvoiceID") or "").strip().upper()
    salt = invoice_salt_val if invoice_salt_val is not None else invoice_salt()
    if not invoice_id_valid(claimed_invoice, pay_to, str(min_drops),
                            network, salt):
        return _clean_fail("InvoiceID mismatch: payment is not bound to "
                           "this challenge (set extra.invoiceId from the "
                           "402 body on your Payment tx)", replay_key=key)

    # --- age gate: reject txs older than the challenge could be ---
    close_time = None
    try:
        # date in tx is seconds since Ripple epoch (2000-01-01).
        close_time = int(tx.get("date") or 0) + 946684800
    except (ValueError, TypeError):
        close_time = 0
    if close_time and (time.time() - close_time) > max_age_s:
        return _clean_fail("tx too old (settled before this challenge)",
                           replay_key=key)

    ledger_index = result.get("ledger_index")
    return True, {"paid_drops": paid_drops, "tx": h, "network": network,
                  "payer": tx.get("Account"), "replay_key": key,
                  "ledger_index": ledger_index}
