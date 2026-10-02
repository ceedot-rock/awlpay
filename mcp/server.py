# mirror of exact/FeeManager.cuni — SoT wins
from mcp.server.fastmcp import FastMCP
import os, httpx

mcp = FastMCP("awlpay-wallets")
BASE = os.getenv("AWLPAY_BASE_URL", "https://api.awlpay.com")
KEY = os.getenv("AWLPAY_API_KEY", "")

PRO_CAP_VOLUME = 3_000_000
PRO_CAP_TXS = 500


def _headers():
    return {"Authorization": f"Bearer {KEY}"}


def _free_formula(amount_cents: int) -> int:
    # integer cents: amount_cents * 10 // 1000 + 25
    return amount_cents * 10 // 1000 + 25


def calculate_fees(
    amount_cents: int,
    tier: str,
    volume_month_usd_cents: int = 0,
    txs_month: int = 0,
) -> dict:
    """Local fee mirror of exact/FeeManager.cuni. Unknown tier → refuse."""
    if tier == "l33t":
        return {"platform_fee_cents": 0, "trading_fee_cents": 0, "tier_applied": "l33t"}
    if tier == "pro":
        if volume_month_usd_cents < PRO_CAP_VOLUME and txs_month < PRO_CAP_TXS:
            return {"platform_fee_cents": 0, "trading_fee_cents": 0, "tier_applied": "pro"}
        return {
            "platform_fee_cents": _free_formula(amount_cents),
            "trading_fee_cents": 0,
            "tier_applied": "pro_overage",
        }
    if tier == "free":
        return {
            "platform_fee_cents": _free_formula(amount_cents),
            "trading_fee_cents": 0,
            "tier_applied": "free",
        }
    # refuse=unknown_tier — matches FeeManager.cuni (no soft PASS)
    return {"refuse": "unknown_tier", "error": "refuse=unknown_tier"}


@mcp.tool()
def awlpay_create_wallet(owner_id: str, chain: str = "solana") -> dict:
    """Create an AwLPay wallet"""
    r = httpx.post(
        f"{BASE}/v1/wallets",
        json={"owner_id": owner_id, "chain": chain},
        headers=_headers(),
        timeout=20,
    )
    return r.json()


@mcp.tool()
def awlpay_get_balance(wallet_id: str) -> dict:
    """Get wallet balance"""
    r = httpx.get(f"{BASE}/v1/wallets/{wallet_id}/balance", headers=_headers(), timeout=20)
    return r.json()


@mcp.tool()
def awlpay_get_quote(
    amount_cents: int,
    tier: str = "free",
    volume_month_usd_cents: int = 0,
    txs_month: int = 0,
) -> dict:
    """Get fee quote — mirror of exact/FeeManager.cuni (SoT wins). Local, no network."""
    return calculate_fees(amount_cents, tier, volume_month_usd_cents, txs_month)


@mcp.tool()
def awlpay_settle(
    from_wallet: str, to_wallet: str, amount_cents: int, tier: str = "free"
) -> dict:
    """Settle payment via AwLPay"""
    r = httpx.post(
        f"{BASE}/v1/settle",
        json={
            "from_wallet": from_wallet,
            "to_wallet": to_wallet,
            "amount_cents": amount_cents,
            "tier": tier,
        },
        headers=_headers(),
        timeout=30,
    )
    return r.json()


if __name__ == "__main__":
    mcp.run()
