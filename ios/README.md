# AwLPay on iPhone

Same product face as Android: `web/` wallets / quote / settle.
Not an App Store listing. Not live money.

## How a person installs today

1. Host `web/` on HTTPS.
2. Open that URL in **Safari** (Chrome on iOS cannot Add to Home Screen the same way).
3. Share → **Add to Home Screen**.
4. The icon is AwLPay. The yellow Demo / stubs badge stays.

Safari honors `apple-mobile-web-app-capable` and `apple-touch-icon` already wired in `web/index.html`.

## Native wrapper later

`ios/AwLPay/ContentView.swift` is a WKWebView shell that loads the hosted PWA (or bundled `web/` for offline demo).

```
open Xcode → ios/AwLPay.xcodeproj
bundle id: com.slidphilabs.awlpay
min iOS: 16
```

Do not wrap slidphilabs.com/awlpay (marketing prose).
Do not wrap awlpay.fly.dev (API JSON).
Wrap only the hosted `web/` UI.

App Store submission waits on CoS + live rails. Same rule as Play.
