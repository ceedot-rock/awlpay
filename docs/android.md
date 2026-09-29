# Android door

User ask: make AwLPay an app you can download on Android.

## Honest state

- Site `/awlpay` is marketing prose.
- `Open AwLPay` currently hits `https://awlpay.fly.dev`, which returns a JSON catalog, not a wallet.
- The real UI is `web/` in this repo. Local stubs. Not live money. Fly deploy HOLD until CoS green.

## What we added

- `web/manifest.webmanifest` + `web/sw.js` so the demo UI is installable as a PWA.
- `android/README.md` for the TWA / sideload APK path.

## What we did not add

- A Play Store listing.
- Live rails.
- A second native fee engine.
