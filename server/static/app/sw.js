// AwLPay app shell — cache the shell, always go live for API.
const SHELL = ['/app/', '/app/index.html', '/app/style.css', '/app/app.js',
  '/app/manifest.json', '/app/icons/icon-192.png'];
self.addEventListener('install', e => {
  e.waitUntil(caches.open('awlpay-v1').then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener('fetch', e => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith('/api/')) return; // never cache API
  e.respondWith(caches.match(e.request).then(r => r || fetch(e.request)));
});
