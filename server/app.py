#!/usr/bin/env python3
"""awLPay v1 — payment service (stdlib http.server; local only).

Endpoints:
    POST /api/pay/quote    FREE.  Validate a conversion: hasValue checks
                           on both tokens, router.find_path, and fee math
                           for all three tiers. 200 with {path, fees, ...}
                           or 200 with {refused: true, reason}.
    POST /api/pay/execute   402-GATED. Route price: 25 cents flat (v1).
                           Unpaid/bad payment -> 402 + machine-readable
                           x402 payment terms. Paid callers retry with an
                           X-PAYMENT header carrying a base64url JSON proof
                           of a real on-chain USDC transfer (scheme
                           "exact": EVM txHash + EIP-191 payer binding, or
                           Solana signature) — verified read-only against
                           the configured rail (server/x402.py, ported from
                           the lab's proven rider-x402 service). Each
                           payment hash is accepted once: payment-hash
                           replay -> 409, and the existing idempotency-key
                           replay -> 409.
    GET  /health            200 {ok: true, mode}.

Refusals are HTTP 200 with {"refused": true, "reason"} — honest,
machine-readable, not errors.

Production note: stdlib http.server is fine for v1 local use. A real
deployment would want an ASGI server (uvicorn/hypercorn) behind a
reverse proxy, real rate limiting, and persistent state.

Env:
    AWL_PORT            listen port (default 8899)
    AWL_ORACLE          "coingecko" for live prices, anything else = MockOracle
    AWL_LOCAL_DEV       "1" enables the X-Test-Payment local-dev bypass on
                        /execute (default OFF; loud warning when on).
                        AWL_TEST_MODE is RETIRED and inert.
    AWL_PAY_TO          EVM address receiving USDC (default 0x0...0)
    AWL_PAY_TO_SOL      base58 Solana address receiving SPL USDC (unset
                        disables the Solana rail)
    AWL_RPC_BASE        comma-separated Base JSON-RPC URLs (required for
                        real Base payment verification)
    AWL_RPC_BASE_SEPOLIA / AWL_RPC_POLYGON / AWL_RPC_ARBITRUM /
    AWL_RPC_OPTIMISM / AWL_RPC_SOLANA
                        per-rail JSON-RPC URLs (see server/x402.py)
    AWL_USDC_BASE / AWL_USDC_BASE_SEPOLIA / ... / AWL_USDC_SOL_MINT
                        asset-address overrides (testnet pointing)
    AWL_DEFAULT_NETWORK CAIP-2 id assumed when X-PAYMENT omits network
                        (default "eip155:8453")
    AWL_X402_STATE      optional path persisting used payment hashes
                        (default: in-memory only)
    AWL_RELAYER_KEY     64-hex-char Ed25519 seed (see chamber.py custody note)
    AWL_MAINNET_ENABLED "1" -> settlement refuses loudly (v1 has no mainnet)
    AWL_MODE            reported in /health (default "mock-local")
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import x402
from .chamber import load_relayer_keys
from .fees import calculate_fees, FREE, PRO, L33T
from .oracle import PriceOracle, CoinGeckoOracle, default_mock_oracle
from .router import ConverterRouter
from .settlement import execute_quote

PORT = int(os.environ.get("AWL_PORT", "8899"))
MODE = os.environ.get("AWL_MODE", "mock-local")
EXECUTE_PRICE_CENTS = 25  # flat v1 route price for /api/pay/execute

MAX_BODY = 64 * 1024

_oracle: PriceOracle | None = None
_router = ConverterRouter()
_signing_key = None
_consumed_nonces: set[str] = set()
# Guards the atomic (idempotency-nonce + payment-hash) consume step in
# _handle_execute: check-and-consume happens under one lock so two
# concurrent requests cannot double-spend the same payment proof.
_settle_lock = threading.Lock()


def get_oracle() -> PriceOracle:
    global _oracle
    if _oracle is None:
        if os.environ.get("AWL_ORACLE", "").lower() == "coingecko":
            print("awlpay: oracle=CoinGecko (TRUST ASSUMPTION: centralized feed)",
                  flush=True)
            _oracle = CoinGeckoOracle()
        else:
            _oracle = default_mock_oracle()
    return _oracle


def get_signing_key():
    global _signing_key
    if _signing_key is None:
        _signing_key, _vk, _src = load_relayer_keys()
    return _signing_key


# --------------------------------------------------------------------------
# payment gate
# --------------------------------------------------------------------------

def _local_dev_enabled() -> bool:
    """The X-Test-Payment bypass is ONLY alive behind the explicit
    AWL_LOCAL_DEV=1 flag. Default OFF."""
    return os.environ.get("AWL_LOCAL_DEV", "0") == "1"


def _local_dev_payment(headers) -> bool:
    """Local-dev bypass: X-Test-Payment: ok counts as paid, but ONLY when
    AWL_LOCAL_DEV=1. LOUD on every use — no real payment is verified."""
    if not _local_dev_enabled():
        return False
    if (headers.get("X-Test-Payment") or "").strip().lower() == "ok":
        print("LOCAL DEV MODE — no real payment verified "
              "(X-Test-Payment accepted; AWL_LOCAL_DEV=1)",
              file=sys.stderr, flush=True)
        return True
    return False


def _402(host: str, reason: str | None = None) -> tuple[int, dict, dict]:
    return (402, x402.payment_terms(host, EXECUTE_PRICE_CENTS, reason=reason),
            {"PAYMENT-REQUIRED": "1"})


def _payment_response_header(info: dict) -> dict:
    """X-PAYMENT-RESPONSE: base64url JSON acknowledging the settled
    payment (x402 convention)."""
    import base64 as _b64
    return {"X-PAYMENT-RESPONSE": _b64.urlsafe_b64encode(json.dumps({
        "x402Version": x402.X402_VERSION,
        "success": True,
        "transaction": info["tx"],
        "network": info["network"],
        "payer": info.get("payer"),
    }).encode()).decode("ascii")}


# --------------------------------------------------------------------------
# shared validation / quote logic
# --------------------------------------------------------------------------
TIER_NAMES = {FREE: "free", PRO: "pro", L33T: "l33t"}


def _bad(body: dict, field: str, want: str) -> dict | None:
    return {"refused": True, "reason": "bad_request",
            "detail": "field %r %s" % (field, want)}


def parse_quote_body(raw: bytes) -> tuple[dict | None, dict | None]:
    """Returns (quote_fields, error_dict). error_dict is a 200-refusal."""
    try:
        body = json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError):
        return None, {"refused": True, "reason": "bad_request",
                      "detail": "body must be JSON"}
    if not isinstance(body, dict):
        return None, {"refused": True, "reason": "bad_request",
                      "detail": "body must be a JSON object"}

    for f in ("from_chain", "from_token", "to_chain", "to_token"):
        if not isinstance(body.get(f), str) or not body[f].strip():
            return None, _bad(body, f, "must be a non-empty string")

    amount = body.get("amount_cents")
    if isinstance(amount, bool) or not isinstance(amount, int):
        return None, _bad(body, "amount_cents", "must be an integer (cents)")
    tier = body.get("tier", FREE)
    if isinstance(tier, bool) or tier not in (FREE, PRO, L33T):
        return None, _bad(body, "tier", "must be 0 (free), 1 (pro) or 2 (l33t)")

    volume_used = body.get("volume_used_cents", 0)
    txs_used = body.get("txs_used", 0)
    for f, v in (("volume_used_cents", volume_used), ("txs_used", txs_used)):
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            return None, _bad(body, f, "must be a non-negative integer")

    return ({
        "from_chain": body["from_chain"].strip().lower(),
        "from_token": body["from_token"].strip().upper(),
        "to_chain": body["to_chain"].strip().lower(),
        "to_token": body["to_token"].strip().upper(),
        "amount_cents": amount,
        "tier": tier,
        "volume_used_cents": volume_used,
        "txs_used": txs_used,
        "idempotency_key": body.get("idempotency_key"),
    }, None)


def build_quote(q: dict) -> dict:
    """Core quote law shared by /quote and /execute. Returns the 200 body."""
    oracle = get_oracle()

    if q["amount_cents"] < 0:
        return {"refused": True, "reason": "refused_negative_amount"}

    # 1. hasValue on BOTH tokens (oracle price not None).
    if not oracle.has_value(q["from_chain"], q["from_token"]):
        return {"refused": True, "reason": "unknown_token_no_price",
                "detail": "%s on %s has no verifiable price"
                          % (q["from_token"], q["from_chain"])}
    if not oracle.has_value(q["to_chain"], q["to_token"]):
        return {"refused": True, "reason": "unknown_token_no_price",
                "detail": "%s on %s has no verifiable price"
                          % (q["to_token"], q["to_chain"])}

    # 2. Conversion path must exist.
    path = _router.find_path(q["from_chain"], q["from_token"],
                             q["to_chain"], q["to_token"], oracle)
    if path is None:
        return {"refused": True, "reason": "no_conversion_path",
                "detail": "no priced route %s/%s -> %s/%s"
                          % (q["from_chain"], q["from_token"],
                             q["to_chain"], q["to_token"])}

    # 3. Fee math for ALL three tiers; dust refusal from the REQUESTED tier.
    fees: dict[str, dict] = {}
    for tier_id in (FREE, PRO, L33T):
        fee_cents, status = calculate_fees(q["amount_cents"], tier_id,
                                           q["volume_used_cents"], q["txs_used"])
        fees[TIER_NAMES[tier_id]] = {
            "fee_cents": fee_cents,
            "status": status,
            "net_cents": q["amount_cents"] - fee_cents,
        }

    requested = fees[TIER_NAMES[q["tier"]]]
    if requested["net_cents"] <= 0:
        return {"refused": True, "reason": "dust_eaten_by_fees",
                "detail": "fee %d¢ >= amount %d¢ on tier %s"
                          % (requested["fee_cents"], q["amount_cents"],
                             TIER_NAMES[q["tier"]]),
                "fees": fees}

    return {
        "path": path,
        "fees": fees,
        "tier": TIER_NAMES[q["tier"]],
        "net_cents": requested["net_cents"],
        "amount_cents": q["amount_cents"],
    }


def _consume_execution(idempotency_key,
                       payment_replay_key: str | None) -> str | None:
    """Atomically consume the idempotency nonce AND the payment hash.
    Returns None on success, or the 409 error string on replay.

    The idempotency nonce is checked FIRST so a caller replaying an old
    idempotency key does not burn a fresh payment proof on the 409.
    """
    with _settle_lock:
        if isinstance(idempotency_key, str) and idempotency_key:
            if idempotency_key in _consumed_nonces:
                return "replay: idempotency_key already used"
        if payment_replay_key is not None:
            if x402.is_payment_used(payment_replay_key):
                return "replay: payment already used"
        if isinstance(idempotency_key, str) and idempotency_key:
            _consumed_nonces.add(idempotency_key)
        if payment_replay_key is not None:
            x402.mark_payment_used(payment_replay_key)
    return None


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "awlpay/1.0"

    def _send(self, code: int, obj: dict, extra: dict | None = None) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes | None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._send(413, {"error": "body too large"})
            return None
        return self.rfile.read(length) if length else b""

    def log_message(self, fmt, *args):  # quiet; audits go to stderr prints
        pass

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/health":
            return self._send(200, {"ok": True, "mode": MODE})
        if path == "/":
            return self._send(200, {
                "service": "awlpay",
                "version": "1.0",
                "mode": MODE,
                "endpoints": ["/api/pay/quote (free)", "/api/pay/execute (402)",
                              "/health"],
                "tiers": {"0": "free: 1.0% + 25¢/tx",
                          "1": "pro: $39/mo, $30k or 500 tx cap",
                          "2": "l33t: $799/mo, fair-use guard"},
            })
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/api/pay/quote":
            return self._handle_quote()
        if path == "/api/pay/execute":
            return self._handle_execute()
        return self._send(404, {"error": "not found"})

    # ---- /api/pay/quote (FREE) ----
    def _handle_quote(self):
        raw = self._read_body()
        if raw is None:
            return
        q, err = parse_quote_body(raw)
        if err:
            return self._send(200, err)
        return self._send(200, build_quote(q))

    # ---- /api/pay/execute (402-GATED, real x402 verification) ----
    def _handle_execute(self):
        raw = self._read_body()
        if raw is None:
            return
        host = self.headers.get("Host", "localhost")
        resource = "https://%s/api/pay/execute" % host

        # ---- payment gate ----
        payment_info: dict | None = None
        payment_replay_key: str | None = None
        if _local_dev_payment(self.headers):
            via = "local-dev"
        else:
            proof, network, payer_sig, perr = x402.parse_x_payment(
                self.headers.get("X-PAYMENT"))
            if perr:
                code, body, extra = _402(host, reason=perr)
                return self._send(code, body, extra)
            # Read-only chain verification: format, receipt, status,
            # USDC Transfer-log sum >= price, payer binding. The used set
            # is only READ here; the payment hash is consumed atomically
            # after the quote validates (see below), so a bad body or a
            # refused quote does not burn the caller's payment.
            pay_to = (x402.sol_pay_to() if network == x402.SOLANA_NETWORK
                      else x402.evm_pay_to())
            ok, info = x402.verify_payment(
                proof, network,
                EXECUTE_PRICE_CENTS * x402.UNITS_PER_CENT,
                pay_to, x402.used_set(),
                rpc=x402.get_rpc(), payer_sig=payer_sig,
                resource=resource)
            if not ok:
                reason = info.get("reason", "payment not verified")
                if reason.startswith("replay:"):
                    return self._send(409, {"error": reason})
                code, body, extra = _402(host, reason=reason)
                return self._send(code, body, extra)
            payment_info = info
            payment_replay_key = info["replay_key"]
            via = "x402"

        q, err = parse_quote_body(raw)
        if err:
            return self._send(200, err)

        quoted = build_quote(q)
        if quoted.get("refused"):
            return self._send(200, quoted)

        # Atomic consume: idempotency nonce first (a nonce replay must not
        # burn a fresh payment), then the payment hash. Concurrent
        # double-spends of one proof serialize here: the loser gets 409.
        conflict = _consume_execution(q.get("idempotency_key"),
                                      payment_replay_key)
        if conflict:
            return self._send(409, {"error": conflict})

        quote = dict(q)
        quote["path"] = quoted["path"]
        try:
            result = execute_quote(
                quote,
                {"volume_used_cents": q["volume_used_cents"],
                 "txs_used": q["txs_used"]},
                signing_key=get_signing_key())
        except RuntimeError as e:  # mainnet guard
            return self._send(503, {"error": str(e)})

        body = {
            "ok": True,
            "charged_cents": EXECUTE_PRICE_CENTS,
            "route_price_cents": EXECUTE_PRICE_CENTS,
            **result,
        }
        extra = None
        if via == "x402":
            body["payment"] = {
                "via": "x402",
                "network": payment_info["network"],
                "tx": payment_info["tx"],
                "payer": payment_info.get("payer"),
                "paid_units": payment_info["paid_units"],
            }
            extra = _payment_response_header(payment_info)
        else:
            body["payment"] = {"via": "local-dev", "verified": False}
        return self._send(200, body, extra)


def run() -> None:
    get_signing_key()  # fail fast on a bad AWL_RELAYER_KEY
    if os.environ.get("AWL_TEST_MODE", "0") == "1":
        print("WARNING: AWL_TEST_MODE is RETIRED and inert — it no longer "
              "enables any payment bypass. Use AWL_LOCAL_DEV=1 for the "
              "local-dev bypass.", file=sys.stderr, flush=True)
    if _local_dev_enabled():
        print("WARNING: AWL_LOCAL_DEV=1 — the X-Test-Payment bypass is "
              "ACTIVE. No real payment is verified on /api/pay/execute. "
              "Never enable outside local development.",
              file=sys.stderr, flush=True)
    else:
        if x402.evm_pay_to() == x402.ZERO_ADDRESS:
            print("WARNING: AWL_PAY_TO unset — EVM payment verification "
                  "will find no USDC transfers (payTo=0x0...0).",
                  file=sys.stderr, flush=True)
        rails = x402.configured_rails()
        if not rails:
            print("WARNING: no x402 payment rail has RPC configured — "
                  "every paid /api/pay/execute will 402. Set AWL_RPC_BASE "
                  "(Base) and AWL_PAY_TO.", file=sys.stderr, flush=True)
        else:
            print("x402 rails: %s" % ", ".join(
                "%s%s" % (r["label"], " [testnet]" if r.get("testnet") else "")
                for _, r in rails), flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("awlpay v1 listening on 127.0.0.1:%d (mode=%s)" % (PORT, MODE),
          flush=True)
    print("oracle=%s local_dev=%s" % (os.environ.get("AWL_ORACLE", "mock"),
                                      "1" if _local_dev_enabled() else "0"),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    run()
