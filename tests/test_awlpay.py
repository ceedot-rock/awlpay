"""awLPay v1 tests: fee exactness, oracle/router law, chamber signatures,
settlement refusal law, and HTTP end-to-end (quote free, execute 402)."""

import json
import os
import sys
import threading
import urllib.request

# Repo root derived from THIS file (not a hardcoded sibling checkout):
# test_awlpay.py must import the server/ under test, not another copy.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from server import fees, oracle, router, chamber, settlement, x402  # noqa: E402
from server.oracle import MockOracle  # noqa: E402
from server.router import ConverterRouter  # noqa: E402


# ---------------------------------------------------------------- fees
def test_fee_exactness_core_cases():
    # Free: 0.5% flat, no fixed fee
    assert fees.calculate_fees(10_000, 0, 0, 0) == (50, "free")
    assert fees.calculate_fees(99, 0, 0, 0) == (0, "free")       # floors to 0
    assert fees.calculate_fees(101, 0, 0, 0) == (0, "free")      # floors to 0
    assert fees.calculate_fees(400, 0, 0, 0) == (2, "free")      # 0.5% of $4
    # Pro under cap: 0 fee
    assert fees.calculate_fees(10_000, 1, 0, 0) == (0, "pro_under_cap")
    # Pro at exactly the caps: volume boundary inclusive, tx boundary exclusive
    assert fees.calculate_fees(1, 1, 2_999_999, 0) == (0, "pro_under_cap")
    assert fees.calculate_fees(1, 1, 2_999_999, 499) == (0, "pro_under_cap")
    # Pro overage: volume or txs -> free pricing
    assert fees.calculate_fees(10_000, 1, 2_999_999, 0) == (50, "pro_overage")
    assert fees.calculate_fees(10_000, 1, 0, 500) == (50, "pro_overage")
    assert fees.calculate_fees(10_000, 1, 0, 501) == (50, "pro_overage")
    # L33t: 0 fee; fair-use guard trips above 127,669 txs/mo (econ-derived;
    # see PRICING.md). Boundary is exclusive: txs_used == cap is still l33t.
    assert fees.calculate_fees(10_000, 2, 0, 0) == (0, "l33t")
    assert fees.calculate_fees(10_000, 2, 0, 127_668) == (0, "l33t")
    assert fees.calculate_fees(10_000, 2, 0, 127_669) == (0, "l33t")
    assert fees.calculate_fees(10_000, 2, 0, 127_670) == (0, "l33t_fair_use_review")
    assert fees.calculate_fees(10_000, 2, 99_999_999, 99_999_999) == (0, "l33t_fair_use_review")
    # Negative amount refused
    assert fees.calculate_fees(-1, 0, 0, 0) == (0, "refused_negative_amount")
    assert fees.calculate_fees(-1, 2, 0, 0) == (0, "refused_negative_amount")
    # Unknown tier raises
    with pytest.raises(ValueError):
        fees.calculate_fees(100, 7, 0, 0)


def test_fee_integer_arithmetic():
    # No floats anywhere: odd cent amounts floor correctly (integer division)
    for amt in (1, 3, 33, 333, 999_999):
        fee, _ = fees.calculate_fees(amt, 0, 0, 0)
        assert fee == amt // 200
        assert isinstance(fee, int)


# ---------------------------------------------------------------- oracle
def test_mock_oracle():
    o = MockOracle({("ethereum", "ETH"): 4000.0, ("base", "USDC"): 1.0})
    assert o.get_price_usd("ethereum", "eth") == 4000.0
    assert o.has_value("BASE", "usdc")
    assert o.get_price_usd("ethereum", "DOGE") is None
    assert not o.has_value("ethereum", "DOGE")


def test_coingecko_never_raises():
    o = oracle.CoinGeckoOracle(timeout=0.001)  # guaranteed timeout
    assert o.get_price_usd("ethereum", "ETH") is None
    assert o.get_price_usd("nope", "NOPE") is None


# ---------------------------------------------------------------- router
FULL = oracle.default_mock_oracle()


def test_router_trivial_path():
    r = ConverterRouter()
    p = r.find_path("ethereum", "USDC", "ethereum", "USDC", FULL)
    assert p == [{"chain": "ethereum", "token": "USDC", "hop": "origin"}]


