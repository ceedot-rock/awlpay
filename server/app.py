#!/usr/bin/env python3
"""awLPay v1 — payment service (Starlette ASGI + uvicorn).

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
    GET  /healthz           200 {ok: true, version, mode} — Fly http check.

Refusals are HTTP 200 with {"refused": true, "reason"} — honest,
machine-readable, not errors.

Method+path parity with the original stdlib server: a request whose
method doesn't match the route (e.g. GET /api/pay/quote) is a JSON 404,
not a 405 — exactly what the old handler returned. Unknown paths are
JSON 404 {"error": "not found"}. Bodies over 64 KiB are 413.

Every request emits one JSON log line to stdout (see server/logging.py)
and echoes X-Request-Id.

Env:
    AWL_PORT            listen port for run() (default 8899). The PORT env
                        (Fly's convention) takes precedence when set.
    AWL_HOST            bind address for run() (default 127.0.0.1; the
                        Dockerfile CMD passes 0.0.0.0 explicitly)
    AWL_ORACLE          "coingecko" for live prices, anything else = MockOracle
    AWL_TEST_MODE       "1" enables the X-Test-Payment test gate on /execute
    AWL_RELAYER_KEY     64-hex-char Ed25519 seed (see chamber.py custody note)
    AWL_MAINNET_ENABLED "1" -> settlement refuses loudly (v1 has no mainnet)
    AWL_MODE            reported in /health and logs (default "mock-local")
"""

from __future__ import annotations

import json
import os
import sys
import threading
from contextlib import asynccontextmanager

import anyio
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from .chamber import load_relayer_keys
from .fees import calculate_fees, FREE, PRO, L33T
from .logging import RequestLogMiddleware, PathNormalizeMiddleware, log_event
from .oracle import PriceOracle, CoinGeckoOracle, default_mock_oracle
from .router import ConverterRouter
from .settlement import execute_quote

VERSION = "1.0"
PORT = int(os.environ.get("PORT", os.environ.get("AWL_PORT", "8899")))
HOST = os.environ.get("AWL_HOST", "127.0.0.1")
MODE = os.environ.get("AWL_MODE", "mock-local")
EXECUTE_PRICE_CENTS = 25  # flat v1 route price for /api/pay/execute

# x402-style envelope shape (mirrors rider-x402's unpaid -> 402 shape).
X402_VERSION = 1
PAY_TO = os.environ.get("AWL_PAY_TO", "").strip() or "0x0000000000000000000000000000000000000000"
PAY_NETWORK = "eip155:8453"  # Base; v1 default rail
PAY_ASSET = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # native USDC, Base

MAX_BODY = 64 * 1024

# Method sets: every route accepts the common methods and 404s on a
# method mismatch — this is exactly what the old stdlib handler did
# (do_GET/do_POST fell through to the JSON 404).
ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

_oracle: PriceOracle | None = None
_router = ConverterRouter()
_signing_key = None
_nonce_lock = threading.Lock()
_consumed_nonces: set[str] = set()


def get_oracle() -> PriceOracle:
    global _oracle
    if _oracle is None:
        if os.environ.get("AWL_ORACLE", "").lower() == "coingecko":
            log_event("oracle", oracle="coingecko",
                      note="TRUST ASSUMPTION: centralized feed")
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
# shared validation / quote logic (unchanged from the stdlib server)
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


def _payment_ok(headers) -> bool:
    """Test-mode gate: X-Test-Payment: ok + AWL_TEST_MODE=1 counts as paid.
    LOUD because no real payment is verified — do not mistake for real."""
    if os.environ.get("AWL_TEST_MODE", "0") != "1":
        return False
    if (headers.get("x-test-payment") or "").strip().lower() == "ok":
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
# ASGI endpoints
# --------------------------------------------------------------------------
async def _read_body(request) -> bytes | None:
    """Body bytes, or None when the body exceeds MAX_BODY (caller 413s).
    Checks the declared Content-Length first (cheap), then the actual
    bytes (covers chunked requests the old server read as empty)."""
    try:
        declared = int(request.headers.get("content-length") or 0)
    except ValueError:
        declared = 0
    if declared > MAX_BODY:
        return None
    body = await request.body()
    if len(body) > MAX_BODY:
        return None
    return body


def _method_404(request, want: str):
    """JSON 404 for a method mismatch — stdlib-server parity."""
    return JSONResponse({"error": "not found"}, status_code=404)


async def health(request):
    if request.method != "GET":
        return _method_404(request, "GET")
    return JSONResponse({"ok": True, "mode": MODE})


