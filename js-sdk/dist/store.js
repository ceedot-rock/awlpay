/**
 * Encrypted wallet storage.
 *
 * The wallet record (both private keys, spend counters, network) is sealed
 * with AES-256-GCM via node:crypto. The 32-byte key is derived from the
 * user's password with scrypt (N=16384, r=8, p=1) and a fresh random salt.
 * The password is never written to disk. A wrong password fails at
 * authentication — the GCM tag check — never silently decrypts garbage.
 */
import { randomBytes, scryptSync, createCipheriv, createDecipheriv } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync, existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
const SCRYPT_N = 16384;
const SCRYPT_R = 8;
const SCRYPT_P = 1;
/** Directory holding wallets.enc. Overridable via AWL_PAY_DIR (used by tests). */
export function storeDir() {
    return process.env.AWL_PAY_DIR ?? join(homedir(), ".awlpay");
}
export function walletFilePath() {
    return join(storeDir(), "wallets.enc");
}
function deriveKey(password, salt) {
    return scryptSync(password, salt, 32, { N: SCRYPT_N, r: SCRYPT_R, p: SCRYPT_P });
}
export function encryptRecord(record, password) {
    const salt = randomBytes(16);
    const nonce = randomBytes(12);
    const key = deriveKey(password, salt);
    const cipher = createCipheriv("aes-256-gcm", key, nonce);
    const plaintext = Buffer.from(JSON.stringify(record), "utf8");
    const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()]);
    const file = {
        kdf: "scrypt",
        n: SCRYPT_N,
        r: SCRYPT_R,
        p: SCRYPT_P,
        salt: salt.toString("base64"),
        nonce: nonce.toString("base64"),
        tag: cipher.getAuthTag().toString("base64"),
        ciphertext: ciphertext.toString("base64"),
    };
    return JSON.stringify(file, null, 2);
}
export function decryptRecord(fileJson, password) {
    const file = JSON.parse(fileJson);
    if (file.kdf !== "scrypt" || file.n !== SCRYPT_N) {
        throw new Error("awlpay: unsupported wallet file format");
    }
    const key = deriveKey(password, Buffer.from(file.salt, "base64"));
    const decipher = createDecipheriv("aes-256-gcm", key, Buffer.from(file.nonce, "base64"));
    decipher.setAuthTag(Buffer.from(file.tag, "base64"));
    let plaintext;
    try {
        plaintext = Buffer.concat([
            decipher.update(Buffer.from(file.ciphertext, "base64")),
            decipher.final(),
        ]);
    }
    catch {
        // Wrong password (or corrupted file): the GCM tag check fails here.
        throw new Error("awlpay: wrong password or corrupted wallet file");
    }
    return JSON.parse(plaintext.toString("utf8"));
}
/** Persist a wallet record (mode 700 dir, 600 file). */
export function saveWallet(record, password) {
    const dir = storeDir();
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    const path = walletFilePath();
    writeFileSync(path, encryptRecord(record, password), { mode: 0o600 });
}
export function walletExists() {
    return existsSync(walletFilePath());
}
/** Load and decrypt the wallet record; throws on a wrong password. */
export function loadWallet(password) {
    const path = walletFilePath();
    if (!existsSync(path)) {
        throw new Error(`awlpay: no wallet found at ${path} — create one first`);
    }
    return decryptRecord(readFileSync(path, "utf8"), password);
}
//# sourceMappingURL=store.js.map