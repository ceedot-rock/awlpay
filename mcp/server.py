from mcp.server.fastmcp import FastMCP
import os, httpx

mcp = FastMCP("awlpay-wallets")
BASE = os.getenv("AWLPAY_BASE_URL", "https://api.awlpay.com")
KEY = os.getenv("AWLPAY_API_KEY", "")

def _headers(): return {"Authorization": f"Bearer {KEY}"}

@mcp.tool()
def awlpay_create_wallet(owner_id: str, chain: str = "solana") -> dict:
    """Create an AwLPay wallet"""
    r = httpx.post(f"{BASE}/v1/wallets", json={"owner_id": owner_id, "chain": chain}, headers=_headers(), timeout=20)
    return r.json()

@mcp.tool()
def awlpay_get_balance(wallet_id: str) -> dict:
    """Get wallet balance"""
    r = httpx.get(f"{BASE}/v1/wallets/{wallet_id}/balance", headers=_headers(), timeout=20)
    return r.json()

@mcp.tool()
def awlpay_get_quote(amount_cents: int, tier: str = "free") -> dict:
    """Get fee quote - mirrors CuNi calculateFees"""
    # local mirror for speed, no network
    if tier == "l33t":
        platform_fee = 0; tier_applied="l33t"
    elif tier == "pro":
        platform_fee = 0; tier_applied="pro"  # assumes under cap, API validates
    else:
        platform_fee = amount_cents * 10 // 1000 + 25; tier_applied="free"
    return {"platform_fee_cents": platform_fee, "trading_fee_cents": 0, "tier_applied": tier_applied}

@mcp.tool()
def awlpay_settle(from_wallet: str, to_wallet: str, amount_cents: int, tier: str = "free") -> dict:
    """Settle payment via AwLPay"""
    r = httpx.post(f"{BASE}/v1/settle", json={"from_wallet": from_wallet, "to_wallet": to_wallet, "amount_cents": amount_cents, "tier": tier}, headers=_headers(), timeout=30)
    return r.json()

if __name__ == "__main__":
    mcp.run()