async def healthz(request):
    """Fly http_service check target: 200 + version."""
    if request.method != "GET":
        return _method_404(request, "GET")
    return JSONResponse({"ok": True, "version": VERSION, "mode": MODE})


async def index(request):
    if request.method != "GET":
        return _method_404(request, "GET")
    return JSONResponse({
        "service": "awlpay",
        "version": VERSION,
        "mode": MODE,
        "endpoints": ["/api/pay/quote (free)", "/api/pay/execute (402)",
                      "/health", "/healthz"],
        "tiers": {"0": "free: 1.0% + 25¢/tx",
                  "1": "pro: $39/mo, $30k or 500 tx cap",
                  "2": "l33t: $799/mo, fair-use guard"},
    })


async def quote(request):
    """POST /api/pay/quote (FREE)."""
    if request.method != "POST":
        return _method_404(request, "POST")
    raw = await _read_body(request)
    if raw is None:
        return JSONResponse({"error": "body too large"}, status_code=413)
    q, err = parse_quote_body(raw)
    if err is not None:
        return JSONResponse(err)
    # Blocking oracle/router/fee law runs in a worker thread so the
    # event loop never stalls (same threading semantics as the old
    # ThreadingHTTPServer).
    body = await anyio.to_thread.run_sync(build_quote, q)
    request.state.tier = TIER_NAMES[q["tier"]]
    request.state.price_cents = 0  # quote is free
    return JSONResponse(body)


async def execute(request):
    """POST /api/pay/execute (402-GATED)."""
    if request.method != "POST":
        return _method_404(request, "POST")
    raw = await _read_body(request)
    if raw is None:
        return JSONResponse({"error": "body too large"}, status_code=413)

    request.state.price_cents = EXECUTE_PRICE_CENTS  # route price

    if not _payment_ok(request.headers):
        return JSONResponse(
            payment_requirements(request.headers.get("host", "localhost")),
            status_code=402,
            headers={"PAYMENT-REQUIRED": "1"})

    q, err = parse_quote_body(raw)
    if err is not None:
        return JSONResponse(err)
    request.state.tier = TIER_NAMES[q["tier"]]

    def _do():
        # Replay protection on the caller's idempotency key.
        if not _consume_nonce(q.get("idempotency_key")):
            return 409, {"error": "replay: idempotency_key already used"}
        quoted = build_quote(q)
        if quoted.get("refused"):
            return 200, quoted
        quote = dict(q)
        quote["path"] = quoted["path"]
        try:
            result = execute_quote(
                quote,
                {"volume_used_cents": q["volume_used_cents"],
                 "txs_used": q["txs_used"]},
                signing_key=get_signing_key())
        except RuntimeError as e:  # mainnet guard
            return 503, {"error": str(e)}
        return 200, {
            "ok": True,
            "charged_cents": EXECUTE_PRICE_CENTS,
            "route_price_cents": EXECUTE_PRICE_CENTS,
            **result,
        }

    code, body = await anyio.to_thread.run_sync(_do)
    return JSONResponse(body, status_code=code)


async def not_found(request, exc):
    return JSONResponse({"error": "not found"}, status_code=404)


@asynccontextmanager
async def lifespan(app):
    get_signing_key()  # fail fast on a bad AWL_RELAYER_KEY
    log_event("startup", service="awlpay", version=VERSION, mode=MODE,
              oracle=os.environ.get("AWL_ORACLE", "mock"),
              test_mode=os.environ.get("AWL_TEST_MODE", "0"))
    yield


app = Starlette(
    lifespan=lifespan,
    exception_handlers={404: not_found},
    routes=[
        Route("/health", health, methods=ALL_METHODS),
        Route("/healthz", healthz, methods=ALL_METHODS),
        Route("/", index, methods=ALL_METHODS),
        Route("/api/pay/quote", quote, methods=ALL_METHODS),
        Route("/api/pay/execute", execute, methods=ALL_METHODS),
    ],
)
# Outermost first: normalize the path, then log the (normalized) request.
# (add_middleware appends; Starlette wraps in reverse, so the FIRST
# added ends up OUTERMOST.)
app.add_middleware(PathNormalizeMiddleware)
app.add_middleware(RequestLogMiddleware)


def run() -> None:
    get_signing_key()  # fail fast on a bad AWL_RELAYER_KEY
    import uvicorn
    log_event("startup", service="awlpay", version=VERSION, mode=MODE,
              host=HOST, port=PORT,
              oracle=os.environ.get("AWL_ORACLE", "mock"),
              test_mode=os.environ.get("AWL_TEST_MODE", "0"))
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning",
                access_log=False)


if __name__ == "__main__":
    run()
