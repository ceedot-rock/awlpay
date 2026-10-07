# AwLPay Wallets - API / npm / MCP

Built from single CuNi FeeManager spec.

- `openapi.yaml` - REST API
- `sdk/` - npm `@awlpay/sdk` (TypeScript)
- `mcp/server.py` - MCP server with tools: awlpay_create_wallet, awlpay_get_balance, awlpay_get_quote, awlpay_settle

Fee logic mirrors CuNi exactly: free 0.5% flat, pro $39/mo under $30k/500txs = $0, l33t $799/mo unlimited.
