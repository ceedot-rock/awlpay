# awlpay — JavaScript/TypeScript SDK

Agent wallets for AwLPay's multi-rail x402 stablecoin payments (Base + Solana).
Five minutes, zero crypto background. See the full developer walkthrough at
[../QUICKSTART.md](../QUICKSTART.md).

```ts
import { AgentWallet } from "awlpay";

const w = await AgentWallet.create({ password: "..." }); // testnet default
console.log(await w.depositAddress("base"));   // "0x..." — fund it, then:
console.log(await w.depositAddress("solana")); // base58
console.log(await w.balances());               // { base: 0, solana: 0 } in USD

const receipt = await w.pay("0x...", 1.5, { rail: "auto", maxUsd: 5 });
// receipt: { rail, txHash, usd, to, confirmedAt, network }

console.log(await w.lifetimeSpentUsd());
```

## Safety rules (enforced, not suggested)

- **Testnet is the default.** Mainnet requires `network: "mainnet"` **and**
  `confirmMainnet: "I UNDERSTAND"` (exact string), otherwise `MainnetConfirmationError`.
- **Spend caps.** `maxSpendUsd` (default 100) is a *lifetime* cap persisted in the
  encrypted wallet file. `pay()` also accepts a per-call `maxUsd`. Exceeding either
  throws `SpendCapExceededError`. The lifetime counter moves **only after a
  confirmed payment**.
- **Keys.** First run generates one secp256k1 key (Base) and one ed25519 key
  (Solana), sealed with AES-256-GCM (node:crypto), key derived from your password
  with scrypt (N=16384) + random salt. Stored at `~/.awlpay/wallets.enc`
  (overridable via `AWL_PAY_DIR`). The password is never written to disk, and
  private keys are never logged.
- **Rail selection.** `rail: "auto"` picks the cheapest *funded* rail
  (balance ≥ payment): Solana first (~$0.0003 est. fee), then Base (~$0.02).
  These are rough estimates used only for selection, not fee quotes. If no rail
  is funded, `pay()` throws `InsufficientFundsError` with per-rail balances attached.

## Adding a rail

Rails are pluggable: add a config to `RAILS` in `src/rails.ts` and a matching
adapter factory to `ADAPTER_FACTORIES` in `src/wallet.ts`. Nothing else changes.

## Errors

`SpendCapExceededError`, `InsufficientFundsError`, `MainnetConfirmationError`,
`UnsupportedRailError` — all extend `AwlpayError` (check `.code`).

## Develop

```bash
npm install
npm test      # vitest — all mocked, no network, no real funds
npm run build # tsc → dist/
```
