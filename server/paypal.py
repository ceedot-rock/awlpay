"""awLPay fiat bridge: PayPal rail (USD <-> USDC).

=====================================================================
SAFETY LAW — READ BEFORE TOUCHING
---------------------------------------------------------------------
* SANDBOX ONLY. The ONLY base URL this module will ever talk to is
  https://api-m.sandbox.paypal.com (hardcoded below). The production
  endpoint https://api-m.paypal.com is HARD-REFUSED by
  assert_sandbox_base() — no env var, no argument, no config can
  override that. A production flip is a deliberate CODE change plus
  Corey's explicit per-charge approval, never a config flip.
* Money-moving calls (capture_order, create_payout) are additionally
  gated on AWL_PAYPAL_PAYOUTS=1. Without it they RAISE instead of
  silently dry-running. create_order alone moves no money (it needs
  buyer approval) and is ungated — but still sandbox-only.
* Credentials: PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET, env only.
  Never hardcoded, never logged, never in error strings.
* Keys/seeds: this rail holds no chain keys. Receipts are Chamber-
  signed with the relayer key (see chamber.py) like every other rail.
* NO real funds move: sandbox play money only. This module must never
  see production credentials.
=====================================================================

Two PayPal REST surfaces:
    Orders v2  — create an order (intent CAPTURE), capture it after the
                 buyer approves. Receiving USD from a payer.
    Payouts    — send USD to an email recipient. Paying out. Needs the
                 Payouts scope enabled on the sandbox REST app.

All network I/O lives in _PayPalHTTP.post/get (urllib). Pure helpers
(usd<->cents math, payload builders, error mapping) are offline and
unit-testable without a network. Pass transport= to PayPalAdapter to
inject a fake transport in tests.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import sys
import urllib.request
import urllib.error
import uuid

from .chamber import sign_attestation, load_relayer_keys
from .fees import calculate_fees, FREE

# --------------------------------------------------------------------------
# Sandbox-only endpoints. THE production-flip surface: replacing these
# constants (a code change) plus Corey's explicit approval is the ONLY
# sanctioned path to production. Nothing here reads a base URL from env.
# --------------------------------------------------------------------------
_SANDBOX_BASE = "https://api-m.sandbox.paypal.com"
_PRODUCTION_BASE = "https://api-m.paypal.com"  # named only to refuse it

_TOKEN_PATH = "/v1/oauth2/token"
_ORDER_PATH = "/v2/checkout/orders"
_PAYOUT_PATH = "/v1/payments/payouts"

_HTTP_TIMEOUT_S = 20

# Money-moving calls need this. Mirrors AWL_BROADCAST in chains.py.
_PAYOUT_GATE_ENV = "AWL_PAYPAL_PAYOUTS"


def assert_sandbox_base(url: str) -> str:
    """Hard guard: only the sandbox base URL is allowed. Returns it.

    Raises RuntimeError on the production base or anything else. No
    env var bypasses this."""
    if url == _SANDBOX_BASE:
        return url
    if url == _PRODUCTION_BASE:
        raise RuntimeError(
            "REFUSED: production PayPal endpoint %r — this rail is "
            "sandbox-only by code; a production flip needs a code change "
            "plus Corey's explicit approval" % url)
    raise RuntimeError(
        "REFUSED: unknown PayPal base URL %r (want sandbox %r)"
        % (url, _SANDBOX_BASE))


def payout_allowed() -> tuple[bool, str]:
    """Payout/capture gate: AWL_PAYPAL_PAYOUTS=1. (ok, reason)."""
    if os.environ.get(_PAYOUT_GATE_ENV, "0") != "1":
        return False, ("refused: %s != 1 — PayPal money movement gate "
                       "closed; not executing" % _PAYOUT_GATE_ENV)
    return True, "ok"


def get_paypal_credentials() -> tuple[str, str]:
    """(client_id, client_secret) from env. Raises cleanly if unset."""
    cid = os.environ.get("PAYPAL_CLIENT_ID", "").strip()
    sec = os.environ.get("PAYPAL_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        raise RuntimeError(
            "PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET are not set — "
            "the PayPal rail needs sandbox REST app credentials "
            "(developer.paypal.com, sandbox app)")
    return cid, sec


# --------------------------------------------------------------------------
# Pure money math. Integer cents everywhere — no floats, same law as
# fees.py and the drops math in xrpl.py.
# --------------------------------------------------------------------------
def usd_to_cents(amount: str) -> int:
    """'10.99' -> 1099. Raises ValueError on bad input. No floats."""
    if not isinstance(amount, str):
        raise ValueError("amount must be a string like '10.99'")
    s = amount.strip()
    if not s:
        raise ValueError("amount must not be empty")
    neg = s.startswith("-")
    if neg:
        s = s[1:]
    if "." in s:
        whole, frac = s.split(".", 1)
        if len(frac) > 2 or not frac.isdigit():
            raise ValueError("amount %r: at most 2 decimal places" % amount)
        frac = (frac + "00")[:2]
    else:
        whole, frac = s, "00"
    if not whole.isdigit() or not whole:
        raise ValueError("amount %r is not a valid USD amount" % amount)
    cents = int(whole) * 100 + int(frac)
    return -cents if neg else cents


def cents_to_usd_str(cents: int) -> str:
    """1099 -> '10.99'. Integer in, canonical string out."""
    if not isinstance(cents, int):
        raise ValueError("cents must be int")
    neg = cents < 0
    c = abs(cents)
    s = "%d.%02d" % (c // 100, c % 100)
    return ("-" + s) if neg else s


def quote_fiat_bridge(usd_cents: int, tier: int = FREE) -> dict:
    """Pure quote: USD in -> USDC out (micro-USDC, 6dp like chains.py).

    1 USD = 1 USDC by definition (unit of account); the lab fee from
    fees.calculate_fees applies on the USD leg. Integer math only.
    Raises ValueError on non-positive amounts."""
    if not isinstance(usd_cents, int) or usd_cents <= 0:
        raise ValueError("usd_cents must be a positive integer")
    fee_cents, status = calculate_fees(usd_cents, tier, 0, 0)
    net_cents = usd_cents - fee_cents
    # 1 cent = 10,000 micro-USDC (USDC has 6 decimals).
    usdc_micro = net_cents * 10_000
    return {
        "usd_cents": usd_cents,
        "fee_cents": fee_cents,
        "fee_status": status,
        "net_cents": net_cents,
        "usdc_micro": usdc_micro,
        "rate": "1 USD = 1 USDC",
    }


# --------------------------------------------------------------------------
# HTTP transport. Real urllib by default; injectable for tests.
# transport(method, url, headers, body_json) -> (status_code, resp_dict)
# --------------------------------------------------------------------------
def _real_transport(method: str, url: str, headers: dict,
                    body: dict | None) -> tuple[int, dict]:
    if url.startswith(_SANDBOX_BASE):
        pass
    elif url.startswith(_PRODUCTION_BASE):
        # Production host with any path — refuse with the production
        # message, not the generic unknown-base one.
        assert_sandbox_base(_PRODUCTION_BASE)
    else:
        assert_sandbox_base(url)  # raises: unknown base
    ctype = (headers.get("Content-Type") or "")
    if body is not None and "x-www-form-urlencoded" in ctype:
        data = urllib.parse.urlencode(body).encode()
    else:
        data = (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers))
    try:
        with urllib.request.urlopen(req,
                                    timeout=_HTTP_TIMEOUT_S) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            ebody = json.loads(raw) if raw.strip() else {}
        except ValueError:
            ebody = {"raw": raw[:300]}
        return e.code, ebody
    except Exception as e:  # noqa: BLE001 — transport errors are data
        return 0, {"transport_error": "%s: %s"
                   % (type(e).__name__, str(e)[:200])}


def _paypal_error(status: int, body: dict, action: str) -> RuntimeError:
    """Map PayPal error responses to clean, actionable errors.

    Never includes credentials (they never appear in responses anyway);
    truncates PayPal's message to keep logs sane."""
    detail = ""
    if isinstance(body, dict):
        # Orders/captures nest under details[]; payouts use message.
        details = body.get("details")
        if isinstance(details, list) and details:
            d0 = details[0]
            detail = "%s: %s" % (d0.get("issue", "?"),
                                 str(d0.get("description", ""))[:200])
        elif body.get("message"):
            detail = str(body["message"])[:200]
        elif body.get("transport_error"):
            detail = str(body["transport_error"])[:200]
    if status == 401:
        return RuntimeError(
            "PayPal auth failed during %s — check PAYPAL_CLIENT_ID / "
            "PAYPAL_CLIENT_SECRET (sandbox app)" % action)
    if status == 422 and "INSUFFICIENT_FUNDS" in detail:
        return RuntimeError(
            "PayPal refused %s: insufficient sandbox funds" % action)
    if status == 403:
        return RuntimeError(
            "PayPal refused %s (403): the sandbox REST app may lack the "
            "required scope (Payouts must be enabled at "
            "developer.paypal.com). Detail: %s" % (action, detail))
    return RuntimeError(
        "PayPal %s failed (HTTP %d): %s" % (action, status, detail or "?"))


