/**
 * AgentWallet — the AwLPay wallet an agent owns and spends from.
 *
 * Plain USDC transfers are fully local: build, sign, submit, confirm. No
 * AwLPay server is involved. Balances are read on-chain per rail.
 *
 * Example:
 *   const w = await AgentWallet.create({ password: "..." }); // testnet default
 *   await w.depositAddress("base");      // "0x..."
 *   await w.depositAddress("solana");    // base58
 *   await w.balances();                  // { base: 0, solana: 0 } in USD
 *   const r = await w.pay("0x...", 1.5); // receipt
 */
import { type Network, type RailConfig, type RailId } from "./rails.js";
import { type WalletRecord } from "./store.js";
export declare const MAINNET_CONFIRM_STRING = "I UNDERSTAND";
export declare const DEFAULT_MAX_SPEND_USD = 100;
export interface CreateOptions {
    /** Encrypts the wallet file. Never written to disk. */
    password: string;
    /** "testnet" (default) or "mainnet". */
    network?: Network;
    /** Required to be exactly "I UNDERSTAND" when network is "mainnet". */
    confirmMainnet?: string;
    /** Lifetime spend cap in USD. Default 100. */
    maxSpendUsd?: number;
}
export interface PayOptions {
    /** "auto" (default) picks the cheapest funded rail; or name a rail. */
    rail?: RailId | "auto";
    /** Per-call cap in USD. Defaults to the wallet's lifetime cap. */
    maxUsd?: number;
}
export interface Receipt {
    rail: RailId;
    txHash: string;
    usd: number;
    to: string;
    confirmedAt: string;
    network: Network;
}
/**
 * One rail's on-chain operations. Implementations sign locally with the
 * wallet's own keys; the transfer method MUST only resolve after the
 * transaction is confirmed.
 */
export interface RailAdapter {
    id: RailId;
    config: RailConfig;
    /** Deposit address for this rail. */
    address(): string;
    /** Raw USDC balance (6 decimals) of this wallet on this rail. */
    balanceUsdcRaw(): Promise<bigint>;
    /** Confirmed USDC transfer of raw units; resolves with the tx hash/signature. */
    transferUsdc(to: string, amountRaw: bigint): Promise<string>;
}
export type AdapterFactory = (record: WalletRecord, config: RailConfig) => RailAdapter;
/** Pluggable rail map — add a chain here and in RAILS to support it. */
export declare const ADAPTER_FACTORIES: Record<RailId, AdapterFactory>;
/** Test-only seam: inject mocked adapter factories instead of real chain clients. */
export interface WalletDeps {
    adapters?: Partial<Record<RailId, AdapterFactory>>;
}
export declare class AgentWallet {
    private record;
    private password;
    private adapters;
    private constructor();
    /**
     * Create a new wallet (first run) or load the existing one.
     * - First run generates fresh keys: one secp256k1 (Base), one ed25519 (Solana).
     * - Later runs decrypt the stored wallet with the same password.
     * - network defaults to "testnet"; mainnet needs confirmMainnet: "I UNDERSTAND".
     */
    static create(opts: CreateOptions, deps?: WalletDeps): Promise<AgentWallet>;
    /** Which network this wallet lives on. */
    get network(): Network;
    /** Deposit address for a rail: "base" -> 0x..., "solana" -> base58. */
    depositAddress(rail: string): Promise<string>;
    /** Per-rail balances in USD (USDC is 1:1 with USD). */
    balances(): Promise<Record<RailId, number>>;
    /** USD spent across this wallet's lifetime (persisted, confirmed payments only). */
    lifetimeSpentUsd(): Promise<number>;
    /**
     * Pay `usd` USDC to `to`. Signs locally, submits, waits for confirmation,
     * then updates the lifetime counter and returns a receipt.
     */
    pay(to: string, usd: number, opts?: PayOptions): Promise<Receipt>;
    private selectRail;
}
//# sourceMappingURL=wallet.d.ts.map