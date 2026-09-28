# awLPay v1 — BUILD REPORT (2026-09-27)

Built by coordinator + 3 workstreams. Local only. No commits, no deploys, no spending, no mainnet.

## Verified numbers
- **pytest: 40 passed, 0 failed** (`cd ~/workspace/awlpay && python3 -m pytest tests/ -q`)
  - 17 service tests (fees, oracle law, router paths/refusals, chamber tamper cases, settlement dust/mainnet guard, HTTP e2e incl. 402 → paid → 409 replay)
  - 18 parametrized fixture replays: every `spec/fixtures/fees.json` case through `server/fees.py` → **zero mismatches** vs the CuNi spec math
  - 1 fixture-count guard, 3 `cuni check` subprocess gates, 1 receipt sign/verify/tamper round-trip
- **CuNi specs: `exactness: PASS (5 langs)`** on all three (`--only py,js,ts,c,cpp`):
  - `spec/FeeManager.cuni`, `spec/SettlementEngine.cuni`, `spec/Profile.cuni`
- **Live smoke test**: quote SOL→ETH returned a 4-hop path (swap→bridge→swap) with all three tiers' fees; free tier on $1000.00 → fee 1025¢ ($10.00 + $0.25) ✓; unpaid execute → 402; test-paid execute → signed Ed25519 receipt; idempotency replay → 409.

## What works
- `POST /api/pay/quote` (free): hasValue() gate on both tokens → BFS conversion path across 5 chains (ethereum, base, polygon, arbitrum, solana; USDC/ETH/SOL) → fees quoted for all three tiers → or honest 200 `{refused: true, reason}` (`unknown_token_no_price`, `no_conversion_path`, `dust_eaten_by_fees`).
- `POST /api/pay/execute` (402-gated, 25¢ route price): tier caps enforced (pro: volume+amount ≤ 3,000,000¢ AND txs < 500 → 0 fee; overage → free pricing), dust refused (net ≤ 0), receipt signed with real Ed25519.
- FeeManager CuNi spec + Python mirror agree exactly (integer cents, floor division on non-negatives).
- Price oracle: pluggable ABC; CoinGecko live-verified working (ETH $2,697.84 at build time); MockOracle for tests.

## What's stubbed (loud comments in code + README HONEST LIMITS)
- **Settlement is MOCK** — coins never move; the receipt attests to the law that *would* execute. `AWL_MAINNET_ENABLED=1` → loud refusal.
- **/execute's 402 gate is test-mode only** — `AWL_TEST_MODE=1` + `X-Test-Payment` header; no real payment rail wired (rider-x402's on-chain USDC verification is the template to port).
- **Chamber key custody** — env var `AWL_RELAYER_KEY` or ephemeral test key; Chamber HSM integration is a marked TODO. Attestation *envelope* and Ed25519 signatures are real.
- **CoinGecko oracle is a centralized trust assumption** — interface exists for a trust-minimized swap.
- **Server is stdlib `http.server`** — needs a real ASGI server for production.
- **CuNi gate is 5 seats here** — go/rs/rb/lua runtimes not installed on this VM; Solidity refuses `typ` records by design (correct CuNi behavior, affects stock examples too).
- Python raises `ValueError` on unknown tier; CuNi returns status `"refused_unknown_tier"`. Tiers 0–2 are the full contract; divergence is documented, not hidden.

## Needs Corey's decision
1. **Oracle choice**: ship with CoinGecko (free, centralized) or invest in a trust-minimized price feed before any real use?
2. **Mainnet enablement**: stays hard-disabled until he explicitly approves; real settlement + real 402 payment verification (port from rider-x402) are the two big builds before that.
3. **Paid-route pricing**: /execute route price is 25¢ flat in v1 — does he want per-call pricing tied to actual compute/cost-plus?
4. **Chamber custody**: where do relayer keys actually live in production?
5. **l33t fair-use threshold**: 100,000 txs/mo is a placeholder — needs his number.
6. **Anything leaving the lab**: nothing under his name has left the repo. Deployment (Fly or otherwise) was explicitly out of scope and not done.

---

# Workstream 1 — REAL SETTLEMENT (2026-09-28, branch `ws-settle`)

Replaced the mocked SettlementEngine with real chain execution behind
`AWL_EXECUTION_MODE`. Committed locally on `ws-settle`; never pushed.

## Verified numbers
- **pytest: 61 passed, 1 skipped, 0 failed**
  (`cd ~/workspace/awlpay-wt-settle && python3 -m pytest tests/ -q`)
  - 40 baseline tests still green (17 service + 18 fixtures + 1 count guard
    + 3 `cuni check` gates + 1 receipt round-trip). One test-infra fix:
    `tests/test_awlpay.py` hardcoded `sys.path` to `~/workspace/awlpay`
    (the main worktree), so it was testing the wrong checkout's code —
    now resolves the repo root from the test file's own path, the same
    pattern `test_crosscheck.py` already used.
  - 21 new tests in `tests/test_chains.py`; the 1 skip is the live Solana
    devnet `simulateTransaction` test — `api.devnet.solana.com` is
    unreachable from this VM (connection closed), so it skips honestly.
