# AwLPay exact/ — FeeManager (CuNi)

CuNi-exact FeeManager for AwLPay. **Same stdout or refuse.** Home of the locked fee law lives here under `exact/` (not the cuni compiler repo).

## Spec

See [`docs/fee-manager-spec.md`](../docs/fee-manager-spec.md).

| Tier | Seat price | Platform fee |
|------|------------|--------------|
| free | — | 1.0% + $0.25 (`amount_cents * 10 / 1000 + 25`) |
| pro | $39/mo | **0** only if `volume_month_usd_cents < 3_000_000` **AND** `txs_month < 500`; else free formula as `pro_overage` |
| l33t | $799/mo | 0 |

Unknown tier prints `refuse=unknown_tier` and returns `-1`.

## Layout

| Path | What |
|------|------|
| `FeeManager.cuni` | SoT + combined Bank gold (all cases) |
| `fixtures/*.cuni` | One case each for Bank paste |
| `bank_receipts/` | Captured `bank: PASS` lines |
| `run_bank.sh` | Local Bank gate (py/go/js/sol) |

## Run

```sh
# needs `cuni` on PATH (or set CUNI=...)
./exact/run_bank.sh
```

Studio / check seats historically py/go/js; `sol` is included when the local catalog proves it.

## Out of scope (this PR)

- npm publish / Fly secrets
- Toll 5 / x402
- Merging without Cos GREEN
