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

import { createPublicClient, createWalletClient, http, erc20Abi } from "viem";
import { generatePrivateKey, privateKeyToAccount } from "viem/accounts";
import { base, baseSepolia } from "viem/chains";
import {
  Connection,
  Keypair,
  PublicKey,
  Transaction,
  sendAndConfirmTransaction,
} from "@solana/web3.js";
import {
  createAssociatedTokenAccountInstruction,
  createTransferInstruction,
  getAssociatedTokenAddress,
} from "@solana/spl-token";

import {
  RAILS,
  SUPPORTED_RAILS,
  USDC_DECIMALS,
  railsByFee,
  type Network,
  type RailConfig,
  type RailId,
} from "./rails.js";
import {
  loadWallet,
  saveWallet,
  walletExists,
  type WalletRecord,
} from "./store.js";
import {
  InsufficientFundsError,
  MainnetConfirmationError,
  SpendCapExceededError,
  UnsupportedRailError,
} from "./errors.js";

export const MAINNET_CONFIRM_STRING = "I UNDERSTAND";
export const DEFAULT_MAX_SPEND_USD = 100;

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

/* ------------------------------------------------------------------ */
/* Real adapters                                                       */
/* ------------------------------------------------------------------ */

function baseAdapter(record: WalletRecord, config: RailConfig): RailAdapter {
  if (config.chainId === undefined) {
    throw new Error("awlpay: base rail config missing chainId");
  }
  const chain = config.network === "mainnet" ? base : baseSepolia;
  const account = privateKeyToAccount(record.basePrivKey);
  const transport = http(config.rpc);
  const pub = createPublicClient({ chain, transport });
  const wallet = createWalletClient({ account, chain, transport });
  const usdc = config.usdc as `0x${string}`;

  return {
    id: "base",
    config,
    address: () => account.address,
    balanceUsdcRaw: () =>
      pub.readContract({
        address: usdc,
        abi: erc20Abi,
        functionName: "balanceOf",
        args: [account.address],
      }) as Promise<bigint>,
    transferUsdc: async (to, amountRaw) => {
      const hash = await wallet.writeContract({
        account,
        address: usdc,
        abi: erc20Abi,
        functionName: "transfer",
        args: [to as `0x${string}`, amountRaw],
      });
      await pub.waitForTransactionReceipt({ hash });
      return hash;
    },
  };
}

function solanaAdapter(record: WalletRecord, config: RailConfig): RailAdapter {
  const connection = new Connection(config.rpc, "confirmed");
  const mint = new PublicKey(config.usdc);
  const keypair = Keypair.fromSecretKey(
    Buffer.from(record.solanaSecretKey, "base64"),
  );

  const senderAta = () => getAssociatedTokenAddress(mint, keypair.publicKey);

  return {
    id: "solana",
    config,
    address: () => keypair.publicKey.toBase58(),
    balanceUsdcRaw: async () => {
      const ata = await senderAta();
      const info = await connection.getAccountInfo(ata);
      if (!info) return 0n;
      const bal = await connection.getTokenAccountBalance(ata);
      return BigInt(bal.value.amount);
    },
    transferUsdc: async (to, amountRaw) => {
      const fromAta = await senderAta();
      const toPubkey = new PublicKey(to);
      const toAta = await getAssociatedTokenAddress(mint, toPubkey);
      const tx = new Transaction();
      // Create the destination associated token account if it doesn't exist.
      if (!(await connection.getAccountInfo(toAta))) {
        tx.add(
          createAssociatedTokenAccountInstruction(
            keypair.publicKey,
            toAta,
            toPubkey,
            mint,
          ),
        );
      }
      tx.add(
        createTransferInstruction(
          fromAta,
          toAta,
          keypair.publicKey,
          amountRaw,
        ),
      );
      return await sendAndConfirmTransaction(connection, tx, [keypair]);
    },
  };
}

/** Pluggable rail map — add a chain here and in RAILS to support it. */
export const ADAPTER_FACTORIES: Record<RailId, AdapterFactory> = {
  base: baseAdapter,
  solana: solanaAdapter,
};

/* ------------------------------------------------------------------ */
/* Wallet                                                              */
/* ------------------------------------------------------------------ */

/** Test-only seam: inject mocked adapter factories instead of real chain clients. */
export interface WalletDeps {
  adapters?: Partial<Record<RailId, AdapterFactory>>;
}

function assertSupportedRail(rail: string): asserts rail is RailId {
  if (!(SUPPORTED_RAILS as readonly string[]).includes(rail)) {
    throw new UnsupportedRailError(
      `awlpay: unsupported rail "${rail}" — supported: ${SUPPORTED_RAILS.join(", ")}`,
    );
  }
}

function assertValidDestination(rail: RailId, to: string): void {
  if (rail === "base") {
    if (!/^0x[0-9a-fA-F]{40}$/.test(to)) {
      throw new Error(`awlpay: invalid base destination address "${to}"`);
    }
    return;
  }
  try {
    new PublicKey(to);
  } catch {
    throw new Error(`awlpay: invalid solana destination address "${to}"`);
  }
}

export class AgentWallet {
  private constructor(
    private record: WalletRecord,
    private password: string,
    private adapters: Record<RailId, RailAdapter>,
  ) {}

