import SwiftUI
import UserNotifications

@main
struct StarfleetCommandApp: App {
    @StateObject private var jeanLuc: StatusPoller
    @StateObject private var kathryn: StatusPoller
    @StateObject private var widgetController: DesktopWidgetController
    @StateObject private var panelController: PanelController

    init() {
        NSApplication.shared.setActivationPolicy(.accessory)
        // Note: needs a real .app bundle (see package.sh) to reliably prompt/deliver --
        // bare `swift run` often can't register for notifications. Requested once here
        // (not per-StatusPoller) now that there are two of them.
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
        let jeanLuc = StatusPoller(host: "jean-luc", label: "Jean-Luc")
        let kathryn = StatusPoller(host: "kathryn", label: "Kathryn")
        _jeanLuc = StateObject(wrappedValue: jeanLuc)
        _kathryn = StateObject(wrappedValue: kathryn)
        _widgetController = StateObject(wrappedValue: DesktopWidgetController(jeanLuc: jeanLuc, kathryn: kathryn))
        _panelController = StateObject(wrappedValue: PanelController(jeanLuc: jeanLuc, kathryn: kathryn))
    }

    var body: some Scene {
        MenuBarExtra {
            ContentView(jeanLuc: jeanLuc, kathryn: kathryn, widgetController: widgetController,
                        panelController: panelController)
        } label: {
            let anyError = jeanLuc.lastError != nil || kathryn.lastError != nil
            let anyStalled = (jeanLuc.status?.sessions.contains(where: \.stalled) ?? false)
                || (kathryn.status?.sessions.contains(where: \.stalled) ?? false)
            let totalProcessing = (jeanLuc.status?.sessions.processingCount ?? 0)
                + (kathryn.status?.sessions.processingCount ?? 0)
            // Glyph choice, not color: MenuBarExtra strips custom RGB colors from
            // its label (confirmed live -- a colored SF Symbol rendered as plain
            // white), so "something's wrong" has to be conveyed by which symbol
            // shows, not by tinting a status dot red/green.
            HStack(spacing: 4) {
                StarfleetMarkView()
                if anyError {
                    Image(systemName: "questionmark.circle.fill")
                } else if anyStalled {
                    Image(systemName: "exclamationmark.triangle.fill")
                }
                if totalProcessing > 0 {
                    Image(systemName: "bolt.fill")
                    Text("\(totalProcessing)")
                }
            }
            .help("Jean-Luc: \(jeanLuc.lastError ?? "ok") · Kathryn: \(kathryn.lastError ?? "ok")")
        }
        .menuBarExtraStyle(.window)
        .commands {
            CommandGroup(after: .toolbar) {
                Button("Reload Fleet") {
                    NotificationCenter.default.post(name: .fleetReload, object: nil)
                }
                .keyboardShortcut("r", modifiers: .command)
            }
        }

        // Singleton window (not WindowGroup -- there's only ever one Fleet
        // dashboard), opened on demand via ContentView's "Open Fleet" button.
        Window("Fleet", id: "fleet") {
            FleetDashboardView()
                .frame(minWidth: 960, minHeight: 640)
        }
        .windowStyle(.hiddenTitleBar)
    }
}
