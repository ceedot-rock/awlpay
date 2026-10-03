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
export const USDC_DECIMALS = 6;

export const RAILS: Record<Network, Record<RailId, RailConfig>> = {
  testnet: {
    base: {
      id: "base",
      network: "testnet",
      chainId: 84532,
      usdc: "0x036CbD53842c5426634e7929541eC2318f3dCF7e",
      rpc: "https://sepolia.base.org",
      explorer: "https://sepolia.basescan.org",
      estFeeUsd: 0.02,
    },
    solana: {
      id: "solana",
      network: "testnet",
      usdc: "4zMMC9srt5Ri5X14GAgXhaHii3GnPAEERYPJgZJDncDU",
      rpc: "https://api.devnet.solana.com",
      estFeeUsd: 0.0003,
    },
  },
  mainnet: {
    base: {
      id: "base",
      network: "mainnet",
      chainId: 8453,
      usdc: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      rpc: "https://mainnet.base.org",
      estFeeUsd: 0.02,
    },
    solana: {
      id: "solana",
      network: "mainnet",
      usdc: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
      rpc: "https://api.mainnet-beta.solana.com",
      estFeeUsd: 0.0003,
    },
  },
};

export const SUPPORTED_RAILS: readonly RailId[] = ["base", "solana"];

/** Return the config for one rail on one network. */
export function railConfig(network: Network, rail: RailId): RailConfig {
  return RAILS[network][rail];
}

/**
 * Rails sorted cheapest-first by estimated transfer fee.
 * Used for auto-selection among funded rails; Solana currently wins.
 */
export function railsByFee(network: Network): RailConfig[] {
  return SUPPORTED_RAILS.map((id) => RAILS[network][id]).sort(
    (a, b) => a.estFeeUsd - b.estFeeUsd,
  );
}
