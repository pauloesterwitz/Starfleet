import SwiftUI
import WebKit

/// Starfleet Command on the iPhone: Fleet's page in a web view, nothing more.
/// A thin client on purpose -- no keys in the app. The Starfleet key is typed into
/// the page and kept in the page's localStorage (the web view's persistent store).
@main
struct StarfleetApp: App {
    var body: some Scene {
        WindowGroup { FleetScreen() }
    }
}

/// Fleet's address, e.g. `https://<head-node>.<tailnet>.ts.net:<port>` (tailscale serve in
/// front of fleet.py), is asked for once and kept in UserDefaults -- not in the source,
/// the repo is public. HTTPS only: ATS blocks plain HTTP, and the page gets the
/// microphone only in a secure context.
struct FleetScreen: View {
    @AppStorage("fleetURL") private var address = ""
    @State private var draft = UserDefaults.standard.string(forKey: "fleetURL") ?? ""
    @State private var loadError: String?
    @State private var webView = FleetScreen.makeWebView()
    @Environment(\.scenePhase) private var scenePhase

    private var fleetURL: URL? {
        URL(string: address).flatMap { $0.scheme == "https" && $0.host != nil ? $0 : nil }
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            // Inside the safe area: the LCARS page knows nothing about notches.
            FleetWebView(webView: webView, url: fleetURL, loadError: $loadError)
            if fleetURL == nil || loadError != nil { card }
        }
        .preferredColorScheme(.dark)
        // Typical fix for "Can't reach Fleet": switch to the Tailscale app, connect,
        // come back. Coming back retries by itself.
        .onChange(of: scenePhase) { _, phase in
            if phase == .active, loadError != nil { load() }
        }
    }

    private var card: some View {
        VStack(spacing: 14) {
            Image(systemName: fleetURL == nil ? "link" : "wifi.exclamationmark")
                .font(.system(size: 40))
                .foregroundStyle(.secondary)
            Text(fleetURL == nil ? "Where is Fleet?" : "Can't reach Fleet")
                .font(.headline)
            if let loadError {
                Text(loadError)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                Text("Is Tailscale connected on this iPhone? Turn it on in the Tailscale app, then come back.")
                    .font(.footnote)
                    .foregroundStyle(.tertiary)
            } else {
                Text("Fleet's HTTPS address on your tailnet.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
            }
            TextField("https://<head-node>.<tailnet>.ts.net:<port>", text: $draft)
                .keyboardType(.URL)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .textFieldStyle(.roundedBorder)
                .onSubmit(connect)
            Button(fleetURL == nil ? "Connect" : "Retry", action: connect)
                .buttonStyle(.borderedProminent)
        }
        .multilineTextAlignment(.center)
        .padding(24)
        .frame(maxWidth: 420)
        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 16))
        .padding()
    }

    private func connect() {
        address = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        load()
    }

    private func load() {
        guard let fleetURL else { return }
        loadError = nil
        webView.load(URLRequest(url: fleetURL))
    }

    private static func makeWebView() -> WKWebView {
        let config = WKWebViewConfiguration()
        config.allowsInlineMediaPlayback = true
        config.mediaTypesRequiringUserActionForPlayback = []
        config.preferences.isElementFullscreenEnabled = true    // iPad; the iPhone has no element full screen
        config.applicationNameForUserAgent = "StarfleetCommand/1.0"
        let view = WKWebView(frame: .zero, configuration: config)
        // Black, not white, before the first paint.
        view.isOpaque = false
        view.backgroundColor = .clear
        view.scrollView.backgroundColor = .clear
        view.scrollView.alwaysBounceVertical = true             // pull-to-reload even on a page that fits
        return view
    }
}

private struct FleetWebView: UIViewRepresentable {
    let webView: WKWebView
    let url: URL?
    @Binding var loadError: String?

    func makeUIView(context: Context) -> WKWebView {
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        let refresh = UIRefreshControl()
        refresh.tintColor = .white
        refresh.addAction(UIAction { _ in context.coordinator.reload(webView) }, for: .valueChanged)
        webView.scrollView.refreshControl = refresh
        if let url { webView.load(URLRequest(url: url)) }
        return webView
    }

    func updateUIView(_ uiView: WKWebView, context: Context) {
        context.coordinator.parent = self
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var parent: FleetWebView
        init(_ parent: FleetWebView) { self.parent = parent }

        /// Reloads Fleet's address, not `webView.reload()`: after a failed first load
        /// there is no page to reload.
        func reload(_ webView: WKWebView) {
            guard let url = parent.url else { return done(webView) }
            webView.load(URLRequest(url: url))
        }

        private func done(_ webView: WKWebView) {
            webView.scrollView.refreshControl?.endRefreshing()
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            parent.loadError = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            done(webView)
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            show(error, webView)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            show(error, webView)
        }

        private func show(_ error: Error, _ webView: WKWebView) {
            // A new load started over a running one cancels it -- not a failure.
            if (error as NSError).code == NSURLErrorCancelled { return }
            done(webView)
            parent.loadError = error.localizedDescription
        }

        // The microphone for Computer's voice input -- Fleet's own origin only.
        // iOS asks once (NSMicrophoneUsageDescription, set in the project's build settings).
        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(type == .microphone && origin.host == parent.url?.host ? .grant : .deny)
        }

        // Without these WKWebView drops alert() and answers every confirm() with false.
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            present(message, in: webView, cancel: false) { _ in completionHandler() }
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            present(message, in: webView, cancel: true, completionHandler)
        }

        private func present(_ message: String, in webView: WKWebView, cancel: Bool,
                             _ answer: @escaping (Bool) -> Void) {
            guard var top = webView.window?.rootViewController else { return answer(false) }
            while let next = top.presentedViewController { top = next }
            let alert = UIAlertController(title: nil, message: message, preferredStyle: .alert)
            alert.addAction(UIAlertAction(title: "OK", style: .default) { _ in answer(true) })
            if cancel {
                alert.addAction(UIAlertAction(title: "Cancel", style: .cancel) { _ in answer(false) })
            }
            top.present(alert, animated: true)
        }
    }
}
