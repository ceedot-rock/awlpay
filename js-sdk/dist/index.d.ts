/**
 * awlpay — JavaScript/TypeScript SDK for AwLPay multi-rail x402 payments.
 *
 * Zero crypto background required:
 *   import { AgentWallet } from "awlpay";
 *   const w = await AgentWallet.create({ password: "..." }); // testnet default
 *   await w.depositAddress("base");       // fund it, then:
 *   const receipt = await w.pay("0x...", 1.5);
 */
export { AgentWallet, MAINNET_CONFIRM_STRING, DEFAULT_MAX_SPEND_USD } from "./wallet.js";
export type { CreateOptions, PayOptions, Receipt, RailAdapter, WalletDeps, AdapterFactory } from "./wallet.js";
export { ADAPTER_FACTORIES } from "./wallet.js";
export { RAILS, SUPPORTED_RAILS, USDC_DECIMALS, railConfig, railsByFee, } from "./rails.js";
export type { Network, RailConfig, RailId } from "./rails.js";
export { AwlpayError, SpendCapExceededError, InsufficientFundsError, MainnetConfirmationError, UnsupportedRailError, } from "./errors.js";
export { storeDir, walletFilePath, walletExists } from "./store.js";
//# sourceMappingURL=index.d.ts.map