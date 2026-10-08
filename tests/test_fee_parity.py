"""Fee parity: every fee mirror must replicate server/fees.py exactly.

Locked fee (Corey 2026-10-06): 0.5% flat, no fixed fee — free tier is
`amount_cents // 200`. Pro is under cap iff
`(volume_used + amount) <= 3_000_000 and txs_used < 500`; overage falls
back to the free formula.

Covers the ledger MAJORs: MCP `_free_formula` drift and the MCP pro-cap
boundary divergence (strict `<` that ignored the current amount).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

from server import fees as server_fees  # noqa: E402


def _load_mcp_server():
    """Import mcp/server.py with a stubbed FastMCP — the real `mcp`
    package is a deploy-time dependency, not a test dependency. The stub
    only needs the @mcp.tool() decorator to be an identity."""
    stub_fastmcp = types.ModuleType("mcp.server.fastmcp")

    class _FastMCP:
        def __init__(self, name):
            self.name = name

        def tool(self):
            def deco(fn):
                return fn
            return deco

    stub_fastmcp.FastMCP = _FastMCP
    sys.modules["mcp"] = types.ModuleType("mcp")
    sys.modules["mcp.server"] = types.ModuleType("mcp.server")
    sys.modules["mcp.server.fastmcp"] = stub_fastmcp
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "mcp", "server.py")
    spec = importlib.util.spec_from_file_location("awlpay_mcp_server", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


MCP = _load_mcp_server()

_TIERS = [("free", 0), ("pro", 1), ("l33t", 2)]
_AMOUNTS = [0, 1, 99, 100, 199, 200, 10_000, 10_001, 999_999, 3_000_000]
_VOLUMES = [0, 2_990_000, 2_995_000, 2_999_999, 3_000_000]
_TXS = [0, 1, 499, 500, 127_669, 127_670]


def test_mcp_mirrors_server_fees_on_grid():
    """mcp.calculate_fees fee output == server calculate_fees fee output
    over a grid that straddles every cap boundary."""
    mismatches = []
    for mcp_tier, server_tier in _TIERS:
        for amount in _AMOUNTS:
            for vol in _VOLUMES:
                for txs in _TXS:
                    got = MCP.calculate_fees(amount, mcp_tier, vol, txs)
                    fee, status = server_fees.calculate_fees(
                        amount, server_tier, vol, txs)
                    if got["platform_fee_cents"] != fee:
                        mismatches.append(
                            (mcp_tier, amount, vol, txs,
                             got["platform_fee_cents"], fee, status))
    assert not mismatches, "mcp/server fee divergence: %r" % (mismatches[:8],)


def test_pro_cap_boundary_parity():
    """volume_used=2_995_000 + amount=10_000 = 3_005_000 > cap: server
    charges overage 50c — mcp must quote the same, not 0c."""
    got = MCP.calculate_fees(10_000, "pro", 2_995_000, 0)
    fee, status = server_fees.calculate_fees(10_000, 1, 2_995_000, 0)
    assert (fee, status) == (50, "pro_overage")
    assert got["platform_fee_cents"] == 50
    assert got["tier_applied"] == "pro_overage"
    # Exactly AT the cap: under (<=), fee 0 on both sides.
    got = MCP.calculate_fees(10_000, "pro", 2_990_000, 0)
    fee, status = server_fees.calculate_fees(10_000, 1, 2_990_000, 0)
    assert (fee, status) == (0, "pro_under_cap")
    assert got["platform_fee_cents"] == 0
    assert got["tier_applied"] == "pro"


def test_mcp_free_formula_is_half_percent_flat():
    assert MCP._free_formula(10_000) == 50
    assert MCP._free_formula(199) == 0  # integer cents, no fixed fee


def test_quote_100_dollars_free_tier_is_50_cents():
    """End-to-end: /api/pay/quote on $100 free tier asserts the locked
    50c fee (README fixtures/examples must match this)."""
    from starlette.testclient import TestClient  # noqa: E402

    from server import app as appmod  # noqa: E402

    client = TestClient(appmod.app)
    r = client.post("/api/pay/quote", json={
        "from_chain": "ethereum", "from_token": "ETH",
        "to_chain": "base", "to_token": "USDC",
        "amount_cents": 10_000, "tier": 0,
        "volume_used_cents": 0, "txs_used": 0})
    assert r.status_code == 200
    body = r.json()
    assert not body.get("refused"), body
    assert body["fees"]["free"]["fee_cents"] == 50
    assert body["fees"]["free"]["status"] == "free"
    assert body["fees"]["free"]["net_cents"] == 9950
