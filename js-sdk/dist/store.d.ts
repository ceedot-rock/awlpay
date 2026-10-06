/**
 * Encrypted wallet storage.
 *
 * The wallet record (both private keys, spend counters, network) is sealed
 * with AES-256-GCM via node:crypto. The 32-byte key is derived from the
 * user's password with scrypt (N=16384, r=8, p=1) and a fresh random salt.
 * The password is never written to disk. A wrong password fails at
 * authentication — the GCM tag check — never silently decrypts garbage.
 */
import type { Network } from "./rails.js";
export interface WalletRecord {
    version: 1;
    network: Network;
    /** Base (secp256k1) private key, 0x-prefixed hex. */
    basePrivKey: `0x${string}`;
    /** Solana (ed25519) 64-byte secret key, base58. */
    solanaSecretKey: string;
    /** Lifetime spend cap in USD, set at wallet creation. */
    maxSpendUsd: number;
    /** USD spent so far, updated only after a CONFIRMED payment. */
    lifetimeSpentUsd: number;
    createdAt: string;
}
/** Directory holding wallets.enc. Overridable via AWL_PAY_DIR (used by tests). */
export declare function storeDir(): string;
export declare function walletFilePath(): string;
export declare function encryptRecord(record: WalletRecord, password: string): string;
export declare function decryptRecord(fileJson: string, password: string): WalletRecord;
/** Persist a wallet record (mode 700 dir, 600 file). */
export declare function saveWallet(record: WalletRecord, password: string): void;
export declare function walletExists(): boolean;
/** Load and decrypt the wallet record; throws on a wrong password. */
export declare function loadWallet(password: string): WalletRecord;
//# sourceMappingURL=store.d.ts.map