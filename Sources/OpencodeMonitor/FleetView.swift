import SwiftUI
import WebKit
import AppKit
import Carbon.HIToolbox

/// Where Fleet lives, e.g. `https://<head-node>.<tailnet>.ts.net:<port>` (tailscale serve
/// in front of fleet.py). Not in the source -- the repo is public -- but in UserDefaults:
/// typed into the Fleet window's setup card, or
/// `defaults write com.oesterwitz.starfleetcommand fleetURL https://…`.
/// HTTPS matters: the page only gets the microphone (Computer's voice input) in a
/// secure context. A plain-HTTP address still loads (NSAllowsArbitraryLoadsInWebContent).
let fleetURLKey = "fleetURL"

extension Notification.Name {
    static let fleetReload = Notification.Name("fleetReload")
    static let fleetVoice = Notification.Name("fleetVoice")
}

/// The Fleet dashboard window: llama-swap model pinning + per-node resources.
/// Lives in the same app/menu-bar-extra as the session monitor, opened on
/// demand from ContentView's "Open Fleet" button (or ⌃⌥C), not shown at launch.
struct FleetDashboardView: View {
    @AppStorage(fleetURLKey) private var address = ""
    @State private var draft = UserDefaults.standard.string(forKey: fleetURLKey) ?? ""
    @State private var webView: WKWebView = {
        // Off by default in WKWebView: without it the page's Full screen button
        // (requestFullscreen) is silently ignored. Needs macOS 12.3+.
        let config = WKWebViewConfiguration()
        config.preferences.isElementFullscreenEnabled = true
        let view = WKWebView(frame: .zero, configuration: config)
        view.customUserAgent = "Starfleet Command/1.0 (macOS)"
        return view
    }()
    @State private var loadError: String?

    private var fleetURL: URL? {
        URL(string: address).flatMap { $0.host == nil ? nil : $0 }
    }

    var body: some View {
        ZStack {
            FleetWebView(webView: webView, url: fleetURL, loadError: $loadError)
                .ignoresSafeArea()

            if fleetURL == nil || loadError != nil {
                VStack(spacing: 12) {
                    Image(systemName: fleetURL == nil ? "link" : "wifi.exclamationmark")
                        .font(.system(size: 36))
                        .foregroundStyle(.secondary)
                    Text(fleetURL == nil ? "Where is Fleet?" : "Can't reach Fleet")
                        .font(.headline)
                    if let loadError {
                        Text(loadError)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                        Text("Check that this Mac is connected to Tailscale and the head node is up.")
                            .font(.caption2)
                            .foregroundStyle(.tertiary)
                    }
                    TextField("https://<head-node>.<tailnet>.ts.net:<port>", text: $draft)
                        .textFieldStyle(.roundedBorder)
                        .onSubmit(connect)
                    // No ⌘R here: that is the menu's "Reload Fleet", which ignores the field.
                    Button(fleetURL == nil ? "Connect" : "Retry", action: connect)
                }
                .padding(24)
                .frame(maxWidth: 420)
                .background(.regularMaterial)
                .clipShape(RoundedRectangle(cornerRadius: 12))
            }
        }
        .overlay(alignment: .top) {
            WindowDragStrip()
                .frame(height: 52)
                .ignoresSafeArea()
        }
        .background(FleetWindowChrome())
        // With the scene's .principal role (App.swift), so the window may go full screen.
        .windowFullScreenBehavior(.enabled)
        .onReceive(NotificationCenter.default.publisher(for: .fleetReload)) { _ in
            load()
        }
        .onReceive(NotificationCenter.default.publisher(for: .fleetVoice)) { _ in
            // Still loading: the navigation delegate starts voice once the page is there.
            if !webView.isLoading, webView.url != nil { ComputerVoice.start(in: webView) }
        }
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
}

/// The "Computer" view's voice input, started from outside the page (the ⌃⌥C hotkey).
/// Does what fleet.html's own in-page hotkey does (default Ctrl+Shift+Space): open
/// Computer, focus its input, toggle the microphone -- through the page's globals
/// `compOn`, `setView` and `compMic`. Rename those in fleet.html and this goes quiet.
enum ComputerVoice {
    /// Set by the hotkey, cleared once the page has been told -- survives the window
    /// (and its web view) being created from scratch in between.
    static var pending = false

    static func start(in webView: WKWebView) {
        pending = false
        webView.evaluateJavaScript("""
            if (typeof compMic === 'function') {
              if (!compOn) setView('comp');
              document.getElementById('comptext').focus();
              compMic();
            }
            """)
    }
}

/// ⌃⌥C from anywhere: Fleet window to the front, Computer view, voice input on.
/// Carbon's RegisterEventHotKey because it needs no Accessibility permission
/// (an NSEvent global monitor does).
enum ComputerHotKey {
    /// The scene's openWindow, handed over by HotKeyBridge -- the Carbon callback
    /// lives outside any view, and only views get SwiftUI's environment.
    static var openWindow: OpenWindowAction?
    private static var ref: EventHotKeyRef?

    static func register() {
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard),
                                 eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            DispatchQueue.main.async { ComputerHotKey.fire() }
            return noErr
        }, 1, &spec, nil, nil)
        let status = RegisterEventHotKey(UInt32(kVK_ANSI_C), UInt32(controlKey | optionKey),
                                         EventHotKeyID(signature: OSType(0x5346_4C54), id: 1), // 'SFLT'
                                         GetApplicationEventTarget(), 0, &ref)
        // Typically another app already holds ⌃⌥C. Console.app shows this line.
        if status != noErr { NSLog("Starfleet Command: ⌃⌥C hotkey not registered (OSStatus \(status))") }
    }

    static func fire() {
        ComputerVoice.pending = true
        if let openWindow { openWindow(id: "fleet") }
        NSApp.activate(ignoringOtherApps: true)
        NotificationCenter.default.post(name: .fleetVoice, object: nil)
    }
}

