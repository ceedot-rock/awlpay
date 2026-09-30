import { generateKeyPairSync, randomBytes } from 'node:crypto';
import type { Chain } from './types.js';

export type Network = 'solana-devnet' | 'base-sepolia' | 'ethereum-sepolia';

export const DEFAULT_NETWORK: Record<Chain, Network> = {
  solana: 'solana-devnet',
  base: 'base-sepolia',
  ethereum: 'ethereum-sepolia',
};

export const NETWORKS: Record<
  Network,
  {
    chain: Chain;
    chain_id: string;
    faucet: string;
    explorer: string;
    label: string;
  }
> = {
  'solana-devnet': {
    chain: 'solana',
    chain_id: 'solana-devnet',
    faucet: 'https://faucet.solana.com',
    explorer: 'https://explorer.solana.com/?cluster=devnet',
    label: 'Solana devnet',
  },
  'base-sepolia': {
    chain: 'base',
    chain_id: 'eip155:84532',
    faucet: 'https://www.alchemy.com/faucets/base-sepolia',
    explorer: 'https://sepolia.basescan.org',
    label: 'Base Sepolia',
  },
  'ethereum-sepolia': {
    chain: 'ethereum',
    chain_id: 'eip155:11155111',
    faucet: 'https://sepoliafaucet.com',
    explorer: 'https://sepolia.etherscan.io',
    label: 'Ethereum Sepolia',
  },
};

const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

function base58Encode(bytes: Uint8Array): string {
  if (bytes.length === 0) return '';
  const digits = [0];
  for (const byte of bytes) {
    let carry = byte;
    for (let i = 0; i < digits.length; i += 1) {
      carry += digits[i] << 8;
      digits[i] = carry % 58;
      carry = (carry / 58) | 0;
    }
    while (carry > 0) {
      digits.push(carry % 58);
      carry = (carry / 58) | 0;
    }
  }
  let zeros = 0;
  for (const b of bytes) {
    if (b === 0) zeros += 1;
    else break;
  }
  return '1'.repeat(zeros) + digits.reverse().map((d) => B58[d]).join('');
}

export function isNetwork(value: string): value is Network {
  return Object.prototype.hasOwnProperty.call(NETWORKS, value);
}

export function resolveNetwork(chain: Chain, network?: string): Network | { error: string; reason: string } {
  if (network) {
    const n = network.trim().toLowerCase();
    if (n === 'mainnet' || n === 'mainnet-beta' || n.endsWith('-mainnet')) {
      return { error: 'refuse', reason: 'mainnet_forbidden' };
    }
    if (!isNetwork(n)) return { error: 'refuse', reason: 'invalid_network' };
    if (NETWORKS[n].chain !== chain) return { error: 'refuse', reason: 'network_chain_mismatch' };
    return n;
  }
  return DEFAULT_NETWORK[chain];
}

export function mintTestnetAddress(network: Network): {
  address: string;
  address_kind: 'ed25519_pubkey' | 'evm_shaped';
} {
  if (network === 'solana-devnet') {
    const { publicKey } = generateKeyPairSync('ed25519');
    const der = publicKey.export({ type: 'spki', format: 'der' });
    const raw = der.subarray(der.length - 32);
    return { address: base58Encode(raw), address_kind: 'ed25519_pubkey' };
  }
  const hex = randomBytes(20).toString('hex');
  return { address: `0x${hex}`, address_kind: 'evm_shaped' };
}