def test_router_bridge_and_swap():
    r = ConverterRouter()
    # ETH(ethereum) -> USDC(ethereum) swap -> USDC(base) bridge
    p = r.find_path("ethereum", "ETH", "base", "USDC", FULL)
    assert p is not None
    assert p[0]["hop"] == "origin" and p[-1] == {"chain": "base", "token": "USDC", "hop": "bridge"}
    assert [s["hop"] for s in p] == ["origin", "swap", "bridge"]
    # SOL(solana) -> USDC(solana) -> USDC(ethereum) -> ETH(ethereum)
    p2 = r.find_path("solana", "SOL", "ethereum", "ETH", FULL)
    assert p2 is not None
    assert [s["token"] for s in p2] == ["SOL", "USDC", "USDC", "ETH"]


def test_router_unpriced_token_refuses():
    o = MockOracle({("ethereum", "ETH"): 4000.0})  # USDC unpriced
    r = ConverterRouter()
    assert r.find_path("ethereum", "ETH", "base", "USDC", o) is None
    assert r.find_path("ethereum", "USDC", "ethereum", "USDC", o) is None  # trivial needs price


def test_router_unknown_token():
    r = ConverterRouter()
    assert r.find_path("ethereum", "DOGE", "base", "USDC", FULL) is None
    assert r.find_path("ethereum", "ETH", "nochain", "USDC", FULL) is None


# ---------------------------------------------------------------- chamber
def test_chamber_sign_verify():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    env = chamber.sign_attestation({"a": 1, "b": [2, 3]}, sk)
    assert env["alg"] == "ed25519"
    assert chamber.verify_attestation(env, sk.verify_key)
    # canonical form is sorted + compact
    assert env["payload"] == '{"a":1,"b":[2,3]}'


def test_chamber_tamper_fails():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    env = chamber.sign_attestation({"x": 9}, sk)
    tampered = dict(env, payload='{"x":10}')
    assert not chamber.verify_attestation(tampered, sk.verify_key)
    assert not chamber.verify_attestation(dict(env, sig="00" * 64), sk.verify_key)
    assert not chamber.verify_attestation({"junk": 1}, sk.verify_key)
    other = SigningKey.generate()
    assert not chamber.verify_attestation(env, other.verify_key)  # wrong key


# ---------------------------------------------------------------- settlement
def test_settlement_mock_receipt():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    out = settlement.execute_quote(
        {"from_chain": "ethereum", "from_token": "ETH",
         "to_chain": "base", "to_token": "USDC",
         "amount_cents": 10_000, "tier": 1,
         "path": [{"chain": "ethereum", "token": "ETH", "hop": "origin"}]},
        {"volume_used_cents": 0, "txs_used": 0}, signing_key=sk)
    env = out["attestation"]
    assert chamber.verify_attestation(env, sk.verify_key)
    receipt = json.loads(env["payload"])
    assert receipt["mode"] == "mock"
    assert receipt["fee_cents"] == 0 and receipt["net_cents"] == 10_000
    assert receipt["status"] == "pro_under_cap"
    assert receipt["receipt_id"]


def test_settlement_dust_refused():
    from nacl.signing import SigningKey
    sk = SigningKey.generate()
    out = settlement.execute_quote(
        {"from_chain": "ethereum", "from_token": "USDC",
         "to_chain": "ethereum", "to_token": "USDC",
         "amount_cents": 0, "tier": 0, "path": []},
        {"volume_used_cents": 0, "txs_used": 0}, signing_key=sk)
    receipt = json.loads(out["attestation"]["payload"])
    assert receipt["refused"] is True
    assert receipt["status"] == "refused_dust_eaten_by_fees"


def test_settlement_mainnet_guard():
    from nacl.signing import SigningKey
    os.environ["AWL_MAINNET_ENABLED"] = "1"
    try:
        with pytest.raises(RuntimeError, match="mainnet"):
            settlement.execute_quote(
                {"from_chain": "a", "from_token": "b", "to_chain": "c",
                 "to_token": "d", "amount_cents": 100, "tier": 0, "path": []},
                {"volume_used_cents": 0, "txs_used": 0},
                signing_key=SigningKey.generate())
    finally:
        os.environ["AWL_MAINNET_ENABLED"] = "0"


