/**
 * Demo stubs matching OpenAPI shapes. Local demo only — the live API is
 * https://awlpay.fly.dev (mock settlement).
 */
import { calculateFees } from './fees.js';

const store = {
  wallets: [],
};

function uid(prefix) {
  return `${prefix}_${Math.random().toString(36).slice(2, 10)}`;
}

function delay(ms = 280) {
  return new Promise((r) => setTimeout(r, ms));
}

export async function createWallet({ owner_id, chain, agent_wallet = false }) {
  await delay();
  if (!owner_id?.trim()) {
    return { error: 'Owner id is required.', status: 400 };
  }
  if (!['solana', 'base', 'ethereum'].includes(chain)) {
    return { error: 'Chain must be solana, base, or ethereum.', status: 400 };
  }
  const agent = Boolean(agent_wallet);
  const created = new Date();
  const wallet = {
    id: uid(agent ? 'awlt' : 'wlt'),
    owner_id: owner_id.trim(),
    chain,
    kind: agent ? 'agent' : 'personal',
    holds_long_term_key: !agent,
    address: `${chain.slice(0, 3)}_${uid('addr')}`,
    created_at: created.toISOString(),
    session_expires_at: agent ? new Date(created.getTime() + 15 * 60 * 1000).toISOString() : null,
    warrant: agent ? { actions: ['settle'], max_calls: 1, calls_used: 0 } : null,
    demo: true,
  };
  store.wallets.unshift(wallet);
  store[`bal_${wallet.id}`] = {
    wallet_id: wallet.id,
    balance_cents: 125000,
    currency: 'USD',
    demo: true,
  };
  return wallet;
}

export async function getBalance(wallet_id) {
  await delay();
  const bal = store[`bal_${wallet_id}`];
  if (!bal) {
    return {
      wallet_id,
      balance_cents: 0,
      currency: 'USD',
      demo: true,
      note: 'Stub balance — wallet not in this session.',
    };
  }
  return { ...bal };
}

export async function getQuote({ amount_cents, tier, volume_month_usd_cents = 0, txs_month = 0 }) {
  await delay(120);
  const fees = calculateFees(Number(amount_cents) || 0, tier || 'free', {
    volume_month_usd_cents: Number(volume_month_usd_cents) || 0,
    txs_month: Number(txs_month) || 0,
  });
  return {
    amount_cents: Number(amount_cents) || 0,
    ...fees,
    demo: true,
  };
}

/**
 * Anything→anything settle only when hasValue + path + fees ok; else refuse.
 * An agent wallet may settle once, inside its 15-minute session.
 */
export async function settle({
  from_wallet,
  to_wallet,
  amount_cents,
  tier = 'free',
  has_value = true,
  path_exists = true,
}) {
  await delay(360);
  const amount = Number(amount_cents) || 0;

  if (!from_wallet?.trim() || !to_wallet?.trim()) {
    return { ok: false, refused: true, reason: 'Need a from wallet and a to wallet.', demo: true };
  }
  if (from_wallet.trim() === to_wallet.trim()) {
    return { ok: false, refused: true, reason: 'From and to can’t be the same wallet.', demo: true };
  }
  const from = store.wallets.find((w) => w.id === from_wallet.trim());
  if (from?.kind === 'agent') {
    if (!from.session_expires_at || Date.parse(from.session_expires_at) <= Date.now()) {
      return { ok: false, refused: true, reason: 'Agent session expired. Mint a new 15-minute wallet.', demo: true };
    }
    if (!from.warrant || from.warrant.calls_used >= from.warrant.max_calls) {
      return { ok: false, refused: true, reason: 'Agent warrant has no settle calls left.', demo: true };
    }
  }
  if (amount <= 0) {
    return { ok: false, refused: true, reason: 'Amount has to be greater than zero.', demo: true, hasValue: false };
  }
  if (!has_value) {
    return {
      ok: false,
      refused: true,
      reason: 'No value on this path — settle needs hasValue.',
      demo: true,
      hasValue: false,
    };
  }
  if (!path_exists) {
    return {
      ok: false,
      refused: true,
      reason: 'No settle path between these wallets yet.',
      demo: true,
      path: false,
    };
  }

  const fees = calculateFees(amount, tier || 'free', {});
  const total_cents = amount + fees.platform_fee_cents + fees.trading_fee_cents;

  const bal = store[`bal_${from_wallet}`];
  if (bal && bal.balance_cents < total_cents) {
    return {
      ok: false,
      refused: true,
      reason: 'Not enough balance to cover amount plus fees.',
      demo: true,
      fees,
    };
  }

  if (from?.kind === 'agent') from.warrant.calls_used += 1;
  if (bal) bal.balance_cents -= total_cents;
  const toBal = store[`bal_${to_wallet}`];
  if (toBal) toBal.balance_cents += amount;

  return {
    ok: true,
    settlement_id: uid('stl'),
    from_wallet,
    to_wallet,
    amount_cents: amount,
    kind: from?.kind || 'personal',
    holds_long_term_key: from ? from.holds_long_term_key : true,
    ...fees,
    total_cents,
    status: 'demo_settled',
    demo: true,
  };
}

export function listWallets() {
  return [...store.wallets];
}
