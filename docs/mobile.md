# AwLPay on Android and Apple

One UI. Two phones. Not two products.

The face is `web/`: Wallets, Quote fees, Settle.
Personal wallet or 15-minute agent wallet. Yellow Demo / stubs badge.

## Install without stores

| Phone | Browser | Action |
|-------|---------|--------|
| Android | Chrome | menu → **Install app** / Add to Home screen |
| iPhone / iPad | Safari | Share → **Add to Home Screen** |

Both need the UI on HTTPS first. `awlpay.fly.dev` root is API JSON. Do not send humans there.

## Native wrappers

| Platform | Path | Bundle |
|----------|------|--------|
| Android | `android/` WebView or Bubblewrap TWA | `com.slidphilabs.awlpay` |
| iPhone | `ios/AwLPay` WKWebView | `com.slidphilabs.awlpay` |

Play Store and App Store wait on CoS + live rails.
