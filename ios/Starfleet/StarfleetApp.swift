import SwiftUI
import WebKit

/// Starfleet Command on the iPhone: Fleet's page in a web view, nothing more.
/// A thin client on purpose -- no keys in the app. The Starfleet key is typed into
/// the page and kept in the page's localStorage (the web view's persistent store).
/// Fleet serves a phone its phone interface (padd.html); that page talks to the app
/// through `PaddBridge` for what a web page cannot do: haptics, drawing under the
/// status bar, opening a link in Safari, and for messages: notification permission, the push
/// token, taps, the icon badge (Push.swift).
@main
struct StarfleetApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

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
    /// Set by the page (PaddBridge "ready"): the phone interface lays itself out around the
    /// notch and the home indicator, the desktop page knows nothing about them.
    @State private var edgeToEdge = false
    @Environment(\.scenePhase) private var scenePhase

    static let bridge = PaddBridge()

    private var fleetURL: URL? {
        URL(string: address).flatMap { $0.scheme == "https" && $0.host != nil ? $0 : nil }
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            // Inside the safe area unless the page asked for the whole screen. Edge to edge also
            // leaves the keyboard to the page: it sizes itself from the visual viewport.
            FleetWebView(webView: webView, url: fleetURL, loadError: $loadError, edgeToEdge: $edgeToEdge)
                .ignoresSafeArea(edgeToEdge ? .all : [], edges: edgeToEdge ? .all : [])
            if fleetURL == nil || loadError != nil { card }
        }
        .preferredColorScheme(.dark)
        .onAppear { FleetScreen.bridge.onReady = { edgeToEdge = $0 } }
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
        config.applicationNameForUserAgent = "StarfleetCommand/1.1"
        config.userContentController.add(bridge, name: "padd")
        let view = WKWebView(frame: .zero, configuration: config)
        view.allowsBackForwardNavigationGestures = true         // swipe back through the page's own screens
        view.allowsLinkPreview = false
        view.scrollView.contentInsetAdjustmentBehavior = .never // the page reads env(safe-area-inset-*) itself
        // Black, not white, before the first paint.
        view.isOpaque = false
        view.backgroundColor = .clear
        view.scrollView.backgroundColor = .clear
        view.scrollView.alwaysBounceVertical = true             // pull-to-reload even on a page that fits
        return view
    }
}

/// What the phone interface asks of the app, as `webkit.messageHandlers.padd.postMessage({type, ...})`.
/// Only Fleet's own page, in the main frame, is listened to.
final class PaddBridge: NSObject, WKScriptMessageHandler {
    var onReady: ((Bool) -> Void)?
    private(set) var readyCount = 0

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        let fleetHost = URL(string: UserDefaults.standard.string(forKey: "fleetURL") ?? "")?.host
        guard message.frameInfo.isMainFrame, fleetHost != nil, message.frameInfo.securityOrigin.host == fleetHost,
              let body = message.body as? [String: Any], let type = body["type"] as? String else { return }
        switch type {
        case "ready":
            readyCount += 1
            onReady?(body["edgeToEdge"] as? Bool ?? false)
            Task { @MainActor in PaddPush.shared.pageReady() }
        case "push-status", "push-enable", "push-disable", "badge":
            let n = (body["n"] as? NSNumber)?.intValue ?? 0
            Task { @MainActor in PaddPush.shared.handle(type, n) }
        case "haptic":
            haptic(body["style"] as? String ?? "light")
        case "open":
            if let text = body["url"] as? String, let url = URL(string: text), url.scheme == "https" || url.scheme == "http" {
                UIApplication.shared.open(url)
            }
        default:
            break
        }
    }

    private func haptic(_ style: String) {
        switch style {
        case "success": UINotificationFeedbackGenerator().notificationOccurred(.success)
        case "warning": UINotificationFeedbackGenerator().notificationOccurred(.warning)
        case "error": UINotificationFeedbackGenerator().notificationOccurred(.error)
        case "select": UISelectionFeedbackGenerator().selectionChanged()
        case "medium": UIImpactFeedbackGenerator(style: .medium).impactOccurred()
        case "heavy": UIImpactFeedbackGenerator(style: .heavy).impactOccurred()
        default: UIImpactFeedbackGenerator(style: .light).impactOccurred()
        }
    }
}

private struct FleetWebView: UIViewRepresentable {
    let webView: WKWebView
    let url: URL?
    @Binding var loadError: String?
    @Binding var edgeToEdge: Bool

    func makeUIView(context: Context) -> WKWebView {
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        // Here, not in makeWebView: that runs again whenever FleetScreen is rebuilt and only the first view is kept.
        Task { @MainActor in PaddPush.shared.webView = webView }
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
        private var readyAtCommit = 0
        init(_ parent: FleetWebView) { self.parent = parent }

        /// Pull to refresh. The phone interface refreshes its data in place (`PADD.refresh()`);
        /// any other page, or none, gets Fleet's address loaded again -- not `webView.reload()`:
        /// after a failed first load there is no page to reload.
        func reload(_ webView: WKWebView) {
            guard let url = parent.url else { return done(webView) }
            let script = "(window.PADD && typeof PADD.refresh === 'function') ? (PADD.refresh(), true) : false"
            webView.evaluateJavaScript(script) { result, _ in
                if (result as? Bool) == true {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.7) { self.done(webView) }
                } else {
                    webView.load(URLRequest(url: url))
                }
            }
        }

        private func done(_ webView: WKWebView) {
            webView.scrollView.refreshControl?.endRefreshing()
        }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            parent.loadError = nil
            // A tapped notification now waits for the new page's "ready".
            Task { @MainActor in PaddPush.shared.pageLoading() }
        }

        func webView(_ webView: WKWebView, didCommit navigation: WKNavigation!) {
            readyAtCommit = FleetScreen.bridge.readyCount
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            done(webView)
            // A page that did not announce itself (the desktop page) goes back inside the safe area.
            if FleetScreen.bridge.readyCount == readyAtCommit { parent.edgeToEdge = false }
        }

        /// iOS can end the page's process while the app is in the background: bring the page back.
        func webViewWebContentProcessDidTerminate(_ webView: WKWebView) {
            // The old page is gone: a tapped notification waits for the new page's "ready".
            Task { @MainActor in PaddPush.shared.pageLoading() }
            if let url = parent.url { webView.load(URLRequest(url: url)) }
        }

        /// A tapped link that leaves Fleet (another host or port, or one meant for a new window,
        /// like every link in a chat answer) opens in Safari instead of replacing the interface.
        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction,
                     decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
            if navigationAction.navigationType == .linkActivated, let link = navigationAction.request.url,
               link.scheme == "https" || link.scheme == "http",
               navigationAction.targetFrame == nil || link.host != parent.url?.host || link.port != parent.url?.port {
                UIApplication.shared.open(link)
                decisionHandler(.cancel)
                return
            }
            decisionHandler(.allow)
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
