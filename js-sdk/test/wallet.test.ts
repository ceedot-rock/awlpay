/**
 * AwLPay JS SDK tests — all offline. Chain clients are replaced with mock
 * RailAdapter factories; the wallet file lives in a fresh temp dir per test
 * via AWL_PAY_DIR. No network, no real funds, no mainnet writes.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Keypair } from "@solana/web3.js";
import { privateKeyToAccount } from "viem/accounts";

import {
  AgentWallet,
  InsufficientFundsError,
  MainnetConfirmationError,
  SpendCapExceededError,
  UnsupportedRailError,
  walletFilePath,
  RAILS,
  type AdapterFactory,
  type RailAdapter,
  type RailId,
} from "../src/index.js";
import type { WalletRecord } from "../src/store.js";
import type { RailConfig } from "../src/index.js";

const PW = "correct horse battery staple";

interface MockOpts {
  balanceRaw?: bigint;
  failTransfer?: boolean;
}

interface TransferCall {
  to: string;
  amountRaw: bigint;
}

/**
 * Mock adapter factory: derives addresses from the wallet record exactly
 * like the real adapters do, but balances and transfers are scripted.
 */
function mockFactory(
  id: RailId,
  opts: MockOpts = {},
): AdapterFactory & { transfers: TransferCall[] } {
  const transfers: TransferCall[] = [];
  const factory = (record: WalletRecord, config: RailConfig): RailAdapter => {
    const address =
      id === "base"
        ? privateKeyToAccount(record.basePrivKey).address
        : Keypair.fromSecretKey(
            Buffer.from(record.solanaSecretKey, "base64"),
          ).publicKey.toBase58();
    return {
      id,
      config,
      address: () => address,
      balanceUsdcRaw: async () => opts.balanceRaw ?? 0n,
      transferUsdc: async (to, amountRaw) => {
        if (opts.failTransfer) throw new Error("mock chain failure");
        transfers.push({ to, amountRaw });
        return id === "base" ? `0x${"ab".repeat(32)}` : "5".repeat(87);
      },
    };
  };
  return Object.assign(factory, { transfers });
}

type Factories = Partial<Record<RailId, AdapterFactory & { transfers: TransferCall[] }>>;

function adapters(opts: { base?: MockOpts; solana?: MockOpts } = {}): Factories {
  return {
    base: mockFactory("base", opts.base),
    solana: mockFactory("solana", opts.solana),
  };
}

const FUNDED = {
  base: { balanceRaw: 10_000_000n }, // $10
  solana: { balanceRaw: 10_000_000n }, // $10
};

const TO_BASE = "0x" + "22".repeat(20);
const TO_SOLANA = Keypair.generate().publicKey.toBase58();

beforeEach(() => {
  process.env.AWL_PAY_DIR = mkdtempSync(join(tmpdir(), "awlpay-test-"));
});

describe("wallet persistence", () => {
  it("encrypt/decrypt roundtrip: same addresses after reload", async () => {
    const w1 = await AgentWallet.create({ password: PW }, { adapters: adapters() });
    const base1 = await w1.depositAddress("base");
    const sol1 = await w1.depositAddress("solana");
    expect(base1).toMatch(/^0x[0-9a-fA-F]{40}$/);
    expect(sol1).toMatch(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/);

    // New instance, same password + same dir → loads the stored keys.
    const w2 = await AgentWallet.create({ password: PW }, { adapters: adapters() });
    expect(await w2.depositAddress("base")).toBe(base1);
    expect(await w2.depositAddress("solana")).toBe(sol1);
  });

  it("wrong password fails", async () => {
    await AgentWallet.create({ password: PW }, { adapters: adapters() });
    await expect(
      AgentWallet.create({ password: "wrong password" }, { adapters: adapters() }),
    ).rejects.toThrow(/wrong password/);
  });

  it("password is never written to disk", async () => {
    await AgentWallet.create({ password: PW }, { adapters: adapters() });
    const raw = readFileSync(walletFilePath(), "utf8");
    expect(raw).not.toContain(PW);
  });
});

describe("network selection", () => {
  it("testnet is the default", async () => {
    const w = await AgentWallet.create({ password: PW }, { adapters: adapters() });
    expect(w.network).toBe("testnet");
  });

  it("mainnet without the exact confirmation string throws", async () => {
    await expect(
      AgentWallet.create({ password: PW, network: "mainnet" }, { adapters: adapters() }),
    ).rejects.toThrow(MainnetConfirmationError);
  });

  it("mainnet with a near-miss confirmation string throws", async () => {
    await expect(
      AgentWallet.create(
        { password: PW, network: "mainnet", confirmMainnet: "i understand" },
        { adapters: adapters() },
      ),
    ).rejects.toThrow(MainnetConfirmationError);
  });

  it("mainnet with the exact confirmation string works", async () => {
    const w = await AgentWallet.create(
      { password: PW, network: "mainnet", confirmMainnet: "I UNDERSTAND" },
      { adapters: adapters() },
    );
    expect(w.network).toBe("mainnet");
  });
});

