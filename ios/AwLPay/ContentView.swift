import SwiftUI
import WebKit

/// Sideload / TestFlight face for the existing web/ UI.
/// Demo / stubs only. Not live money.
struct ContentView: View {
  var body: some View {
    AwLPayWebView(url: URL(string: "https://www.slidphilabs.com/awlpay/app")!)
      .ignoresSafeArea()
  }
}

struct AwLPayWebView: UIViewRepresentable {
  let url: URL

  func makeUIView(context: Context) -> WKWebView {
    let config = WKWebViewConfiguration()
    config.defaultWebpagePreferences.allowsContentJavaScript = true
    let view = WKWebView(frame: .zero, configuration: config)
    view.scrollView.bounces = true
    view.isOpaque = false
    view.backgroundColor = UIColor(red: 0.03, green: 0.04, blue: 0.05, alpha: 1)
    view.load(URLRequest(url: url))
    return view
  }

  func updateUIView(_ uiView: WKWebView, context: Context) {}
}