# ---------------------------------------------------------------- HTTP end-to-end
def _post(port, path, body, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode()), dict(e.headers)


@pytest.fixture(scope="module")
def server():
    # Local-dev bypass ONLY behind the explicit flag (AWL_TEST_MODE is
    # retired and inert — see test_x402.py).
    os.environ["AWL_LOCAL_DEV"] = "1"
    os.environ["AWL_PORT"] = "8899"
    # Dummy URL: the rail counts as configured (so the 402 lists Base)
    # but no real chain is ever touched — this module only exercises the
    # local-dev bypass. Real verification is covered in test_x402.py.
    os.environ["AWL_RPC_BASE"] = "http://127.0.0.1:9/"
    import uvicorn
    from server import app as appmod
    config = uvicorn.Config(appmod.app, host="127.0.0.1", port=8899,
                            log_level="critical", access_log=False,
                            lifespan="off")
    srv = uvicorn.Server(config)
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    import time
    for _ in range(100):
        try:
            with urllib.request.urlopen("http://127.0.0.1:8899/health", timeout=2) as r:
                assert r.status == 200
                break
        except Exception:
            time.sleep(0.1)
    yield 8899
    srv.should_exit = True
    t.join(timeout=15)


def test_http_health(server):
    with urllib.request.urlopen("http://127.0.0.1:%d/health" % server) as r:
        assert json.load(r) == {"ok": True, "mode": "mock-local"}


def test_http_quote_ok(server):
    code, body, _ = _post(server, "/api/pay/quote", {
        "from_chain": "ethereum", "from_token": "ETH",
        "to_chain": "base", "to_token": "USDC",
        "amount_cents": 10_000, "tier": 1,
        "volume_used_cents": 0, "txs_used": 0})
    assert code == 200
    assert not body.get("refused")
    assert body["fees"]["free"]["fee_cents"] == 50
    assert body["fees"]["pro"]["status"] == "pro_under_cap"
    assert body["fees"]["l33t"]["fee_cents"] == 0
    assert body["net_cents"] == 10_000
    assert body["path"]


def test_http_quote_refusals(server):
    # unknown token
    code, body, _ = _post(server, "/api/pay/quote", {
        "from_chain": "ethereum", "from_token": "DOGE",
        "to_chain": "base", "to_token": "USDC", "amount_cents": 100})
    assert code == 200 and body == {"refused": True, "reason": "unknown_token_no_price",
                                    "detail": "DOGE on ethereum has no verifiable price"}
    # dust
    code, body, _ = _post(server, "/api/pay/quote", {
        "from_chain": "ethereum", "from_token": "USDC",
        "to_chain": "ethereum", "to_token": "USDC", "amount_cents": 0, "tier": 0})
    assert code == 200 and body["refused"] is True and body["reason"] == "dust_eaten_by_fees"
    # bad request shape still 200-refusal
    code, body, _ = _post(server, "/api/pay/quote", {"amount_cents": "lots"})
    assert code == 200 and body["reason"] == "bad_request"


def test_http_execute_402_then_local_dev_payment(server):
    body = {"from_chain": "ethereum", "from_token": "ETH",
            "to_chain": "base", "to_token": "USDC",
            "amount_cents": 10_000, "tier": 1,
            "volume_used_cents": 0, "txs_used": 0,
            "idempotency_key": "test-nonce-1"}
    # unpaid -> 402 + machine-readable x402 terms
    code, b402, h402 = _post(server, "/api/pay/execute", body)
    assert code == 402
    assert b402["x402Version"] == 2
    assert b402["accepts"], "402 must list at least one verifiable rail"
    first = b402["accepts"][0]
    assert first["scheme"] == "exact"
    assert first["network"] == "eip155:8453"
    assert first["amount"] == str(
        fees.ROUTE_PRICE_CENTS * x402.UNITS_PER_CENT), \
        "402 must price the 1c cost-plus route toll"
    assert first["extra"]["howto"], "402 terms must tell the payer how to pay"
    assert {k.lower(): v for k, v in h402.items()}.get("payment-required") == "1"
    # paid (local-dev bypass) -> signed receipt
    code, b200, _ = _post(server, "/api/pay/execute", body,
                          {"X-Test-Payment": "ok"})
    assert code == 200 and b200["ok"] is True
    assert b200["charged_cents"] == 1
    assert b200["route_price_cents"] == 1
    env = b200["attestation"]
    assert env["alg"] == "ed25519"
    receipt = json.loads(env["payload"])
    assert receipt["mode"] == "mock" and receipt["net_cents"] == 10_000
    assert b200["payment"] == {"via": "local-dev", "verified": False}
    # replay -> 409
    code, b409, _ = _post(server, "/api/pay/execute", body,
                          {"X-Test-Payment": "ok"})
    assert code == 409


