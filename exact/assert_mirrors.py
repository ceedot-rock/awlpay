#!/usr/bin/env python3
"""Assert MCP fee mirror against exact/FeeManager.cuni gold. Not CI-gated."""
# mirror of exact/FeeManager.cuni — SoT wins
import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "mcp" / "server.py"

# Stub mcp.server.fastmcp so we can import without the package installed
mcp_mod = types.ModuleType("mcp")
mcp_server = types.ModuleType("mcp.server")
fastmcp = types.ModuleType("mcp.server.fastmcp")

class FastMCP:
    def __init__(self, name): self.name = name
    def tool(self):
        def deco(fn): return fn
        return deco
    def run(self): pass

fastmcp.FastMCP = FastMCP
sys.modules["mcp"] = mcp_mod
sys.modules["mcp.server"] = mcp_server
sys.modules["mcp.server.fastmcp"] = fastmcp
sys.modules.setdefault("httpx", types.ModuleType("httpx"))

spec = importlib.util.spec_from_file_location("awlpay_mcp_server", SERVER)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
calculate_fees = mod.calculate_fees

GOLD = [
    ("free", 10000, 0, 0, 125, "free"),
    ("pro", 10000, 100000, 10, 0, "pro"),
    ("pro", 10000, 3000000, 10, 125, "pro_overage"),
    ("pro", 10000, 100000, 500, 125, "pro_overage"),
    ("l33t", 99999999, 0, 0, 0, "l33t"),
]

failed = 0
for tier, amt, vol, txs, fee, applied in GOLD:
    got = calculate_fees(amt, tier, vol, txs)
    ok = got.get("platform_fee_cents") == fee and got.get("tier_applied") == applied
    print(f"{'PASS' if ok else 'FAIL'} {tier} amt={amt} vol={vol} txs={txs} → {got}")
    if not ok:
        failed += 1

refuse = calculate_fees(10000, "enterprise")
ok_refuse = refuse.get("refuse") == "unknown_tier"
print(f"{'PASS' if ok_refuse else 'FAIL'} unknown tier → {refuse}")
if not ok_refuse:
    failed += 1

src = SERVER.read_text()
ok_comment = "mirror of exact/FeeManager.cuni — SoT wins" in src
print(f"{'PASS' if ok_comment else 'FAIL'} mcp SoT comment")
if not ok_comment:
    failed += 1

sys.exit(1 if failed else 0)