- **CuNi specs untouched** (`exactness: PASS (5 langs)` still gates in
  test_crosscheck.py): the fee law and `SettlementEngine.cuni` are
  unchanged; the executor adds `refused_*`-contract statuses only
  (`refused_unwired_hop`, `refused_no_recipient`, `refused_no_price`,
  `refused_no_path`), never touching the integer-cents floor-division
  agreement.
- **Live dry-run proof (Base Sepolia, real node):** `eth_call` of a
  0-value native transfer AND a 0-unit USDC `transfer()` against
  `0x036CbD53842c5426634e7929541eC2318f3dCF7e` both execute cleanly;
  the ERC-20 call returned boolean `true` (32-byte `0x...01`), proving
  the configured address is a live token contract.
- **Full-stack smoke (manual):** `AWL_EXECUTION_MODE=dryrun` server,
  `/api/pay/execute` base/USDC→base/USDC $100 free tier → signed
  receipt `mode: dryrun`, one transfer hop, 98,750,000 base units
  ($98.75 — fee 125¢ applied exactly), real signed tx
  (`1f71c958…`), `sim_ok: false` with the node's honest verdict
  ("ERC20: transfer amount exceeds balance" — the throwaway sender
  holds 0 USDC), `key_source: throwaway`, `throwaway_recipient: true`.
- **Solana:** system-transfer instruction built + signed fully offline
  (instruction decodes to program `1111…1111`, discriminator `02`,
  5000 lamports LE; signature nacl-verifies against the message);
  `simulateTransaction` request shape + ok/err parsing unit-tested.

## What changed
- `server/chains.py` (new): testnet-only `NETWORKS` table (Sepolia,
  Base Sepolia, Polygon Amoy, Arbitrum Sepolia, Solana devnet;
  Circle-issued USDC addresses web-verified across 4 sources);
  `assert_testnet()` hard-refuses mainnet chain ids (1, 8453, 137,
  42161) and non-devnet Solana clusters with no env override;
  `EvmAdapter` (web3.py: native + ERC-20 USDC build/sign/eth_call/
  broadcast) and `SolanaAdapter` (solders + stdlib JSON-RPC: native
  SOL system transfers, build/sign/simulateTransaction/broadcast);
  `get_settler_seed()` (`AWL_SETTLER_KEY_<chain>` or throwaway);
  `broadcast_allowed()` (testnet AND `AWL_BROADCAST=1`).
- `server/executor.py` (new): consumes `ConverterRouter` path output.
  Transfer hops execute (build→sign→simulate in dryrun;
  build→fill→sign→broadcast in broadcast). Swap/bridge hops have no
  protocol wired → whole settlement refuses `refused_unwired_hop`
  BEFORE executing anything (atomic — verified by test). Dust law:
  net_cents → base units via exact `Fraction` floor math; 0 units →
  `refused_dust_eaten_by_fees`. Broadcast without `AWL_BROADCAST=1`
  RAISES (fail closed); broadcast without an explicit recipient
  refuses `refused_no_recipient` (never sends to a throwaway).
- `server/settlement.py`: `AWL_EXECUTION_MODE` mock (default, byte-
  identical behavior to v1) / dryrun / broadcast; unknown mode raises.
  Mainnet guard still first. Receipts gain `hops`, `to_address`,
  `throwaway_recipient`, `key_source`; still Ed25519-signed.
- `server/app.py`: optional `to_address` quote field (validated);
  configured oracle passed into `execute_quote`.
- `server/requirements.txt`: `pynacl`, `web3`, `solders`.

## Deliberately stubbed (and why)
- **Swap/bridge hops** (DEX swaps, CCTP-style bridges): no on-chain
  liquidity/bridge protocol is wired; executing a fake would be
  dishonest. They refuse atomically with `refused_unwired_hop`. The
  executor's hop registry is the plug-in point (a swap/bridge executor
  registers per hop kind).
- **Solana SPL/USDC transfers**: needs ATA derivation + token-program
  instructions; native SOL transfers are real. `can_transfer("USDC")`
  returns `(False, "solana_spl_not_wired")` loudly.
- **Broadcasts in tests**: no test ever broadcasts (no funds, no
  accidents). The broadcast path is unit-tested via stub adapters plus
  the real gate (`AWL_BROADCAST` + `assert_testnet`).
- **CoinGecko oracle**: stays live per Corey's call; the
  trust-minimized feed interface is untouched for later swap.
- **Chamber HSM custody**: settler keys are env/throwaway (marked TODO
  as before); nothing here authorizes real custody.

## Mainnet-flip config surface (NOT enabled — documented only)
- `AWL_EXECUTION_MODE`: `mock` (default) | `dryrun` | `broadcast`
- `AWL_BROADCAST=1`: opens the broadcast gate (still testnet-only)
- `AWL_SETTLER_KEY_<chain>`: 64-hex settler seed per chain
  (ethereum, base, polygon, arbitrum, solana); unset → throwaway
- `AWL_SETTLER_RECIPIENT`: default broadcast recipient
- `AWL_RPC_<chain>`: override testnet RPC URL per chain
- `AWL_USDC_<chain>` / `AWL_USDC_MINT_solana`: override USDC contract
- `AWL_MAINNET_ENABLED=1`: loud refusal (unchanged)
- A real mainnet flip = replace the `NETWORKS` table (code change) +
  Corey's explicit per-charge approval. Nothing in this build flips it.