class PayPalAdapter:
    """PayPal sandbox rail: Orders v2 (receive) + Payouts (send).

    transport is injectable for tests; default is the real sandbox HTTP.
    """

    def __init__(self, transport=None):
        self.transport = transport or _real_transport
        self._token: str | None = None
        self._token_exp: float = 0.0

    # -- auth ------------------------------------------------------------
    def get_access_token(self) -> str:
        """OAuth2 client_credentials. Cached in-memory until expiry.

        PayPal's token endpoint takes form encoding; the transport
        honors the Content-Type header (the real one urlencodes, fakes
        in tests just return canned responses)."""
        import time
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        cid, sec = get_paypal_credentials()
        basic = base64.b64encode(("%s:%s" % (cid, sec)).encode()).decode()
        url = _SANDBOX_BASE + _TOKEN_PATH
        status, body = self.transport(
            "POST", url,
            {"Authorization": "Basic " + basic,
             "Content-Type": "application/x-www-form-urlencoded",
             "Accept": "application/json"},
            {"grant_type": "client_credentials"})
        if status != 200 or not isinstance(body, dict) \
                or not body.get("access_token"):
            raise _paypal_error(status, body, "oauth2 token")
        self._token = body["access_token"]
        try:
            self._token_exp = now + int(body.get("expires_in", 3600) or 3600)
        except (ValueError, TypeError):
            self._token_exp = now + 3600
        return self._token

    def _authed(self, method: str, path: str,
                payload: dict | None = None,
                idempotency_key: str | None = None) -> tuple[int, dict]:
        """Authenticated JSON call against the sandbox API."""
        token = self.get_access_token()
        headers = {"Authorization": "Bearer " + token,
                   "Accept": "application/json",
                   "Content-Type": "application/json"}
        if idempotency_key:
            headers["PayPal-Request-Id"] = idempotency_key
        url = _SANDBOX_BASE + path
        return self.transport(method, url, headers, payload)

    # -- orders (receive USD) --------------------------------------------
    def create_order(self, amount_cents: int,
                     idempotency_key: str | None = None) -> dict:
        """Create a CAPTURE-intent order. Returns {order_id, approve_url,
        status}. Moves no money — the payer must approve via approve_url."""
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ValueError("amount_cents must be a positive integer")
        key = idempotency_key or ("awlpay-order-" + uuid.uuid4().hex)
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {"currency_code": "USD",
                           "value": cents_to_usd_str(amount_cents)},
            }],
        }
        status, body = self._authed("POST", _ORDER_PATH, payload, key)
        if status not in (200, 201) or not body.get("id"):
            raise _paypal_error(status, body, "create order")
        approve = ""
        for link in body.get("links", []) or []:
            if link.get("rel") == "approve":
                approve = link.get("href", "")
        return {"order_id": body["id"], "status": body.get("status"),
                "approve_url": approve,
                "amount_cents": amount_cents, "currency": "USD"}

    def get_order(self, order_id: str) -> dict:
        """Fetch order status (CREATED/APPROVED/COMPLETED/...)."""
        if not order_id or not isinstance(order_id, str):
            raise ValueError("order_id must be a non-empty string")
        status, body = self._authed(
            "GET", "%s/%s" % (_ORDER_PATH, order_id))
        if status != 200 or not body.get("id"):
            raise _paypal_error(status, body, "get order")
        return {"order_id": body["id"], "status": body.get("status"),
                "amount_cents": _units_amount_cents(body)}

    def capture_order(self, order_id: str,
                      idempotency_key: str | None = None) -> dict:
        """Capture an approved order. MOVES sandbox money — gated."""
        ok, reason = payout_allowed()
        if not ok:
            raise RuntimeError(reason)
        if not order_id or not isinstance(order_id, str):
            raise ValueError("order_id must be a non-empty string")
        key = idempotency_key or ("awlpay-capture-" + uuid.uuid4().hex)
        status, body = self._authed(
            "POST", "%s/%s/capture" % (_ORDER_PATH, order_id), {}, key)
        if status not in (200, 201):
            raise _paypal_error(status, body, "capture order")
        captures = (((body.get("purchase_units") or [{}])[0]
                     .get("payments") or {}).get("captures") or [])
        cap = captures[0] if captures else {}
        return {"order_id": body.get("id"),
                "status": body.get("status"),
                "capture_id": cap.get("id"),
                "amount_cents": _units_amount_cents(body),
                "currency": "USD"}

    # -- payouts (send USD) ------------------------------------------------
    def create_payout(self, recipient_email: str, amount_cents: int,
                      note: str = "",
                      idempotency_key: str | None = None) -> dict:
        """Send USD to an email recipient. MOVES sandbox money — gated on
        AWL_PAYPAL_PAYOUTS=1. Returns {batch_id, batch_status}."""
        ok, reason = payout_allowed()
        if not ok:
            raise RuntimeError(reason)
        if not recipient_email or "@" not in recipient_email:
            raise ValueError("recipient_email must be a valid email address")
        if not isinstance(amount_cents, int) or amount_cents <= 0:
            raise ValueError("amount_cents must be a positive integer")
        key = idempotency_key or ("awlpay-payout-" + uuid.uuid4().hex)
        payload = {
            "sender_batch_header": {
                "sender_batch_id": key,
                "email_subject": "awLPay sandbox payout",
            },
            "items": [{
                "recipient_type": "EMAIL",
                "amount": {"value": cents_to_usd_str(amount_cents),
                           "currency": "USD"},
                "receiver": recipient_email,
                "note": note[:500],
            }],
        }
        status, body = self._authed("POST", _PAYOUT_PATH, payload, key)
        if status not in (200, 201):
            raise _paypal_error(status, body, "create payout")
        header = body.get("batch_header") or {}
        return {"batch_id": header.get("payout_batch_id"),
                "batch_status": header.get("batch_status"),
                "amount_cents": amount_cents, "currency": "USD",
                "recipient": recipient_email}

    def get_payout(self, batch_id: str) -> dict:
        """Fetch payout batch status."""
        if not batch_id or not isinstance(batch_id, str):
            raise ValueError("batch_id must be a non-empty string")
        status, body = self._authed(
            "GET", "%s/%s" % (_PAYOUT_PATH, batch_id))
        if status != 200:
            raise _paypal_error(status, body, "get payout")
        header = body.get("batch_header") or {}
        return {"batch_id": header.get("payout_batch_id"),
                "batch_status": header.get("batch_status")}