/// Hands the scene's openWindow to ComputerHotKey. Sits behind the menu bar label,
/// the one view that exists from launch on.
struct HotKeyBridge: View {
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Color.clear.onAppear { ComputerHotKey.openWindow = openWindow }
    }
}

/// A transparent grab handle along the top of the window.
///
/// The Fleet window is `.hiddenTitleBar` with a full-bleed WKWebView over the
/// titlebar area (`.ignoresSafeArea()`), which left it with no draggable
/// surface at all: there's no title bar to grab, and `isMovableByWindowBackground`
/// (set in FleetWindowChrome) only fires for mouse-downs that reach the window
/// background -- a web view consumes every one of them first.
///
/// 52pt tall so it lands entirely in the empty upper half of the page's 96px
/// LCARS header, whose title and clock are `align-items:flex-end` bottom-aligned
/// -- nothing in the page needs clicks up there. The traffic lights sit in the
/// window's own title bar, which stays above the content view, so they keep
/// working through this.
struct WindowDragStrip: NSViewRepresentable {
    final class DragView: NSView {
        override var mouseDownCanMoveWindow: Bool { true }
    }

    func makeNSView(context: Context) -> NSView { DragView() }
    func updateNSView(_ nsView: NSView, context: Context) {}
}

/// Two jobs: (1) strip the title bar down to just the traffic lights, floating
/// over the page's own black background, so the LCARS header is the only
/// header; (2) this app is normally .accessory (no Dock icon, no Cmd+Tab
/// entry -- see App.swift), but the Fleet window is a real, primary-feeling
/// window someone will want to alt-tab back to, so flip to .regular for as
/// long as it's open and back to .accessory the moment it closes.
private struct FleetWindowChrome: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = NSView(frame: .zero)
        DispatchQueue.main.async {
            guard let window = view.window else { return }
            window.titlebarAppearsTransparent = true
            window.titleVisibility = .hidden
            window.styleMask.insert(.fullSizeContentView)
            window.isMovableByWindowBackground = true
            window.backgroundColor = .black
            // Green button = a full-screen Space of its own, scaled to the display. The scene's
            // .principal role (App.swift) does it in SwiftUI; this is the same opt-in in AppKit,
            // which Apple recommends for primary windows. The full-screen flags exclude each
            // other, so the auxiliary/none ones go first.
            window.collectionBehavior.remove([.fullScreenAuxiliary, .fullScreenNone])
            window.collectionBehavior.insert(.fullScreenPrimary)

            NSApp.setActivationPolicy(.regular)
            NSApp.activate(ignoringOtherApps: true)
            NotificationCenter.default.addObserver(
                forName: NSWindow.willCloseNotification, object: window, queue: .main
            ) { _ in
                NSApp.setActivationPolicy(.accessory)
            }
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

private struct FleetWebView: NSViewRepresentable {
    let webView: WKWebView
    let url: URL?
    @Binding var loadError: String?

    func makeNSView(context: Context) -> WKWebView {
        webView.navigationDelegate = context.coordinator
        webView.uiDelegate = context.coordinator
        if let url { webView.load(URLRequest(url: url)) }
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {
        context.coordinator.parent = self
    }

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    final class Coordinator: NSObject, WKNavigationDelegate, WKUIDelegate {
        var parent: FleetWebView
        init(_ parent: FleetWebView) { self.parent = parent }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            parent.loadError = nil
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            if ComputerVoice.pending { ComputerVoice.start(in: webView) }
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            show(error)
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            show(error)
        }

        private func show(_ error: Error) {
            // A reload started while the previous load was still running cancels it -- not a failure.
            if (error as NSError).code == NSURLErrorCancelled { return }
            parent.loadError = error.localizedDescription
        }

        // The microphone for Computer's voice input -- Fleet's own origin only, never
        // a page it links to. macOS asks once (NSMicrophoneUsageDescription in Info.plist).
        func webView(_ webView: WKWebView, requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                     initiatedByFrame frame: WKFrameInfo, type: WKMediaCaptureType,
                     decisionHandler: @escaping (WKPermissionDecision) -> Void) {
            decisionHandler(type == .microphone && origin.host == parent.url?.host ? .grant : .deny)
        }

        // Without these WKWebView drops alert() and answers every confirm() with false,
        // so the page's "load over a resident model?" and "cancel loading?" never went through.
        func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping () -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            present(alert, in: webView) { _ in completionHandler() }
        }

        func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                     initiatedByFrame frame: WKFrameInfo, completionHandler: @escaping (Bool) -> Void) {
            let alert = NSAlert()
            alert.messageText = message
            alert.addButton(withTitle: "OK")
            alert.addButton(withTitle: "Cancel")
            present(alert, in: webView) { completionHandler($0 == .alertFirstButtonReturn) }
        }

        private func present(_ alert: NSAlert, in webView: WKWebView,
                             _ done: @escaping (NSApplication.ModalResponse) -> Void) {
            if let window = webView.window {
                alert.beginSheetModal(for: window, completionHandler: done)
            } else {
                done(alert.runModal())
            }
        }
    }
}
