#!/usr/bin/env python3
"""awLPay Lightning rail — receive-side helpers for x402 payments in
millisats, verified by preimage proof (the x402 Foundation Lightning
scheme, Block/Spiral, merged 2026-09-23).

HARD RULES for this module:

  * PREIMAGE PROOF ONLY. Lightning has no public ledger to query, so the
    proof is cryptographic, not ledger-read: the payer pays a BOLT11
    invoice we issued and hands us the 32-byte payment preimage in
    X-PAYMENT. We verify SHA256(preimage) == payment_hash of the invoice
    we stored for that challenge. No node, no ledger scan, no trust.
  * NO funds ever move here. We never hold a wallet, never sign, never
    send. The ZBD Charges API only ISSUES invoices into the lab's ZBD
    project (custodied sats flow to the lab's ZBD project on payment).
  * Invoice issuance requires the AWL_ZBD_API_KEY. Unset -> the rail is
    DISABLED (clean refusal in payment_terms, no crash). Invoices are
    issued by ZBD against the lab's project — mainnet only; there is no
    ZBD testnet issuer, so lightning:1 (testnet) has no issuance path in
    v1 and testnet verification stays unit-test-only.
  * Amounts are INTEGER millisats end-to-end. Never floats. sub-sat
    amounts are the whole point of this rail (the x402 ecosystem's
    Lightning unit bug — a $5 offer emitting maxAmountRequired "0" —
    must never recur here).
  * verify_lightning_payment NEVER mutates used_set (replay store):
    read-only check; the caller consumes the replay key after success,
    exactly like the XRPL rail.

Env (read at call time):
    AWL_ZBD_API_KEY   ZBD project API key for invoice issuance. Unset ->
                      rail disabled (clean refusal, not a crash).
    AWL_ZBD_API_BASE  ZBD REST base (default https://api.zebedee.io/v0).
                      ZBD only serves mainnet invoices (hrp lnbc).

Challenge binding:
    The payment_hash IS the binding. Each 402 challenge mints a fresh
    invoice with a unique payment_hash; the payer's X-PAYMENT preimage
    only verifies against the payment_hash stored for THAT challenge.
    A proof minted for one challenge can never validate against a
    different challenge, because the hash differs. No invoice ID, no
    salt — the invoice itself is the nonce.

X-PAYMENT shape for x402.py wiring:
    {"x402Version": 2, "scheme": "exact", "network": "lightning:0",
     "payload": {"preimage": "<64 hex>", "paymentHash": "<64 hex>"}}
    paymentHash must equal the invoice's payment_hash stored at 402
    challenge time; preimage must be the 32-byte secret that hashes to it.

ZBD Charges API shape (confirmed against https://docs.zbd.dev, mirrored
at github.com/illuminated-shade/zbd-docs api-reference/charges/create):
    POST {base}/charges
    header: apikey: <key>   (NOT "Authorization")
    body:  {"amount": "<msats STRING>", "description": "...",
            "expiresIn": <seconds NUMBER>}
    -> {"success": true,
        "data": {"invoice": {"request": "<bolt11>",
                             "expiresAt": "<ISO8601>"}, ...}}
    Note: docs also show an older gamertag variant keying the invoice as
    data.invoiceRequest / data.invoiceExpiresAt; both are parsed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.request

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

LIGHTNING_MAINNET = "lightning:0"
LIGHTNING_TESTNET = "lightning:1"

MSATS_PER_SAT = 1_000

ZBD_API_BASE_DEFAULT = "https://api.zebedee.io/v0"

# preimage / payment_hash: 32 bytes, 64 lowercase hex chars.
PREIMAGE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

# BOLT11 sanity: starts with lnbc / lntb / lnbcrt + amount + '1'.
BOLT11_RE = re.compile(r"^ln(bc|tb|bcrt)[0-9a-z]+\.?[0-9a-z]*1[0-9a-z]+$",
                       re.IGNORECASE)

# expiresIn bounds (ZBD documents 60..3600s for charges).
EXPIRES_MIN = 60
EXPIRES_MAX = 3600


# --------------------------------------------------------------------------
# config (read at call time — env flips take effect without restart)
# --------------------------------------------------------------------------

def zbd_api_key() -> str:
    return os.environ.get("AWL_ZBD_API_KEY", "").strip()


def zbd_api_base() -> str:
    return os.environ.get("AWL_ZBD_API_BASE", ZBD_API_BASE_DEFAULT).rstrip("/")


def lightning_enabled() -> bool:
    """True iff a ZBD API key is configured. Unset key -> rail disabled,
    a clean refusal in payment_terms, never a crash."""
    return bool(zbd_api_key())


# --------------------------------------------------------------------------
# test seam: injectable HTTP (mirrors xrpl.set_test_rpc)
# --------------------------------------------------------------------------

_test_http = None
_test_http_lock = threading.Lock()


def set_test_http(fn) -> None:
    """fn(method, url, body_dict, headers) -> response dict. Replaces the
    network for tests."""
    global _test_http
    with _test_http_lock:
        _test_http = fn


def clear_test_http() -> None:
    global _test_http
    with _test_http_lock:
        _test_http = None


def get_test_http():
    with _test_http_lock:
        return _test_http


# --------------------------------------------------------------------------
# HTTP (stdlib urllib — proxy-safe, matches xrpl.py style)
# --------------------------------------------------------------------------

def _post_json(url: str, body: dict, headers: dict, timeout: int = 25) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers=dict(headers, **{"Content-Type": "application/json",
                                 "User-Agent": "awlpay-lightning/1.0"}),
        method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------

def _clean_fail(reason: str, **kw) -> tuple[bool, dict]:
    info = {"reason": reason}
    info.update(kw)
    return False, info


def valid_preimage_hex(s) -> bool:
    """32-byte preimage, 64 lowercase hex chars, no 0x."""
    return bool(isinstance(s, str) and PREIMAGE_HEX_RE.fullmatch(s))


def valid_payment_hash_hex(s) -> bool:
    return bool(isinstance(s, str) and PREIMAGE_HEX_RE.fullmatch(s))


def msats_to_int(v) -> int:
    """Strict millisats -> int. Raises on anything non-integer
    (floats collapse sub-sat amounts — see the x402 ecosystem's
    Lightning unit bug; this rail must never repeat it)."""
    if isinstance(v, bool):
        raise ValueError("bool is not millisats")
    if isinstance(v, int):
        if v < 0:
            raise ValueError("negative millisats: %r" % (v,))
        return v
    s = str(v).strip()
    if not re.fullmatch(r"[0-9]+", s):
        raise ValueError("not an integer millisats value: %r" % (v,))
    return int(s)


def clamp_expires(expires_secs: int) -> int:
    try:
        e = int(expires_secs)
    except (ValueError, TypeError):
        e = 900
    return max(EXPIRES_MIN, min(EXPIRES_MAX, e))


def preimage_replay_key(payment_hash_hex: str) -> str:
    return "lightning:" + payment_hash_hex.lower()


# --------------------------------------------------------------------------
# invoice decode (bolt11 lib; hand-roll only if the lib is missing)
# --------------------------------------------------------------------------

def decode_invoice(bolt11_str: str) -> dict:
    """Decode a BOLT11 invoice. Returns dict:
        {"payment_hash": <64 hex>, "amount_msats": int,
         "expires_at": <unix ts>, "expiry_secs": int,
         "currency": "bc"|"tb"|..., "is_mainnet": bool,
         "description": str|None}
    Raises ValueError on any malformed input.
    """
    inv = (bolt11_str or "").strip()
    if not BOLT11_RE.match(inv):
        raise ValueError("not a BOLT11 invoice: %r" % (inv[:32],))
    try:
        import bolt11
    except ImportError as e:
        raise ValueError(
            "bolt11 library missing — install server/requirements.txt "
            "(pip install bolt11)") from e
    try:
        b = bolt11.decode(inv)
    except Exception as e:  # noqa: BLE001 - normalize all decode errors
        raise ValueError("BOLT11 decode failed: %s" % (e,)) from e
    try:
        ph = b.payment_hash
    except Exception as e:
        raise ValueError("invoice has no payment_hash tag") from e
    if not valid_payment_hash_hex(ph):
        raise ValueError("invoice payment_hash malformed: %r" % (ph,))
    if b.amount_msat is None:
        raise ValueError("invoice has no amount (any-amount invoices are "
                         "refused: the challenge amount must be exact)")
    amount_msats = int(b.amount_msat)
    expires_at = int(b.expiry_time)
    return {
        "payment_hash": ph.lower(),
        "amount_msats": amount_msats,
        "expires_at": expires_at,
        "expiry_secs": int(b.expiry),
        "currency": b.currency,
        "is_mainnet": bool(b.is_mainnet()),
        "description": b.description,
    }


# --------------------------------------------------------------------------
# invoice issuance (ZBD Charges API)
# --------------------------------------------------------------------------

def _charge_request(amount_msats: int, description: str,
                   expires_secs: int) -> tuple[str | None, str | None]:
    """POST /charges, return (raw_response_json_or_none, error_or_none).
    Uses the test seam when set."""
    url = zbd_api_base() + "/charges"
    body = {
        "amount": str(amount_msats),          # ZBD wants msats as a STRING
        "description": description,
        "expiresIn": clamp_expires(expires_secs),  # seconds, NUMBER
    }
    headers = {"apikey": zbd_api_key()}
    test_http = get_test_http()
    try:
        if test_http is not None:
            resp = test_http("POST", url, body, headers)
        else:
            resp = _post_json(url, body, headers)
    except Exception as e:  # noqa: BLE001 - surfaced as clean failure
        return None, "zbd unreachable: %s" % str(e)[:120]
    return resp, None


def _iso8601_to_unix(s: str) -> int | None:
    try:
        s = s.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        from datetime import datetime
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return None


def _parse_charge(resp: dict) -> tuple[dict, str | None]:
    """Extract (bolt11, expires_at, zbd_amount_msats) from a ZBD charge
    response; handles both the new invoice{} and older invoiceRequest
    response shapes."""
    if not isinstance(resp, dict):
        return {}, "bad zbd response (not a dict)"
    data = resp.get("data")
    if not isinstance(data, dict):
        msg = resp.get("message") or resp.get("error") or "no data"
        return {}, "zbd error: %s" % str(msg)[:160]
    inv_obj = data.get("invoice") or {}
    bolt11_str = inv_obj.get("request") or data.get("invoiceRequest")
    if not bolt11_str:
        return {}, "zbd response missing invoice.request"
    expires_at = None
    if inv_obj.get("expiresAt"):
        expires_at = _iso8601_to_unix(inv_obj["expiresAt"])
    elif data.get("invoiceExpiresAt"):
        expires_at = _iso8601_to_unix(data["invoiceExpiresAt"])
    try:
        zbd_msats = msats_to_int(data.get("amount", "0"))
    except ValueError:
        zbd_msats = None
    return ({"bolt11": bolt11_str, "expires_at": expires_at,
             "zbd_amount_msats": zbd_msats}, None)


def create_invoice(amount_msats: int, description: str,
                   expires_secs: int = 900) -> tuple:
    """Mint a BOLT11 invoice for one 402 challenge via ZBD.

    Returns (invoice_bolt11, payment_hash_hex, expires_at). On any
    failure — no API key, unreachable ZBD, bad response — returns
    (None, None, None): a clean failure, never a crash.

    The payment_hash comes from decoding the issued invoice itself
    (authoritative), never from the payer. Store it with the challenge;
    the payer's X-PAYMENT preimage must hash to it.
    """
    if not lightning_enabled():
        return (None, None, None)
    try:
        msats = msats_to_int(amount_msats)
    except ValueError:
        return (None, None, None)
    if msats <= 0:
        return (None, None, None)
    desc = str(description or "")[:150]
    if not desc:
        return (None, None, None)

    resp, err = _charge_request(msats, desc, expires_secs)
    if err or resp is None:
        return (None, None, None)
    parsed, perr = _parse_charge(resp)
    if perr or not parsed:
        return (None, None, None)
    try:
        dec = decode_invoice(parsed["bolt11"])
    except ValueError:
        return (None, None, None)
    # ZBD must have minted exactly the amount we asked for.
    if dec["amount_msats"] != msats:
        return (None, None, None)
    if (parsed["zbd_amount_msats"] is not None
            and parsed["zbd_amount_msats"] != msats):
        return (None, None, None)
    expires_at = parsed["expires_at"] or dec["expires_at"]
    return (parsed["bolt11"], dec["payment_hash"], expires_at)


# --------------------------------------------------------------------------
# verification (the preimage checklist)
# --------------------------------------------------------------------------

def verify_lightning_payment(preimage_hex, payment_hash_hex, amount_msats,
                             min_msats, expires_at, used_set) \
        -> tuple[bool, dict]:
    """Verify a Lightning payment by preimage proof. Returns (ok, info).
    READ-ONLY: never mutates used_set — the caller consumes the replay
    key after success.

    preimage_hex:  64 lowercase hex chars (32-byte preimage), from
                   the payer's X-PAYMENT payload.
    payment_hash_hex: the invoice's payment_hash, stored when the 402
                   challenge was issued — the challenge binding.
    amount_msats:  the invoice amount, integer millisats.
    min_msats:     minimum acceptable amount, integer millisats.
    expires_at:    invoice expiry as unix time; now must be before it.
    used_set:      replay store (checked read-only).
    """
    payment_hash_hex = (payment_hash_hex or "").strip().lower()
    if not valid_payment_hash_hex(payment_hash_hex):
        return _clean_fail("bad payment_hash format (want 64 hex chars)")
    key = preimage_replay_key(payment_hash_hex)
    if key in used_set:
        return _clean_fail("replay: preimage already used", replay_key=key)

    preimage_hex = (preimage_hex or "").strip().lower()
    if not valid_preimage_hex(preimage_hex):
        return _clean_fail("bad preimage format (want 64 hex chars, "
                           "the 32-byte BOLT11 preimage)", replay_key=key)

    try:
        min_msats = msats_to_int(min_msats)
    except ValueError:
        return _clean_fail("bad min_msats %r" % (min_msats,),
                           replay_key=key)
    if min_msats <= 0:
        return _clean_fail("min_msats must be positive", replay_key=key)
    try:
        amount_msats = msats_to_int(amount_msats)
    except ValueError:
        return _clean_fail("bad amount_msats %r" % (amount_msats,),
                           replay_key=key)
    if amount_msats < min_msats:
        return _clean_fail("underpaid: invoice %d msats < need %d msats"
                           % (amount_msats, min_msats),
                           amount_msats=amount_msats, replay_key=key)

    try:
        expires_at = int(expires_at)
    except (ValueError, TypeError):
        return _clean_fail("bad expires_at %r" % (expires_at,),
                           replay_key=key)
    if not time.time() < expires_at:
        return _clean_fail("invoice expired", replay_key=key)

    # --- the cryptographic check: preimage must hash to the invoice's
    # payment_hash. SHA256 over the raw 32 bytes. ---
    digest = hashlib.sha256(bytes.fromhex(preimage_hex)).hexdigest()
    if digest != payment_hash_hex:
        return _clean_fail("preimage does not match invoice payment_hash",
                           replay_key=key)

    return True, {"payment_hash": payment_hash_hex,
                  "amount_msats": amount_msats, "expires_at": expires_at,
                  "replay_key": key}
