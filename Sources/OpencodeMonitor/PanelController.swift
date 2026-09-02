import AppKit
import Foundation
import SwiftUI

/// Feeds the external 3.5" USB panel from the same StatusPollers that drive the
/// menu bar and the desktop widget -- deliberately no SSH of its own, so adding
/// the panel costs zero extra polling on either Spark.
///
/// The panel renders a compiled theme stored on the device; all we push are
/// numbers on channels 1...9. The channel map below MUST stay in sync with the
/// theme generator (`build_theme.py`), because the labels next to each value
/// live in the theme, not here -- swapping two channels here would silently
/// mislabel them on screen.
@MainActor
final class PanelController: ObservableObject {
    @Published var isEnabled: Bool {
        didSet {
            UserDefaults.standard.set(isEnabled, forKey: Self.enabledKey)
            if !isEnabled { driver.close() }
        }
    }

    /// Nil until the first push attempt; then true/false for "panel reachable".
    @Published private(set) var isConnected: Bool?

    /// Backlight level, 1...100. The panel does not persist this itself -- it is
    /// re-sent on every push -- so we keep it in UserDefaults like the toggles.
    /// Held as Double for the slider; `level` is what actually goes on the wire.
    @Published var brightness: Double {
        didSet {
            // A slider drag fires this continuously, so only act when the value
            // the device would actually see has changed.
            guard Self.level(brightness) != Self.level(oldValue) else { return }
            UserDefaults.standard.set(brightness, forKey: Self.brightnessKey)
            // Apply straight away rather than waiting for the next 2s tick, so
            // dragging the slider reads as live.
            if isEnabled, !isAsleep, driver.isOpen {
                _ = driver.sendDateTime(brightness: Self.level(brightness),
                                        timeout: Self.blankAfter)
            }
        }
    }

    private static let enabledKey = "usbPanelEnabled"
    private static let brightnessKey = "usbPanelBrightness"
    static let defaultBrightness: Double = 50

    private static func level(_ value: Double) -> UInt8 {
        UInt8(max(1, min(100, Int(value.rounded()))))
    }

    /// Backlight blanking timeout, in EIGHTHS of a second (firmware units, max
    /// 255 ~= 32s; anything under 8 disables blanking entirely).
    ///
    /// This is the panel's own dead-man switch: it blanks when it stops hearing
    /// from us. We push every 2s, so it never trips while the Mac is awake, but
    /// it covers sleep, an app crash, a quit, or the cable being pulled at the
    /// Mac end -- none of which the panel could otherwise detect. We used to
    /// send 0 here, which pinned the backlight on forever.
    private static let blankAfter: UInt8 = 80          // 10 seconds
    /// Sent on the way into sleep so blanking happens promptly rather than
    /// after the full idle timeout. 8 eighths = 1 second, the shortest value
    /// that still counts as "enabled".
    private static let blankOnSleep: UInt8 = 8

    /// Set between willSleep and didWake so the push loop can't relight the
    /// panel if it gets a tick in before the machine actually suspends.
    private var isAsleep = false

    private let jeanLuc: StatusPoller
    private let kathryn: StatusPoller
    private let driver = PanelDriver()

    // Channel map -- mirrors build_theme.py.
    private enum Channel {
        static let jeanLucGPU: UInt8 = 1
        static let jeanLucTemp: UInt8 = 2
        static let jeanLucRAM: UInt8 = 3
        static let jeanLucPower: UInt8 = 4
        static let kathrynGPU: UInt8 = 5
        static let kathrynTemp: UInt8 = 6
        static let kathrynRAM: UInt8 = 7
        static let kathrynPower: UInt8 = 8
        static let sessions: UInt8 = 9
        /// Two session rows, four channels each: status, age, todos done, todos total.
        static let sessionRows: [(status: UInt8, age: UInt8, done: UInt8, total: UInt8)] = [
            (10, 11, 12, 13),
            (14, 15, 16, 17),
        ]
        static let jeanLucModel: UInt8 = 18
        static let kathrynModel: UInt8 = 19
    }

