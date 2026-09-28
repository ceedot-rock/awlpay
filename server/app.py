#!/usr/bin/env python3
"""awLPay v1 — payment service (stdlib http.server).

Settlement modes via AWL_EXECUTION_MODE: "mock" (default, local only —
coins never move), "dryrun" (real tx build+sign+simulate on testnets),
"broadcast" (gated real broadcast on testnets). Mainnet stays
hard-disabled (AWL_MAINNET_ENABLED=1 -> loud refusal; mainnet chain ids
are refused in code with no override).

Endpoints:
    POST /api/pay/quote    FREE.  Validate a conversion: hasValue checks
                           on both tokens, router.find_path, and fee math
                           for all three tiers. 200 with {path, fees, ...}
                           or 200 with {refused: true, reason}.
    POST /api/pay/execute   402-GATED. Route price: 25 cents flat (v1).
                           Unpaid/bad payment -> 402 + payment
                           requirements. In AWL_TEST_MODE=1, header
                           X-Test-Payment: ok counts as paid (loud log
                           line — NO real payment is verified). Otherwise
                           this endpoint has no real rail wired and every
                           unpaid request 402s.
    GET  /health            200 {ok: true, mode}.

Refusals are HTTP 200 with {"refused": true, "reason"} — honest,
machine-readable, not errors.

Production note: stdlib http.server is fine for v1 local use. A real
deployment would want an ASGI server (uvicorn/hypercorn) behind a
reverse proxy, real rate limiting, and persistent state.

Env:
    AWL_PORT            listen port (default 8899)
    AWL_ORACLE          "coingecko" for live prices, anything else = MockOracle
    AWL_TEST_MODE       "1" enables the X-Test-Payment test gate on /execute
    AWL_RELAYER_KEY     64-hex-char Ed25519 seed (see chamber.py custody note)
    AWL_MAINNET_ENABLED "1" -> settlement refuses loudly (v1 has no mainnet)
    AWL_MODE            reported in /health (default "mock-local")
    AWL_EXECUTION_MODE  settlement mode: "mock" (default), "dryrun"
                        (build+sign+simulate on testnets, no broadcast),
                        "broadcast" (dryrun + real broadcast, gated)
    AWL_BROADCAST       "1" opens the broadcast gate (testnets only;
                        without it broadcast mode RAISES)
    AWL_SETTLER_KEY_<chain>
                        64-hex settler seed per chain (ethereum, base,
                        polygon, arbitrum, solana); unset -> throwaway
    AWL_SETTLER_RECIPIENT
                        default recipient for broadcast mode
    AWL_RPC_<chain>     override the testnet RPC URL per chain
    AWL_USDC_<chain>    override the testnet USDC contract (EVM)
    AWL_USDC_MINT_solana
                        override the devnet USDC mint
"""

from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .chamber import load_relayer_keys
from .fees import calculate_fees, FREE, PRO, L33T
from .oracle import PriceOracle, CoinGeckoOracle, default_mock_oracle
from .router import ConverterRouter
from .settlement import execute_quote

PORT = int(os.environ.get("AWL_PORT", "8899"))
MODE = os.environ.get("AWL_MODE", "mock-local")
EXECUTE_PRICE_CENTS = 25  # flat v1 route price for /api/pay/execute

# x402-style envelope shape (mirrors rider-x402's unpaid -> 402 shape).
X402_VERSION = 1
PAY_TO = os.environ.get("AWL_PAY_TO", "").strip() or "0x0000000000000000000000000000000000000000"
PAY_NETWORK = "eip155:8453"  # Base; v1 default rail
PAY_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # native USDC, Base

MAX_BODY = 64 * 1024

_oracle: PriceOracle | None = None
_router = ConverterRouter()
_signing_key = None
_nonce_lock = threading.Lock()
_consumed_nonces: set[str] = set()


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


def payment_requirements(host: str) -> dict:
    return {
        "x402Version": X402_VERSION,
        "error": ("payment required: pay %d cents, then retry with "
                  "X-Test-Payment: ok (test mode only)" % EXECUTE_PRICE_CENTS),
        "accepts": [{
            "scheme": "exact",
            "network": PAY_NETWORK,
            "maxAmountRequired": str(EXECUTE_PRICE_CENTS),
            "price_cents": EXECUTE_PRICE_CENTS,
            "asset": PAY_ASSET,
            "payTo": PAY_TO,
            "resource": "https://%s/api/pay/execute" % host,
            "description": ("awLPay v1 /api/pay/execute — %d cents flat per "
                            "execution (route price)" % EXECUTE_PRICE_CENTS),
            "mimeType": "application/json",
            "maxTimeoutSeconds": 300,
        }],
    }


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

    to_address = body.get("to_address")
    if to_address is not None and (
            not isinstance(to_address, str) or not to_address.strip()):
        return None, _bad(body, "to_address",
                           "must be a non-empty string when present")

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
        "to_address": to_address.strip() if to_address else None,
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


def _payment_ok(headers) -> bool:
    """Test-mode gate: X-Test-Payment: ok + AWL_TEST_MODE=1 counts as paid.
    LOUD because no real payment is verified — do not mistake for real."""
    if os.environ.get("AWL_TEST_MODE", "0") != "1":
        return False
    if (headers.get("X-Test-Payment") or "").strip().lower() == "ok":
        print("TEST MODE — no real payment verified (X-Test-Payment accepted)",
              file=sys.stderr, flush=True)
        return True
    return False


def _consume_nonce(key) -> bool:
    """True if the nonce was fresh and is now consumed; False = replay."""
    if not isinstance(key, str) or not key:
        return True
    with _nonce_lock:
        if key in _consumed_nonces:
            return False
        _consumed_nonces.add(key)
        return True


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

    # ---- /api/pay/execute (402-GATED) ----
    def _handle_execute(self):
        raw = self._read_body()
        if raw is None:
            return

        if not _payment_ok(self.headers):
            return self._send(
                402,
                payment_requirements(self.headers.get("Host", "localhost")),
                {"PAYMENT-REQUIRED": "1"})

        q, err = parse_quote_body(raw)
        if err:
            return self._send(200, err)

        # Replay protection on the caller's idempotency key.
        if not _consume_nonce(q.get("idempotency_key")):
            return self._send(409, {"error": "replay: idempotency_key already used"})

        quoted = build_quote(q)
        if quoted.get("refused"):
            return self._send(200, quoted)

        quote = dict(q)
        quote["path"] = quoted["path"]
        try:
            result = execute_quote(
                quote,
                {"volume_used_cents": q["volume_used_cents"],
                 "txs_used": q["txs_used"]},
                signing_key=get_signing_key(),
                oracle=get_oracle())
        except RuntimeError as e:  # mainnet guard / broadcast gate
            return self._send(503, {"error": str(e)})

        return self._send(200, {
            "ok": True,
            "charged_cents": EXECUTE_PRICE_CENTS,
            "route_price_cents": EXECUTE_PRICE_CENTS,
            **result,
        })


def run() -> None:
    get_signing_key()  # fail fast on a bad AWL_RELAYER_KEY
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("awlpay v1 listening on 127.0.0.1:%d (mode=%s)" % (PORT, MODE),
          flush=True)
    print("oracle=%s test_mode=%s" % (os.environ.get("AWL_ORACLE", "mock"),
                                      os.environ.get("AWL_TEST_MODE", "0")),
          flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    run()
