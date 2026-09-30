#!/usr/bin/env python3
"""awLPay v1 — payment service (Starlette ASGI + uvicorn).

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
    POST /api/pay/execute   402-GATED. Route price: 1 cent, cost-plus
                            (measured compute + Base RPC for x402
                            verification, x4 margin, 1c floor — see
                            server/fees.py and PRICING.md). Paid callers
                            retry with an X-PAYMENT header carrying a
                            base64url JSON proof of a real on-chain USDC
                            transfer (scheme "exact": EVM txHash + EIP-191
                            payer binding, or Solana signature) — verified
                            read-only against the configured rail
                            (server/x402.py, ported from the lab's proven
                            rider-x402 service). Unpaid/bad payment -> 402 +
                            payment requirements. Payment-hash replay -> 409,
                            idempotency-key replay -> 409. With
                            AWL_LOCAL_DEV=1, header X-Test-Payment: ok counts
                            as paid (loud log line — NO real payment is
                            verified).
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
    AWL_MODE            reported in /health and logs (default "mock-local")
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

import functools
import json
import os
import sys
import threading
from contextlib import asynccontextmanager

import anyio
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from . import x402
from .chamber import load_relayer_keys
from .fees import calculate_fees, FREE, PRO, L33T, ROUTE_PRICE_CENTS
from .logging import RequestLogMiddleware, PathNormalizeMiddleware, log_event
from .oracle import PriceOracle, CoinGeckoOracle, default_mock_oracle
from .router import ConverterRouter
from .settlement import execute_quote
from .toll import deposit_verify as toll_deposit_verify
from .toll import toll_send as toll_send_ep

VERSION = "1.0"
PORT = int(os.environ.get("PORT", os.environ.get("AWL_PORT", "8899")))
HOST = os.environ.get("AWL_HOST", "127.0.0.1")
MODE = os.environ.get("AWL_MODE", "mock-local")
# Cost-plus route toll, derived in server/fees.py (== 1). NOT a flat pick:
# re-derive by re-running the benchmark and updating the fee constants.
EXECUTE_PRICE_CENTS = ROUTE_PRICE_CENTS

MAX_BODY = 64 * 1024

# Method sets: every route accepts the common methods and 404s on a
# method mismatch — this is exactly what the old stdlib handler did
# (do_GET/do_POST fell through to the JSON 404).
ALL_METHODS = ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]

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
    """POST /api/pay/execute (402-GATED, real x402 verification)."""
    if request.method != "POST":
        return _method_404(request, "POST")
    raw = await _read_body(request)
    if raw is None:
        return JSONResponse({"error": "body too large"}, status_code=413)

    request.state.price_cents = EXECUTE_PRICE_CENTS  # route price (1c cost-plus)

    host = request.headers.get("host", "localhost")
    resource = "https://%s/api/pay/execute" % host

    # ---- payment gate: real x402 verification, read-only against the rail.
    # The X-Test-Payment local-dev bypass is ONLY alive behind the explicit
    # AWL_LOCAL_DEV=1 flag (default OFF; loud on every use).
    payment_info: dict | None = None
    payment_replay_key: str | None = None
    if _local_dev_payment(request.headers):
        via = "local-dev"
    else:
        proof, network, payer_sig, perr = x402.parse_x_payment(
            request.headers.get("X-PAYMENT"))
        if perr:
            code, body, extra = _402(host, reason=perr)
            return JSONResponse(body, status_code=code, headers=extra)
        # Read-only chain verification: format, receipt, status, USDC
        # Transfer-log sum >= price, payer binding. The used set is only
        # READ here; the payment hash is consumed atomically after the
        # quote validates (below), so a bad body or a refused quote does
        # not burn the caller's payment.
        pay_to = (x402.sol_pay_to() if network == x402.SOLANA_NETWORK
                  else x402.evm_pay_to())
        ok, info = await anyio.to_thread.run_sync(
            functools.partial(
                x402.verify_payment,
                proof, network,
                EXECUTE_PRICE_CENTS * x402.UNITS_PER_CENT,
                pay_to, x402.used_set(),
                rpc=x402.get_rpc(), payer_sig=payer_sig,
                resource=resource))
        if not ok:
            reason = info.get("reason", "payment not verified")
            if reason.startswith("replay:"):
                return JSONResponse({"error": reason}, status_code=409)
            code, body, extra = _402(host, reason=reason)
            return JSONResponse(body, status_code=code, headers=extra)
        payment_info = info
        payment_replay_key = info["replay_key"]
        via = "x402"

    q, err = parse_quote_body(raw)
    if err is not None:
        return JSONResponse(err)
    request.state.tier = TIER_NAMES[q["tier"]]

    def _do():
        quoted = build_quote(q)
        if quoted.get("refused"):
            return 200, quoted, None
        # Atomic consume: idempotency nonce first (a nonce replay must not
        # burn a fresh payment), then the payment hash. Concurrent
        # double-spends of one proof serialize here: the loser gets 409.
        conflict = _consume_execution(q.get("idempotency_key"),
                                      payment_replay_key)
        if conflict:
            return 409, {"error": conflict}, None
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
            return 503, {"error": str(e)}, None
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
        return 200, body, extra

    code, body, extra = await anyio.to_thread.run_sync(_do)
    return JSONResponse(body, status_code=code, headers=extra or {})


async def not_found(request, exc):
    return JSONResponse({"error": "not found"}, status_code=404)


@asynccontextmanager
async def lifespan(app):
    get_signing_key()  # fail fast on a bad AWL_RELAYER_KEY
    log_event("startup", service="awlpay", version=VERSION, mode=MODE,
              oracle=os.environ.get("AWL_ORACLE", "mock"),
              local_dev=os.environ.get("AWL_LOCAL_DEV", "0"))
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
        Route("/internal/toll/deposit/verify", toll_deposit_verify,
              methods=ALL_METHODS),
        Route("/internal/toll/send", toll_send_ep, methods=ALL_METHODS),
    ],
)
# Outermost first: normalize the path, then log the (normalized) request.
# (add_middleware appends; Starlette wraps in reverse, so the FIRST
# added ends up OUTERMOST.)
app.add_middleware(PathNormalizeMiddleware)
app.add_middleware(RequestLogMiddleware)


def run() -> None:
    get_signing_key()  # fail fast on a bad AWL_RELAYER_KEY
def run() -> None:
    get_signing_key()  # fail fast on a bad AWL_RELAYER_KEY
    import uvicorn
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
    log_event("startup", service="awlpay", version=VERSION, mode=MODE,
              host=HOST, port=PORT,
              oracle=os.environ.get("AWL_ORACLE", "mock"),
              local_dev=os.environ.get("AWL_LOCAL_DEV", "0"))
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning",
                access_log=False)


if __name__ == "__main__":
    run()
