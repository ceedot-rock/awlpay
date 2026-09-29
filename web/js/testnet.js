/** Testnet map + Solana keygen + public RPC reads. No mainnet. */

export const NETWORKS = {
  solana: {
    id: 'solana-devnet',
    label: 'Solana devnet',
    chain: 'solana',
    rpc: 'https://api.devnet.solana.com',
    explorer: 'https://explorer.solana.com',
    faucet: 'https://faucet.solana.com',
    addressKind: 'solana',
  },
  base: {
    id: 'base-sepolia',
    label: 'Base Sepolia',
    chain: 'base',
    chainId: 84532,
    rpc: 'https://sepolia.base.org',
    explorer: 'https://sepolia.basescan.org',
    faucet: 'https://www.alchemy.com/faucets/base-sepolia',
    addressKind: 'evm',
  },
  ethereum: {
    id: 'ethereum-sepolia',
    label: 'Ethereum Sepolia',
    chain: 'ethereum',
    chainId: 11155111,
    rpc: 'https://rpc.sepolia.org',
    explorer: 'https://sepolia.etherscan.io',
    faucet: 'https://sepoliafaucet.com',
    addressKind: 'evm',
  },
};

const B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';

export function networkOf(chain) {
  return NETWORKS[chain] || null;
}

export function isEvmAddress(value) {
  return /^0x[0-9a-fA-F]{40}$/.test(String(value || '').trim());
}

function bytesToHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

function base58Encode(bytes) {
  let zeros = 0;
  while (zeros < bytes.length && bytes[zeros] === 0) zeros += 1;
  const digits = [0];
  for (let i = zeros; i < bytes.length; i += 1) {
    let carry = bytes[i];
    for (let j = 0; j < digits.length; j += 1) {
      carry += digits[j] << 8;
      digits[j] = carry % 58;
      carry = (carry / 58) | 0;
    }
    while (carry > 0) {
      digits.push(carry % 58);
      carry = (carry / 58) | 0;
    }
  }
  let out = '1'.repeat(zeros);
  for (let i = digits.length - 1; i >= 0; i -= 1) out += B58[digits[i]];
  return out;
}

export async function mintTestnetAddress(chain, { agent = false, attach_address = '' } = {}) {
  const net = networkOf(chain);
  if (!net) throw new Error('Unknown chain.');

  const attached = String(attach_address || '').trim();
  if (net.addressKind === 'evm') {
    if (attached && !isEvmAddress(attached)) {
      throw new Error('EVM address must be 0x plus 40 hex characters.');
    }
    if (attached) {
      return {
        address: attached,
        address_source: 'attached',
        spendable: true,
        holds_secret: false,
        network: net.id,
      };
    }
    const raw = crypto.getRandomValues(new Uint8Array(20));
    return {
      address: `0x${bytesToHex(raw)}`,
      address_source: 'watch_label',
      spendable: false,
      holds_secret: false,
      network: net.id,
      note: 'Watch label only. Paste a Sepolia address you control to read a real testnet balance.',
    };
  }

  if (!crypto.subtle || typeof crypto.subtle.generateKey !== 'function') {
    throw new Error('This browser cannot mint a Solana testnet key.');
  }
  const key = await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify']);
  const pub = new Uint8Array(await crypto.subtle.exportKey('raw', key.publicKey));
  const address = base58Encode(pub);
  let secret_b64 = null;
  if (!agent) {
    const pkcs8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', key.privateKey));
    secret_b64 = btoa(String.fromCharCode(...pkcs8));
  }
  return {
    address,
    address_source: 'ed25519',
    spendable: !agent,
    holds_secret: Boolean(secret_b64),
    secret_b64,
    network: net.id,
  };
}

export async function readTestnetBalance(chain, address) {
  const net = networkOf(chain);
  if (!net || !address) {
    return { ok: false, reason: 'Need a testnet address.' };
  }
  try {
    if (net.addressKind === 'solana') {
      const res = await fetch(net.rpc, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          jsonrpc: '2.0',
          id: 1,
          method: 'getBalance',
          params: [address],
        }),
      });
      const json = await res.json();
      if (json.error) return { ok: false, reason: json.error.message, network: net.id };
      return {
        ok: true,
        network: net.id,
        address,
        lamports: json.result?.value ?? 0,
        sol: (json.result?.value ?? 0) / 1_000_000_000,
        source: 'solana-devnet-rpc',
      };
    }
    const res = await fetch(net.rpc, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({
        jsonrpc: '2.0',
        id: 1,
        method: 'eth_getBalance',
        params: [address, 'latest'],
      }),
    });
    const json = await res.json();
    if (json.error) return { ok: false, reason: json.error.message, network: net.id };
    const wei = BigInt(json.result || '0x0');
    return {
      ok: true,
      network: net.id,
      address,
      wei: wei.toString(),
      eth: Number(wei) / 1e18,
      source: `${net.id}-rpc`,
    };
  } catch (err) {
    return { ok: false, reason: err.message || 'RPC failed', network: net.id };
  }
}