  /**
   * Create a new wallet (first run) or load the existing one.
   * - First run generates fresh keys: one secp256k1 (Base), one ed25519 (Solana).
   * - Later runs decrypt the stored wallet with the same password.
   * - network defaults to "testnet"; mainnet needs confirmMainnet: "I UNDERSTAND".
   */
  static async create(opts: CreateOptions, deps: WalletDeps = {}): Promise<AgentWallet> {
    const { password, confirmMainnet, maxSpendUsd = DEFAULT_MAX_SPEND_USD } = opts;
    const network: Network = opts.network ?? "testnet";

    if (!password) {
      throw new Error("awlpay: a password is required");
    }
    if (network === "mainnet" && confirmMainnet !== MAINNET_CONFIRM_STRING) {
      throw new MainnetConfirmationError(
        `awlpay: mainnet requires confirmMainnet: "${MAINNET_CONFIRM_STRING}" (exact string)`,
      );
    }

    let record: WalletRecord;
    if (walletExists()) {
      record = loadWallet(password); // throws on wrong password
      if (record.network !== network) {
        throw new Error(
          `awlpay: existing wallet is on ${record.network}, but "${network}" was requested`,
        );
      }
    } else {
      record = {
        version: 1,
        network,
        basePrivKey: generatePrivateKey(),
        solanaSecretKey: Buffer.from(Keypair.generate().secretKey).toString("base64"),
        maxSpendUsd,
        lifetimeSpentUsd: 0,
        createdAt: new Date().toISOString(),
      };
      saveWallet(record, password);
    }

    const adapters = {} as Record<RailId, RailAdapter>;
    for (const rail of SUPPORTED_RAILS) {
      adapters[rail] =
        deps.adapters?.[rail]?.(record, RAILS[network][rail]) ??
        ADAPTER_FACTORIES[rail](record, RAILS[network][rail]);
    }
    return new AgentWallet(record, password, adapters);
  }

  /** Which network this wallet lives on. */
  get network(): Network {
    return this.record.network;
  }

  /** Deposit address for a rail: "base" -> 0x..., "solana" -> base58. */
  async depositAddress(rail: string): Promise<string> {
    assertSupportedRail(rail);
    return this.adapters[rail].address();
  }

  /** Per-rail balances in USD (USDC is 1:1 with USD). */
  async balances(): Promise<Record<RailId, number>> {
    const out = {} as Record<RailId, number>;
    for (const rail of SUPPORTED_RAILS) {
      const raw = await this.adapters[rail].balanceUsdcRaw();
      out[rail] = Number(raw) / 10 ** USDC_DECIMALS;
    }
    return out;
  }

  /** USD spent across this wallet's lifetime (persisted, confirmed payments only). */
  async lifetimeSpentUsd(): Promise<number> {
    return this.record.lifetimeSpentUsd;
  }

  /**
   * Pay `usd` USDC to `to`. Signs locally, submits, waits for confirmation,
   * then updates the lifetime counter and returns a receipt.
   */
  async pay(to: string, usd: number, opts: PayOptions = {}): Promise<Receipt> {
    if (!Number.isFinite(usd) || usd <= 0) {
      throw new Error("awlpay: usd must be a positive number");
    }
    const perCallCap = opts.maxUsd ?? this.record.maxSpendUsd;
    if (usd > perCallCap) {
      throw new SpendCapExceededError(
        `awlpay: payment of $${usd} exceeds per-call cap of $${perCallCap}`,
      );
    }
    if (this.record.lifetimeSpentUsd + usd > this.record.maxSpendUsd) {
      throw new SpendCapExceededError(
        `awlpay: payment of $${usd} would exceed lifetime cap of $${this.record.maxSpendUsd} (spent $${this.record.lifetimeSpentUsd})`,
      );
    }

    const rail = await this.selectRail(usd, opts.rail ?? "auto");
    assertValidDestination(rail, to);

    const amountRaw = BigInt(Math.round(usd * 10 ** USDC_DECIMALS));
    if (amountRaw <= 0n) {
      throw new Error("awlpay: amount below 1 micro-USDC — increase usd");
    }

    // Transfer only resolves after on-chain confirmation.
    const txHash = await this.adapters[rail].transferUsdc(to, amountRaw);

    // Lifetime counter moves only after a confirmed payment.
    this.record.lifetimeSpentUsd += usd;
    saveWallet(this.record, this.password);

    return {
      rail,
      txHash,
      usd,
      to,
      confirmedAt: new Date().toISOString(),
      network: this.record.network,
    };
  }

  private async selectRail(usd: number, railOpt: RailId | "auto"): Promise<RailId> {
    if (railOpt !== "auto") {
      assertSupportedRail(railOpt);
      const raw = await this.adapters[railOpt].balanceUsdcRaw();
      const bal = Number(raw) / 10 ** USDC_DECIMALS;
      if (bal < usd) {
        throw new InsufficientFundsError(
          `awlpay: rail "${railOpt}" has $${bal} but payment needs $${usd}`,
          { [railOpt]: bal },
        );
      }
      return railOpt;
    }

    // auto: cheapest FUNDED rail wins (balance >= usd). Solana is currently
    // cheapest (~$0.0003 vs ~$0.02 on Base — rough estimates, not quotes).
    const balances = await this.balances();
    for (const cfg of railsByFee(this.record.network)) {
      if (balances[cfg.id] >= usd) {
        return cfg.id;
      }
    }
    throw new InsufficientFundsError(
      `awlpay: no rail funded for $${usd} — top up a deposit address first`,
      balances,
    );
  }
}