# ---------------------------------------------------------------- hardening
def test_http_healthz(server):
    """Fly http_service check target: 200 + version."""
    with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % server) as r:
        body = json.load(r)
    assert r.status == 200
    assert body["ok"] is True
    assert body["version"] == "1.0"
    assert body["mode"] == "mock-local"


def _get(port, path, headers=None):
    req = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def test_http_quote_trailing_slash_parity(server):
    """POST /api/pay/quote/ behaves exactly like /api/pay/quote
    (stdlib server stripped trailing slashes)."""
    body = {"from_chain": "ethereum", "from_token": "ETH",
            "to_chain": "base", "to_token": "USDC",
            "amount_cents": 10_000, "tier": 1}
    code1, b1, _ = _post(server, "/api/pay/quote", body)
    code2, b2, _ = _post(server, "/api/pay/quote/", body)
    assert code1 == code2 == 200
    assert b1 == b2 and not b1.get("refused")


def test_http_method_mismatch_is_json_404(server):
    """Method+path parity with the stdlib server: wrong method on a
    known route is a JSON 404, not a 405."""
    code, raw, _ = _get(server, "/api/pay/quote")
    assert code == 404 and json.loads(raw) == {"error": "not found"}
    code, raw, _ = _get(server, "/nope")
    assert code == 404 and json.loads(raw) == {"error": "not found"}
    # POST on a GET-only route likewise 404s (old do_POST fell through)
    code, b, _ = _post(server, "/health", {})
    assert code == 404 and b == {"error": "not found"}


def test_http_body_too_large_413(server):
    big = {"from_chain": "ethereum", "from_token": "ETH",
           "to_chain": "base", "to_token": "USDC",
           "amount_cents": 100, "pad": "x" * (70 * 1024)}
    code, b, _ = _post(server, "/api/pay/quote", big)
    assert code == 413 and b == {"error": "body too large"}


def test_http_execute_refused_quote_parity(server):
    """Paid execute of a dust quote returns the 200 refusal shape —
    same law as /quote, through the 402 gate."""
    body = {"from_chain": "ethereum", "from_token": "USDC",
            "to_chain": "ethereum", "to_token": "USDC",
            "amount_cents": 0, "tier": 0,
            "idempotency_key": "test-nonce-dust-1"}
    code, b, _ = _post(server, "/api/pay/execute", body,
                       {"X-Test-Payment": "ok"})
    assert code == 200
    assert b["refused"] is True and b["reason"] == "dust_eaten_by_fees"


def test_http_request_id_echo(server):
    """Every response carries X-Request-Id; a caller-supplied id is
    passed through untouched."""
    _, _, h1 = _get(server, "/health")
    rid1 = {k.lower(): v for k, v in h1.items()}.get("x-request-id")
    assert rid1, "X-Request-Id missing on response"
    _, _, h2 = _get(server, "/health", {"X-Request-Id": "probe-123"})
    rid2 = {k.lower(): v for k, v in h2.items()}.get("x-request-id")
    assert rid2 == "probe-123"


def test_log_event_json_shape(capsys):
    """log_event emits exactly one JSON line with the required fields."""
    from server import logging as logmod
    logmod.log_event("request", request_id="r1", method="POST",
                     path="/api/pay/quote", status=200, latency_ms=1.5,
                     tier="pro", price_cents=0, mode="mock-local")
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    rec = json.loads(out[0])
    assert rec["event"] == "request" and rec["request_id"] == "r1"
    assert rec["tier"] == "pro" and rec["price_cents"] == 0
    assert rec["latency_ms"] == 1.5 and "ts" in rec
