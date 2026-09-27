import { calculateFees, TIER_COPY } from './fees.js';
import {
  createWallet,
  getBalance,
  getQuote,
  settle,
  listWallets,
} from './stub-api.js';

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function centsToUsd(cents) {
  const n = Number(cents) || 0;
  return (n / 100).toLocaleString('en-US', { style: 'currency', currency: 'USD' });
}

function setStatus(el, msg, kind = 'info') {
  if (!el) return;
  el.textContent = msg;
  el.dataset.kind = kind;
  el.hidden = !msg;
}

function refreshWalletSelects() {
  const wallets = listWallets();
  $$('[data-wallet-select]').forEach((sel) => {
    const prev = sel.value;
    const placeholder = sel.dataset.placeholder || 'Pick a wallet';
    sel.innerHTML = `<option value="">${placeholder}</option>`;
    wallets.forEach((w) => {
      const opt = document.createElement('option');
      opt.value = w.id;
      opt.textContent = `${w.id} · ${w.chain}`;
      sel.appendChild(opt);
    });
    if (prev && [...sel.options].some((o) => o.value === prev)) sel.value = prev;
  });
}

function renderWalletList() {
  const list = $('#wallet-list');
  const wallets = listWallets();
  if (!wallets.length) {
    list.innerHTML = `<p class="empty">No wallets yet. Create one above — demo only, not live money.</p>`;
    return;
  }
  list.innerHTML = wallets
    .map(
      (w) => `
    <article class="wallet-card" data-id="${w.id}">
      <div class="wallet-card__top">
        <span class="chain-pill" data-chain="${w.chain}">${w.chain}</span>
        <code class="mono">${w.id}</code>
      </div>
      <p class="muted small">Owner: ${escapeHtml(w.owner_id)}</p>
      <p class="muted small mono trunc">${escapeHtml(w.address)}</p>
      <button type="button" class="btn btn--ghost btn--sm" data-check-balance="${w.id}">Check balance</button>
      <pre class="result" hidden data-balance-out="${w.id}"></pre>
    </article>`,
    )
    .join('');
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showJson(el, data) {
  el.hidden = false;
  el.textContent = JSON.stringify(data, null, 2);
}

function initTabs() {
  $$('[data-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      $$('[data-tab]').forEach((b) => {
        b.classList.toggle('is-active', b === btn);
        b.setAttribute('aria-selected', b === btn ? 'true' : 'false');
      });
      $$('[data-panel]').forEach((p) => {
        p.hidden = p.dataset.panel !== btn.dataset.tab;
      });
    });
  });
}

function initCreateWallet() {
  const form = $('#form-create-wallet');
  const status = $('#create-status');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const btn = form.querySelector('[type="submit"]');
    btn.disabled = true;
    setStatus(status, 'Creating wallet…');
    try {
      const res = await createWallet({
        owner_id: fd.get('owner_id'),
        chain: fd.get('chain'),
      });
      if (res.error) {
        setStatus(status, res.error, 'error');
      } else {
        setStatus(status, `Created ${res.id} on ${res.chain}. Demo stub — not live.`, 'ok');
        form.reset();
        renderWalletList();
        refreshWalletSelects();
      }
    } catch (err) {
      setStatus(status, err.message || 'Something went wrong.', 'error');
    } finally {
      btn.disabled = false;
    }
  });

  $('#wallet-list').addEventListener('click', async (e) => {
    const btn = e.target.closest('[data-check-balance]');
    if (!btn) return;
    const id = btn.dataset.checkBalance;
    const out = document.querySelector(`[data-balance-out="${id}"]`);
    btn.disabled = true;
    try {
      const bal = await getBalance(id);
      showJson(out, bal);
    } finally {
      btn.disabled = false;
    }
  });
}

function initQuote() {
  const form = $('#form-quote');
  const out = $('#quote-result');
  const live = $('#quote-live');

  function updateLive() {
    const amountUsd = Number($('#quote-amount').value) || 0;
    const amount_cents = Math.round(amountUsd * 100);
    const tier = $('#quote-tier').value;
    const vol = Math.round((Number($('#quote-vol').value) || 0) * 100);
    const txs = Number($('#quote-txs').value) || 0;
    const fees = calculateFees(amount_cents, tier, {
      volume_month_usd_cents: vol,
      txs_month: txs,
    });
    const copy = TIER_COPY[tier];
    live.innerHTML = `
      <div class="quote-grid">
        <div><span class="label">Amount</span><strong>${centsToUsd(amount_cents)}</strong></div>
        <div><span class="label">Platform fee</span><strong>${centsToUsd(fees.platform_fee_cents)}</strong></div>
        <div><span class="label">Trading fee</span><strong>${centsToUsd(fees.trading_fee_cents)}</strong></div>
        <div><span class="label">Tier applied</span><strong><code>${fees.tier_applied}</code></strong></div>
      </div>
      <p class="muted small">${copy.label} · ${copy.price} · ${copy.fee}</p>
    `;
  }

  ['quote-amount', 'quote-tier', 'quote-vol', 'quote-txs'].forEach((id) => {
    $(`#${id}`).addEventListener('input', updateLive);
    $(`#${id}`).addEventListener('change', updateLive);
  });
  updateLive();

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const amountUsd = Number($('#quote-amount').value) || 0;
    const btn = form.querySelector('[type="submit"]');
    btn.disabled = true;
    try {
      const res = await getQuote({
        amount_cents: Math.round(amountUsd * 100),
        tier: $('#quote-tier').value,
        volume_month_usd_cents: Math.round((Number($('#quote-vol').value) || 0) * 100),
        txs_month: Number($('#quote-txs').value) || 0,
      });
      showJson(out, res);
    } finally {
      btn.disabled = false;
    }
  });
}

function initSettle() {
  const form = $('#form-settle');
  const status = $('#settle-status');
  const out = $('#settle-result');

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const fd = new FormData(form);
    const amountUsd = Number(fd.get('amount')) || 0;
    const btn = form.querySelector('[type="submit"]');
    btn.disabled = true;
    setStatus(status, 'Checking path and fees…');
    out.hidden = true;
    try {
      const res = await settle({
        from_wallet: fd.get('from_wallet'),
        to_wallet: fd.get('to_wallet'),
        amount_cents: Math.round(amountUsd * 100),
        tier: fd.get('tier'),
        has_value: fd.get('has_value') === 'on',
        path_exists: fd.get('path_exists') === 'on',
      });
      showJson(out, res);
      if (res.refused) {
        setStatus(status, res.reason || 'Settle refused.', 'error');
      } else if (res.ok) {
        setStatus(
          status,
          `Settled ${centsToUsd(res.amount_cents)} (demo). Fee ${centsToUsd(res.platform_fee_cents)} · ${res.tier_applied}.`,
          'ok',
        );
        renderWalletList();
      }
    } catch (err) {
      setStatus(status, err.message || 'Settle failed.', 'error');
    } finally {
      btn.disabled = false;
    }
  });
}

function initBalanceLookup() {
  const form = $('#form-balance');
  const out = $('#balance-result');
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = new FormData(form).get('wallet_id');
    const btn = form.querySelector('[type="submit"]');
    btn.disabled = true;
    try {
      const res = await getBalance(String(id || ''));
      showJson(out, res);
    } finally {
      btn.disabled = false;
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  initTabs();
  initCreateWallet();
  initBalanceLookup();
  initQuote();
  initSettle();
  renderWalletList();
  refreshWalletSelects();
});
