# x402 multi-rail study — CoinPayPortal (2026-10-01)

Source: `~/workspace/coinpayportal-study/` (cloned 2026-10-01, read-only study,
nothing wired in). CoinPayPortal is a non-custodial multi-chain payment platform
whose x402 facilitator advertises BTC, ETH, SOL, POL, BCH, USDC (4 chains),
Lightning/BOLT12, and Stripe cards in a single 402 `accepts` array.

## Adopt

1. **Multi-rail `accepts[]` offers.** One 402 response advertises every rail the
   merchant funded; the client picks. We already build `accepts` in
   `server/x402.py` — extend the rail table toward theirs (native BTC/ETH/SOL/
   POL + USDC variants), keeping our per-rail verification. Use their `extra`
   sub-object pattern for machine-readable rail metadata (facilitator URL,
   methodKey, chainId, assetSymbol).
2. **Gasless EVM auth (EIP-712).** Signed `transferFrom` authorization, no
   on-chain tx until settlement — agents pay without managing gas/nonces.
   Their v1→v2 lesson is the point: bind `resource` into the signed struct
   (their domain-v2 `Payment` struct); the EIP-3009 path can't bind it and
   they document that as residual exposure (one proof re-buying a cheaper
   resource from the same merchant).
3. **Integer-exact amount math.** BigInt comparisons, per-asset smallest-unit
   handling, mandatory `expected {amount, resource}` on every verify call —
   derived from the offer just built, never from the payer's proof. (They
   fixed a float64 collapse bug; don't repeat it.)
4. **Replay protection.** Unique index on (proof-id, network) at the
   settlement layer. Cheap, directly reusable alongside our namespaced
   `<network>:<txhash>` replay keys.

## Do not adopt

5. **Their facilitator trust model.** Verification delegates to their central
   server; Lightning *requires* their node's `ln_payments` ledger. That is
   the "pragmatic relayer" end of the spectrum — useful as a reference for
   what centralized looks like, but our design is ChainRelay + `hasValue()`,
   trust-minimized. Keep verification client-side/per-rail.
6. **Anything-to-anything stays ours.** They settle each rail natively to the
   merchant's per-chain wallet; no cross-chain conversion inside the x402
   flow. Our converter (anything→anything when `hasValue()` true, path
   exists, survives fees) is a genuine differentiator over them.

## Bugs found there → rules for us

7. **Lightning unit bug.** Their `$5` offer emitted `"maxAmountRequired": "0"`
   on the Lightning entry (sub-sat rounds to zero at `decimals: 0`) while the
   verifier expects millisats — the x402 amount check on Lightning was
   vacuous. Rule: every rail's amount field gets a unit test asserting
   nonzero for a $1 offer, in the rail's native smallest unit.
8. **Advertised ≠ working.** Docs list BCH; the settle route refuses it ("no
   verified lookup wired up"). Rule: a rail is listed in `accepts` only when
   its verify path is tested green.
9. **Fee honesty.** Their 1.0%/0.5% x402 commission is not actually collected
   (buyer pays the merchant wallet directly; nothing to deduct from) while
   the pricing page implies otherwise. Rule: our fee story must match the
   code path — what the docs say the fee is must be what the code collects.
10. **Audit follow-through.** Their third-party audit (338 findings) is mostly
    not visibly dispositioned in-repo. If we ever commission one, every
    finding gets a disposition row or it didn't happen.