def _units_amount_cents(body: dict) -> int | None:
    """Extract the first purchase_unit amount as cents, or None."""
    try:
        units = body.get("purchase_units") or []
        amt = (units[0].get("amount") or {})
        if amt.get("currency_code") != "USD":
            return None
        return usd_to_cents(str(amt.get("value", "0")))
    except (ValueError, IndexError, AttributeError):
        return None


# --------------------------------------------------------------------------
# Chamber-signed bridge receipts. Every bridge payment produces one —
# same envelope shape as settlement.py.
# --------------------------------------------------------------------------
def bridge_receipt(kind: str, paypal_id: str, amount_cents: int,
                   direction: str, mode: str = "sandbox",
                   extra: dict | None = None) -> dict:
    """Receipt payload dict (unsigned). kind: order|capture|payout."""
    if kind not in ("order", "capture", "payout"):
        raise ValueError("kind must be order|capture|payout")
    if direction not in ("in", "out"):
        raise ValueError("direction must be 'in' (receive) or 'out' (send)")
    receipt = {
        "rail": "paypal",
        "kind": kind,
        "paypal_id": paypal_id,
        "amount_cents": amount_cents,
        "currency": "USD",
        "direction": direction,
        "sandbox": True,
        "mode": mode,  # sandbox | dry_run
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    if extra:
        receipt.update(extra)
    return receipt


def sign_bridge_receipt(receipt: dict, signing_key=None) -> dict:
    """Chamber-sign a bridge receipt. Returns the attestation envelope."""
    if signing_key is None:
        signing_key, _vk, _src = load_relayer_keys()
    return {"attestation": sign_attestation(receipt, signing_key)}


def main(argv: list[str]) -> int:
    """Tiny CLI: quote only (offline)."""
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print("usage: python -m server.paypal <usd_amount>   "
              "(e.g. 10.00) — prints the fiat-bridge quote (offline)",
              file=sys.stderr)
        return 2
    try:
        cents = usd_to_cents(argv[1])
    except ValueError as e:
        print("bad amount: %s" % e, file=sys.stderr)
        return 2
    q = quote_fiat_bridge(cents)
    print(json.dumps(q, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
