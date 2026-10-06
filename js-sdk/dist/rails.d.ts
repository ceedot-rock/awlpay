/**
 * Rail configurations for AwLPay.
 *
 * Rails are a pluggable map: to add a new chain, add an entry to RAILS and a
 * matching adapter factory in ADAPTER_FACTORIES (see wallet.ts). No other
 * code needs to change.
 */
export type Network = "testnet" | "mainnet";
export type RailId = "base" | "solana";
export interface RailConfig {
    /** Rail identifier, e.g. "base" or "solana". */
    id: RailId;
    /** "testnet" or "mainnet". */
    network: Network;
    /** EVM chain id (Base only). */
    chainId?: number;
    /** USDC contract (Base) or mint (Solana). USDC has 6 decimals everywhere. */
    usdc: string;
    /** Public RPC endpoint. */
    rpc: string;
    /** Block explorer base URL (where available). */
    explorer?: string;
    /**
     * Rough estimate of the fee for one USDC transfer on this rail, in USD.
     * Used ONLY for cheapest-funded-rail auto-selection — not a fee quote.
     */
    estFeeUsd: number;
}
/** USDC uses 6 decimals on every rail. */
export declare const USDC_DECIMALS = 6;
export declare const RAILS: Record<Network, Record<RailId, RailConfig>>;
export declare const SUPPORTED_RAILS: readonly RailId[];
/** Return the config for one rail on one network. */
export declare function railConfig(network: Network, rail: RailId): RailConfig;
/**
 * Rails sorted cheapest-first by estimated transfer fee.
 * Used for auto-selection among funded rails; Solana currently wins.
 */
export declare function railsByFee(network: Network): RailConfig[];
//# sourceMappingURL=rails.d.ts.map