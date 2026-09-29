# AwLPay Android

This is the human door onto the existing `web/` wallets / quote / settle UI.
It is **not** a Play Store listing and **not** live money.

## Why not a Kotlin rewrite

`web/index.html` already is the product surface:
Wallets → Quote fees → Settle, with the yellow **Demo / stubs** badge.
Fee law stays in `exact/FeeManager.cuni`. Agents keep the HTTP API.

## Ladder

1. Host `web/` at a real HTTPS origin (`app.awlpay.com` or `awlpay.fly.dev/app`).
2. Point site CTA **Open AwLPay** at that origin. Do not point humans at fly.dev root JSON.
3. PWA: `web/manifest.webmanifest` + `web/sw.js`. Android Chrome → Install app / Add to Home Screen.
4. TWA: wrap the hosted PWA with Bubblewrap when CoS is green.
5. Play Store only after live rails + money-transmitter review. Not tonight.

## PWA locally

```bash
cd web
python3 -m http.server 5173
```

Chrome on Android against that host (or the hosted URL) can install the app.
Keep the Demo / stubs badge.

## TWA later (sideload APK)

```bash
npm i -g @bubblewrap/cli
bubblewrap init --manifest https://HOST/manifest.webmanifest
bubblewrap build
```

Package id: `com.slidphilabs.awlpay`
Display mode: standalone
Theme: `#0b0b0c`
Icons: `brand/awlpay-a-mark-512.png`, `brand/awlpay-app-icon-180.png`

Do not wrap `https://www.slidphilabs.com/awlpay` (marketing prose).
Do not wrap `https://awlpay.fly.dev/` (API catalog JSON).
Wrap only the hosted `web/` UI.
