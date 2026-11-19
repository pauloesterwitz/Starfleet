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
        ComputerHotKey.register()
        SelfUpdate.shared.run()
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
            .background(HotKeyBridge())
        }
        .menuBarExtraStyle(.window)
        // The menu bar shows these only while the Fleet window is open (the app is a Dock
        // app then); the dropdown has the same Help, About and update entries for the rest.
        .commands {
            CommandGroup(replacing: .appInfo) {
                Button("About Starfleet Command") { AboutPanel.show() }
                CheckForUpdatesButton()
            }
            CommandGroup(after: .toolbar) {
                Button("Reload Fleet") {
                    NotificationCenter.default.post(name: .fleetReload, object: nil)
                }
                .keyboardShortcut("r", modifiers: .command)
            }
            CommandGroup(replacing: .help) {
                HelpButton(title: "Starfleet Command Help")
            }
        }

        // Singleton window (not WindowGroup -- there's only ever one Fleet
        // dashboard), opened on demand via ContentView's "Open Fleet" button.
        Window("Fleet", id: "fleet") {
            FleetDashboardView()
                .frame(minWidth: 960, minHeight: 640)
        }
        .windowStyle(.hiddenTitleBar)
        // The green button's full screen. SwiftUI gives a Window that is not the app's first
        // scene (the MenuBarExtra is) the .associated role: such a window only joins another
        // window's full screen and never gets one of its own. .principal is Apple's override.
        .windowManagerRole(.principal)

        Window("Starfleet Command Help", id: "help") {
            HelpView()
        }
    }
}

/// Opens the Help window, from the Help menu or the dropdown. A view of its own, so it can
/// read the openWindow action from the environment.
struct HelpButton: View {
    var title = "Help"
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        Button(title) {
            openWindow(id: "help")
            NSApp.activate(ignoringOtherApps: true)
        }
        .keyboardShortcut("?", modifiers: .command)
    }
}

/// Updates itself at every start, and on demand from Check for Updates…. package.sh stamps
/// the clone this build came from into the bundle (Resources/repo-path); that clone's
/// update.sh fast-forwards main from origin, rebuilds and relaunches this app when its
/// sources changed, and reinstalls the iPhone app when ios/ changed or its signing profile
/// expired. It logs to
/// ~/Library/Logs/Starfleet Command update.log. A `swift run` build has no stamp: no update.
final class SelfUpdate: ObservableObject {
    static let shared = SelfUpdate()
    static let log = FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Logs/Starfleet Command update.log")

    @Published private(set) var running = false
    /// What a run is doing right now, read from its lines in the log; nil when idle.
    @Published private(set) var phase: String?
    /// What the last run ended with, for the dropdown; nil before the first one ends.
    @Published private(set) var result: String?
    @Published private(set) var failed = false
    private var poll: Timer?

    private let script: URL? = {
        guard Bundle.main.bundlePath.hasSuffix(".app"),
              let stamp = Bundle.main.url(forResource: "repo-path", withExtension: nil),
              let repo = try? String(contentsOf: stamp, encoding: .utf8)
                  .trimmingCharacters(in: .whitespacesAndNewlines)
        else { return nil }
        return URL(fileURLWithPath: repo).appendingPathComponent("update.sh")
    }()

    var available: Bool { script != nil }

    /// One run at a time: a second one would build into the same clone. When it finds a new
    /// Mac build it quits this app and opens the new one, so this process never hears back.
    func run() {
        guard !running, let script else { return }
        let process = Process()
        process.executableURL = script
        process.arguments = [Bundle.main.bundlePath, String(ProcessInfo.processInfo.processIdentifier)]
        process.terminationHandler = { done in
            let status = done.terminationStatus
            DispatchQueue.main.async { self.finished(status) }
        }
        // This run's lines start at the log's current end.
        let start = (try? FileManager.default.attributesOfItem(atPath: Self.log.path)[.size] as? UInt64) ?? 0
        do {
            try process.run()
            running = true
            phase = "Looking for a newer version…"
            let timer = Timer(timeInterval: 1, repeats: true) { [weak self] _ in
                if let now = SelfUpdate.phase(inLogFrom: start), now != self?.phase { self?.phase = now }
            }
            RunLoop.main.add(timer, forMode: .common)
            poll = timer
        } catch {
            finish("Could not start update.sh: \(error.localizedDescription)", failed: true)
        }
    }

    /// update.sh's exit codes (its header has the details); anything it does not name is a
    /// failure, explained in the log.
    private func finished(_ status: Int32) {
        running = false
        poll?.invalidate()
        poll = nil
        phase = nil
        let build = AboutPanel.buildCommit.map { " (build \($0))" } ?? ""
        switch status {
        case 0: finish("Up to date\(build).", failed: false)
        case 3: finish("GitHub not reachable. No update\(build).", failed: true)
        case 4: finish("The clone is not on main, or has commits or changes of its own: update it by hand.", failed: true)
        case 5: finish("Mac app up to date\(build); the iPhone app was not updated: see the log.", failed: true)
        case 6: finish("Another update is already running.", failed: false)
        default: finish("Update failed (exit \(status)): see the log.", failed: true)
        }
    }

    private func finish(_ message: String, failed: Bool) {
        result = message
        self.failed = failed
    }

    /// The step update.sh has reached, from the lines it logged since `offset`.
    private static func phase(inLogFrom offset: UInt64) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: log) else { return nil }
        defer { try? handle.close() }
        try? handle.seek(toOffset: offset)
        let lines = String(decoding: (try? handle.readToEnd()) ?? Data(), as: UTF8.self)
        if let found = lines.range(of: "\nMac: rebuilding ") {
            let commit = lines[found.upperBound...].prefix { !$0.isNewline }
            return "Building \(commit), a few minutes; then the app restarts in the new version."
        }
        if lines.contains("\niPhone: building and installing") { return "Installing on the iPhone…" }
        return nil
    }
}

/// Check for Updates…, in the app menu and the dropdown.
struct CheckForUpdatesButton: View {
    @ObservedObject private var updater = SelfUpdate.shared

    var body: some View {
        Button(updater.running ? "Checking for Updates…" : "Check for Updates…") { updater.run() }
            .disabled(updater.running || !updater.available)
    }
}