    /// Which model family llama-swap currently has resident on a node, rendered
    /// on the panel as a WORD via the theme's glyph atlas (see PanelStatus).
    ///
    /// A single digit indexes ten atlas cells, so this is deliberately the model
    /// FAMILY, not the member name -- the roster is 26 members across 12
    /// families. 1...4 are the everyday residents, 5...8 the tensor-parallel
    /// ones, 9 catches everything else. Keep in sync with build_theme.py.
    private enum PanelModel: UInt16 {
        case none = 0
        case gemma4 = 1
        case qwen36 = 2
        case qwen38 = 3
        case nemcascade = 4
        case qwen35 = 5
        case qwen235b = 6
        case nemotron = 7
        case minimax = 8
        case other = 9

        /// Matched against llama-swap's member id (e.g. "gemma4-26b-46tps-kathryn",
        /// "qwen3.6-35b-57tps-mtp4-jean-luc"), lowercased. Order matters: the
        /// 235B check must precede the generic "qwen3" families, and gemma4
        /// covers both the 26b and 31b variants.
        init(memberID: String) {
            let id = memberID.lowercased()
            switch true {
            case id.contains("qwen3-235b"), id.contains("qwenvl235"): self = .qwen235b
            case id.contains("qwen3.5"):                              self = .qwen35
            case id.contains("qwen3.6"):                              self = .qwen36
            case id.contains("qwen3.8"):                              self = .qwen38
            case id.contains("gemma4"):                               self = .gemma4
            case id.contains("nemcascade"):                           self = .nemcascade
            case id.contains("nemotron"):                             self = .nemotron
            case id.contains("minimax"):                              self = .minimax
            default:                                                  self = .other
            }
        }
    }

    /// Rendered on the panel as a WORD, not a number: the theme's glyph atlas
    /// for these channels holds word bitmaps instead of digits, so the value is
    /// an index into that atlas. Keep in sync with build_theme.py's word list.
    private enum PanelStatus: UInt16 {
        case none = 0
        case working = 1
        case stalled = 2
        case waiting = 3
        case idle = 4

        init(_ session: SessionStatus) {
            // Order matters: a session can be both stalled and nominally
            // "waiting", and stalled is the more urgent thing to surface.
            if session.isProcessing { self = .working }
            else if session.stalled { self = .stalled }
            else if session.status == "waiting" { self = .waiting }
            else { self = .idle }
        }
    }

    init(jeanLuc: StatusPoller, kathryn: StatusPoller) {
        self.jeanLuc = jeanLuc
        self.kathryn = kathryn
        self.isEnabled = UserDefaults.standard.bool(forKey: Self.enabledKey)
        // object(forKey:), not double(forKey:) -- the latter returns 0 when the
        // key is absent, which would read as "brightness 0" on a fresh install.
        self.brightness = UserDefaults.standard.object(forKey: Self.brightnessKey) as? Double
            ?? Self.defaultBrightness
        observeSleepWake()
        Task { await self.pushLoop() }
    }

