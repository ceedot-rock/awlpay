function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}
function isIos() {
  return /iphone|ipad|ipod/i.test(window.navigator.userAgent);
}
function isAndroid() {
  return /android/i.test(window.navigator.userAgent);
}

document.addEventListener('DOMContentLoaded', () => {
  const card = document.getElementById('install-card');
  const title = document.getElementById('install-title');
  const body = document.getElementById('install-body');
  const btn = document.getElementById('install-btn');
  if (!card || !title || !body) return;
  if (isStandalone()) {
    card.hidden = true;
    return;
  }
  let deferred;
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferred = e;
    title.textContent = 'Install on Android';
    body.textContent = 'Add AwLPay to the home screen. Testnet wallets only. Not live money.';
    if (btn) {
      btn.hidden = false;
      btn.textContent = 'Install app';
    }
  });
  if (btn) {
    btn.addEventListener('click', async () => {
      if (!deferred) return;
      deferred.prompt();
      await deferred.userChoice;
      deferred = null;
    });
  }
  if (isIos()) {
    title.textContent = 'Add on iPhone';
    body.textContent = 'Safari → Share → Add to Home Screen. Chrome on iOS cannot install it.';
    if (btn) btn.hidden = true;
  } else if (isAndroid()) {
    title.textContent = 'Install on Android';
    body.textContent = 'Chrome menu → Install app / Add to Home screen. Needs this page on HTTPS.';
  } else {
    title.textContent = 'Phone app';
    body.textContent = 'Open this page in Chrome on Android or Safari on iPhone, then add it to the home screen.';
  }
});
