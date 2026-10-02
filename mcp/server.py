# mirror of exact/FeeManager.cuni — SoT wins
from mcp.server.fastmcp import FastMCP
import os, httpx

mcp = FastMCP("awlpay")
BASE = os.getenv("AWLPAY_BASE_URL", "https://awlpay.fly.dev")

PRO_CAP_VOLUME = 3_000_000
PRO_CAP_TXS = 500

TIER_IDS = {"free": 0, "pro": 1, "l33t": 2}


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


def _quote_body(from_chain, from_token, to_chain, to_token, amount_cents,
                tier="free", volume_used_cents=0, txs_used=0,
                idempotency_key=None, to_address=None) -> dict:
    body = {
        "from_chain": from_chain,
        "from_token": from_token,
        "to_chain": to_chain,
        "to_token": to_token,
        "amount_cents": amount_cents,
        "tier": TIER_IDS.get(tier, 0),
        "volume_used_cents": volume_used_cents,
        "txs_used": txs_used,
    }
    if idempotency_key:
        body["idempotency_key"] = idempotency_key
    if to_address:
        body["to_address"] = to_address
    return body


@mcp.tool()
def awlpay_health() -> dict:
    """AwLPay liveness check."""
    r = httpx.get(f"{BASE}/health", timeout=10)
    return r.json()


@mcp.tool()
def awlpay_fee_preview(
    amount_cents: int,
    tier: str = "free",
    volume_month_usd_cents: int = 0,
    txs_month: int = 0,
) -> dict:
    """Fee preview — mirror of exact/FeeManager.cuni (SoT wins). Local, no network."""
    return calculate_fees(amount_cents, tier, volume_month_usd_cents, txs_month)


@mcp.tool()
def awlpay_quote(
    from_chain: str,
    from_token: str,
    to_chain: str,
    to_token: str,
    amount_cents: int,
    tier: str = "free",
    volume_used_cents: int = 0,
    txs_used: int = 0,
    to_address: str | None = None,
) -> dict:
    """Priced conversion quote (FREE). Returns the path + all-tier fees,
    or {refused: true, reason} when the law says no."""
    r = httpx.post(
        f"{BASE}/api/pay/quote",
        json=_quote_body(from_chain, from_token, to_chain, to_token,
                         amount_cents, tier, volume_used_cents, txs_used,
                         to_address=to_address),
        timeout=30,
    )
    return r.json()


@mcp.tool()
def awlpay_execute(
    from_chain: str,
    from_token: str,
    to_chain: str,
    to_token: str,
    amount_cents: int,
    x_payment: str,
    tier: str = "free",
    volume_used_cents: int = 0,
    txs_used: int = 0,
    idempotency_key: str | None = None,
    to_address: str | None = None,
) -> dict:
    """Execute a quoted conversion (402-GATED). x_payment is the x402
    X-PAYMENT header proving the route-price payment; without it the
    server answers 402 with PaymentRequirements."""
    r = httpx.post(
        f"{BASE}/api/pay/execute",
        json=_quote_body(from_chain, from_token, to_chain, to_token,
                         amount_cents, tier, volume_used_cents, txs_used,
                         idempotency_key, to_address),
        headers={"X-PAYMENT": x_payment},
        timeout=60,
    )
    return r.json()


if __name__ == "__main__":
    mcp.run()