describe("spend caps", () => {
  it("per-call maxUsd is enforced", async () => {
    const w = await AgentWallet.create(
      { password: PW, maxSpendUsd: 100 },
      { adapters: adapters(FUNDED) },
    );
    await expect(w.pay(TO_BASE, 1, { maxUsd: 0.5 })).rejects.toThrow(
      SpendCapExceededError,
    );
  });

  it("lifetime cap is enforced across payments", async () => {
    const w = await AgentWallet.create(
      { password: PW, maxSpendUsd: 2 },
      { adapters: adapters(FUNDED) },
    );
    await w.pay(TO_BASE, 1.5, { rail: "base" });
    expect(await w.lifetimeSpentUsd()).toBe(1.5);
    await expect(w.pay(TO_BASE, 1, { rail: "base" })).rejects.toThrow(
      SpendCapExceededError,
    );
    expect(await w.lifetimeSpentUsd()).toBe(1.5);
  });

  it("lifetime counter moves only after a confirmed payment", async () => {
    const a = adapters({ base: { balanceRaw: 10_000_000n, failTransfer: true } });
    const w = await AgentWallet.create(
      { password: PW, maxSpendUsd: 100 },
      { adapters: a },
    );
    await expect(w.pay(TO_BASE, 1, { rail: "base" })).rejects.toThrow(
      /mock chain failure/,
    );
    expect(await w.lifetimeSpentUsd()).toBe(0);
    expect(a.base!.transfers).toHaveLength(0);
  });
});

describe("rail auto-selection", () => {
  it("prefers solana (cheapest) when both rails are funded", async () => {
    const w = await AgentWallet.create({ password: PW }, { adapters: adapters(FUNDED) });
    const r = await w.pay(TO_SOLANA, 1, { rail: "auto" });
    expect(r.rail).toBe("solana");
  });

  it("falls back to base when only base is funded", async () => {
    const w = await AgentWallet.create(
      { password: PW },
      { adapters: adapters({ base: { balanceRaw: 10_000_000n } }) },
    );
    const r = await w.pay(TO_BASE, 1);
    expect(r.rail).toBe("base");
  });

  it("throws InsufficientFundsError with balances when nothing is funded", async () => {
    const w = await AgentWallet.create({ password: PW }, { adapters: adapters() });
    const err = await w.pay(TO_BASE, 1).catch((e) => e);
    expect(err).toBeInstanceOf(InsufficientFundsError);
    expect(err.balances).toEqual({ base: 0, solana: 0 });
  });

  it("explicit rail with insufficient balance throws", async () => {
    const w = await AgentWallet.create(
      { password: PW },
      { adapters: adapters({ solana: { balanceRaw: 10_000_000n } }) },
    );
    await expect(w.pay(TO_BASE, 1, { rail: "base" })).rejects.toThrow(
      InsufficientFundsError,
    );
  });
});

describe("balances and addresses", () => {
  it("balances() returns per-rail USD", async () => {
    const w = await AgentWallet.create(
      { password: PW },
      {
        adapters: adapters({
          base: { balanceRaw: 2_000_000n },
          solana: { balanceRaw: 500_000n },
        }),
      },
    );
    expect(await w.balances()).toEqual({ base: 2, solana: 0.5 });
  });

  it("solana deposit address is base58", async () => {
    const w = await AgentWallet.create({ password: PW }, { adapters: adapters() });
    const addr = await w.depositAddress("solana");
    expect(addr).toMatch(/^[1-9A-HJ-NP-Za-km-z]{32,44}$/);
  });

  it("unknown rail throws UnsupportedRailError", async () => {
    const w = await AgentWallet.create({ password: PW }, { adapters: adapters() });
    await expect(w.depositAddress("tron")).rejects.toThrow(UnsupportedRailError);
    await expect(
      w.pay(TO_BASE, 1, { rail: "tron" as RailId }),
    ).rejects.toThrow(UnsupportedRailError);
  });
});

describe("receipt", () => {
  it("has the documented shape after a confirmed payment", async () => {
    const w = await AgentWallet.create({ password: PW }, { adapters: adapters(FUNDED) });
    const r = await w.pay(TO_BASE, 1.5, { rail: "base" });
    expect(r.rail).toBe("base");
    expect(typeof r.txHash).toBe("string");
    expect(r.txHash.length).toBeGreaterThan(0);
    expect(r.usd).toBe(1.5);
    expect(r.to).toBe(TO_BASE);
    expect(typeof r.confirmedAt).toBe("string");
    expect(Number.isNaN(Date.parse(r.confirmedAt))).toBe(false);
    expect(r.network).toBe("testnet");
    // exact documented keys
    expect(Object.keys(r).sort()).toEqual(
      ["confirmedAt", "network", "rail", "to", "txHash", "usd"].sort(),
    );
  });
});

// Referenced so the RAILS import stays honest: configs exist for both networks.
void RAILS;
