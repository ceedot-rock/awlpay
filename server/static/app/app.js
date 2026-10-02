// AwLPay app v1 — talks to the live API on this same host.
const $ = id => document.getElementById(id);
const cents = d => '$' + (d / 100).toFixed(2);

// tabs
document.querySelectorAll('nav button').forEach(b => b.onclick = () => {
  document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  $('tab-' + b.dataset.tab).classList.add('active');
});

// live status
fetch('/healthz').then(r => r.json()).then(j => {
  if (j.ok) $('live-dot').classList.add('ok');
}).catch(() => {});

function copyText(t, btn) {
  navigator.clipboard.writeText(t).then(() => {
    const o = btn.textContent; btn.textContent = 'copied';
    setTimeout(() => btn.textContent = o, 1200);
  });
}
window.copyText = copyText;

// ---- QUOTE ----
$('q-go').onclick = async () => {
  const out = $('q-out'); out.innerHTML = '…';
  const body = {
    amount_cents: Math.round(parseFloat($('q-amount').value || '0') * 100),
    from_chain: $('q-from-chain').value, from_token: $('q-from-token').value,
    to_chain: $('q-to-chain').value, to_token: $('q-to-token').value,
  };
  try {
    const r = await fetch('/api/pay/quote', {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json();
    if (j.refused) { out.innerHTML = `<p class="err">refused: ${j.reason}</p>`; return; }
    const f = j.fees;
    out.innerHTML = `<div class="card">
      <div class="kv"><span>You send</span><b>${cents(j.amount_cents)}</b></div>
      <div class="kv"><span>Free tier fee</span><span>${cents(f.free.fee_cents)}</span></div>
      <div class="kv"><span>You receive (free)</span><b class="ok-t">${cents(f.free.net_cents)}</b></div>
      <div class="kv"><span>Pro / L33t fee</span><span>${cents(f.pro.fee_cents)}</span></div>
      <div class="kv"><span>Route</span><span>${(j.path || []).map(p => p.chain + '/' + p.token).join(' → ')}</span></div>
    </div>`;
  } catch (e) { out.innerHTML = `<p class="err">quote failed: ${e.message}</p>`; }
};

// ---- PAY (x402) ----
let lastAccepts = null;
$('p-step1').onclick = async () => {
  const box = $('p-accepts'); box.innerHTML = '…'; $('p-out').innerHTML = '';
  $('p-step2wrap').hidden = true;
  const body = { amount_cents: Math.round(parseFloat($('p-amount').value || '0') * 100),
    from_chain: 'base', from_token: 'USDC', to_chain: 'base', to_token: 'USDC' };
  const r = await fetch('/api/pay/execute', {method: 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  if (r.status !== 402) {
    box.innerHTML = `<p class="err">unexpected status ${r.status}</p>`; return;
  }
  const j = await r.json();
  lastAccepts = j.accepts || [];
  const a = lastAccepts[0];
  if (!a) { box.innerHTML = '<p class="err">no payment rails offered</p>'; return; }
  const units = parseInt(a.amount, 10) / 1e6;
  box.innerHTML = `<div class="card">
    <div class="kv"><span>Pay</span><b>${units} ${a.asset === a.network ? a.asset : 'USDC'}</b></div>
    <div class="kv"><span>Network</span><span class="mono">${a.network}</span></div>
    <div class="kv"><span>To</span><span class="mono">${a.payTo}<button class="copy" onclick="copyText('${a.payTo}',this)">copy</button></span></div>
    <p class="hint">1) Transfer the amount above to the address.<br>
    2) Sign this exact text with the paying wallet (EIP-191 personal_sign):<br>
    <span class="mono">awlpay payment proof<br>txHash: &lt;your 0x txhash, lowercase&gt;<br>resource: ${a.resource}</span></p>
    <p class="hint">${lastAccepts.length} rails offered — showing the first.</p>
  </div>`;
  $('p-step2wrap').hidden = false;
  $('p-step2wrap').dataset.resource = a.resource;
};

$('p-step2').onclick = async () => {
  const out = $('p-out'); out.innerHTML = 'submitting…';
  const resource = $('p-step2wrap').dataset.resource;
  const proof = { x402Version: 2, scheme: 'exact', network: lastAccepts[0].network,
    payload: { txHash: $('p-txhash').value.trim(), payerSig: $('p-sig').value.trim() } };
  const xp = btoa(JSON.stringify(proof)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  try {
    const body = { amount_cents: Math.round(parseFloat($('p-amount').value || '0') * 100),
      from_chain: 'base', from_token: 'USDC', to_chain: 'base', to_token: 'USDC' };
    const r = await fetch('/api/pay/execute', {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-PAYMENT': xp},
      body: JSON.stringify(body)});
    const j = await r.json();
    out.innerHTML = `<div class="card"><b class="${r.ok ? 'ok-t' : ''}">
      ${r.ok ? 'Paid ✓' : 'Not accepted (' + r.status + ')'}</b>
      <pre class="mono">${JSON.stringify(j, null, 1).slice(0, 600)}</pre></div>`;
  } catch (e) { out.innerHTML = `<p class="err">submit failed: ${e.message}</p>`; }
};