    /// The panel has no idea the host has gone to sleep -- it just stops being
    /// fed. These come from NSWorkspace (system sleep), NOT NotificationCenter.
    private func observeSleepWake() {
        let center = NSWorkspace.shared.notificationCenter
        center.addObserver(forName: NSWorkspace.willSleepNotification,
                           object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.handleSleep() }
        }
        center.addObserver(forName: NSWorkspace.didWakeNotification,
                           object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.handleWake() }
        }
    }

    /// Delivered shortly BEFORE the machine suspends, which is our only chance
    /// to say anything to the panel -- so shorten its blanking timeout to ~1s
    /// and let go of the device. Even if this packet doesn't make it out in
    /// time, `blankAfter` still blanks the panel ~10s later.
    private func handleSleep() {
        isAsleep = true
        if driver.isOpen {
            _ = driver.sendDateTime(brightness: Self.level(brightness),
                                    timeout: Self.blankOnSleep)
        }
        driver.close()
        isConnected = nil
    }

    /// USB may have re-enumerated across the sleep, so don't reuse the old
    /// handle -- push() re-opens on demand, and a failed write drops it anyway.
    private func handleWake() {
        isAsleep = false
        if isEnabled { push() }
    }

    /// Matches StatusPoller's own 2s cadence; each tick just reads whatever the
    /// pollers last published, so a slow/unreachable host never stalls the panel.
    /// The two loops are independent, so a value is at worst one poll interval
    /// stale by the time it reaches the panel.
    private func pushLoop() async {
        while !Task.isCancelled {
            if isEnabled, !isAsleep { push() }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
    }

    private func push() {
        if !driver.isOpen, !driver.open() {
            isConnected = false
            return
        }

        var values: [UInt8: UInt16] = [:]
        stats(for: jeanLuc, gpu: Channel.jeanLucGPU, temp: Channel.jeanLucTemp,
              ram: Channel.jeanLucRAM, power: Channel.jeanLucPower, into: &values)
        stats(for: kathryn, gpu: Channel.kathrynGPU, temp: Channel.kathrynTemp,
              ram: Channel.kathrynRAM, power: Channel.kathrynPower, into: &values)

        // Same figure as the menu bar's aggregate bolt count.
        let generating = (jeanLuc.status?.sessions.processingCount ?? 0)
            + (kathryn.status?.sessions.processingCount ?? 0)
        values[Channel.sessions] = UInt16(clamping: generating)

        values[Channel.jeanLucModel] = residentModel(on: jeanLuc, other: kathryn).rawValue
        values[Channel.kathrynModel] = residentModel(on: kathryn, other: jeanLuc).rawValue

        addSessionRows(into: &values)

        let ok = driver.sendSensors(values)
        // Refreshes the clock and re-arms the blanking timeout. At 2s between
        // pushes against a 10s timeout the panel stays lit while we're feeding
        // it, and blanks on its own once we stop (sleep, crash, quit, unplug).
        _ = driver.sendDateTime(brightness: Self.level(brightness),
                                timeout: Self.blankAfter)
        isConnected = ok
    }

    /// The model family resident on one node, for its MODEL row.
    ///
    /// Uses `effectiveLlamaSwapModels` rather than the host's own report so a
    /// TP=2 cluster model shows under BOTH Sparks -- llama-swap only runs on the
    /// head node, but such a model genuinely occupies both. Several models can
    /// be co-resident (the small-model group is `swap: false`), so this reports
    /// the first, matching the "Serving:" line in the dropdown and widget.
    private func residentModel(on poller: StatusPoller, other: StatusPoller) -> PanelModel {
        guard let host = poller.status?.host else { return .none }
        let models = effectiveLlamaSwapModels(
            own: host.llamaSwap,
            other: other.status?.host.llamaSwap ?? .unavailable
        )
        guard let name = models.compactMap(\.model).first else { return .none }
        return PanelModel(memberID: name)
    }

    /// Fills the two session rows from whichever sessions are most worth
    /// looking at across BOTH Sparks, pooled -- the panel has no room to
    /// dedicate rows per host, and "what is my cluster doing" doesn't care
    /// which box a session happens to sit on.
    ///
    /// `processingFirst` is reused rather than a plain recency sort so an
    /// actively-generating session is never pushed off the panel by a more
    /// recently-touched idle one -- the same ordering the dropdown uses.
    private func addSessionRows(into values: inout [UInt8: UInt16]) {
        let pooled = ((jeanLuc.status?.sessions ?? []) + (kathryn.status?.sessions ?? []))
            .recentEnoughToShow
            .processingFirst

        for (index, channels) in Channel.sessionRows.enumerated() {
            guard index < pooled.count else {
                // Blank the row rather than leaving the previous session's
                // numbers frozen on screen once it ages out.
                values[channels.status] = PanelStatus.none.rawValue
                values[channels.age] = 0
                values[channels.done] = 0
                values[channels.total] = 0
                continue
            }
            let session = pooled[index]
            let todos = session.todos
            values[channels.status] = PanelStatus(session).rawValue
            values[channels.age] = UInt16(clamping: Int(session.updatedSecsAgo / 60))
            values[channels.done] = UInt16(clamping: todos.completed)
            values[channels.total] = UInt16(clamping: todos.pending + todos.inProgress + todos.completed)
        }
    }

    /// An unreachable host reports zeros rather than stale numbers -- a frozen
    /// reading on a glanceable panel is worse than an obviously-dead one.
    private func stats(for poller: StatusPoller, gpu: UInt8, temp: UInt8, ram: UInt8,
                       power: UInt8, into values: inout [UInt8: UInt16]) {
        let host = poller.status?.host
        values[gpu] = UInt16(clamping: Int(host?.gpuUtilPct ?? 0))
        values[temp] = UInt16(clamping: Int(host?.gpuTempC ?? 0))
        values[ram] = UInt16(clamping: Int(host?.ramPct ?? 0))
        values[power] = UInt16(clamping: Int(host?.gpuPowerW ?? 0))
    }
}
