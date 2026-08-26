import SwiftUI
import WebKit
import AppKit

// Fleet's own dashboard (fleet.py on jean-luc) only ever serves plain HTTP,
// bound to the tailnet IP -- see NSAppTransportSecurity exception in Info.plist.
private let fleetURL = URL(string: "http://100.105.20.79:8090")!

extension Notification.Name {
    static let fleetReload = Notification.Name("fleetReload")
}

/// The Fleet dashboard window: llama-swap model pinning + per-node resources.
/// Lives in the same app/menu-bar-extra as the session monitor, opened on
/// demand from ContentView's "Open Fleet" button, not shown at launch.
struct FleetDashboardView: View {
    @State private var webView: WKWebView = {
        let view = WKWebView(frame: .zero)
        view.customUserAgent = "Starfleet Command/1.0 (macOS)"
        return view
    }()
    @State private var loadError: String?

    var body: some View {
        ZStack {
            FleetWebView(webView: webView, url: fleetURL, loadError: $loadError)
                .ignoresSafeArea()

            if let loadError {
                VStack(spacing: 12) {
                    Image(systemName: "wifi.exclamationmark")
                        .font(.system(size: 36))
                        .foregroundStyle(.secondary)
                    Text("Can't reach Fleet")
                        .font(.headline)
                    Text(loadError)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                    Text("Check that this Mac is connected to Tailscale and jean-luc is up.")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    Button("Retry") {
                        webView.load(URLRequest(url: fleetURL))
                    }
                    .keyboardShortcut("r", modifiers: .command)
                }
                .padding(24)
                .frame(maxWidth: 360)
                .background(.regularMaterial)
                .clipShape(RoundedRectangle(cornerRadius: 12))
            }
        }
        .background(FleetWindowChrome())
        .onReceive(NotificationCenter.default.publisher(for: .fleetReload)) { _ in
            webView.load(URLRequest(url: fleetURL))
        }
    }
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
    let url: URL
    @Binding var loadError: String?

    func makeNSView(context: Context) -> WKWebView {
        webView.navigationDelegate = context.coordinator
        webView.load(URLRequest(url: url))
        return webView
    }

    func updateNSView(_ nsView: WKWebView, context: Context) {}

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    final class Coordinator: NSObject, WKNavigationDelegate {
        let parent: FleetWebView
        init(_ parent: FleetWebView) { self.parent = parent }

        func webView(_ webView: WKWebView, didStartProvisionalNavigation navigation: WKNavigation!) {
            parent.loadError = nil
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            parent.loadError = error.localizedDescription
        }

        func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!, withError error: Error) {
            parent.loadError = error.localizedDescription
        }
    }
}
