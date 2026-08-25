import AppKit
import SwiftUI

/// Owns a borderless, always-on-top NSPanel showing DesktopWidgetView, as an
/// alternative to clicking the menu bar item for glanceable status. Not
/// WidgetKit: real desktop widgets only refresh a few times an hour, which
/// can't show the 5s-live poll / ~90s stall detection this app is built
/// around -- a floating panel gets the same live data the menu bar does.
@MainActor
final class DesktopWidgetController: ObservableObject {
    @Published var isVisible: Bool {
        didSet {
            UserDefaults.standard.set(isVisible, forKey: Self.visibleKey)
            isVisible ? showPanel() : panel?.orderOut(nil)
        }
    }

    private static let visibleKey = "desktopWidgetVisible"
    private let jeanLuc: StatusPoller
    private let kathryn: StatusPoller
    private var panel: NSPanel?
    private var hosting: NSHostingView<DesktopWidgetView>?

    init(jeanLuc: StatusPoller, kathryn: StatusPoller) {
        self.jeanLuc = jeanLuc
        self.kathryn = kathryn
        self.isVisible = UserDefaults.standard.bool(forKey: Self.visibleKey)
        if isVisible { showPanel() }
        Task { await self.resizeLoop() }
    }

    private func showPanel() {
        if panel == nil {
            let panel = NSPanel(
                contentRect: NSRect(x: 0, y: 0, width: 230, height: 160),
                styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            panel.isFloatingPanel = true
            panel.level = .floating
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            panel.isOpaque = false
            panel.backgroundColor = .clear
            panel.hasShadow = true
            panel.isMovableByWindowBackground = true
            panel.isReleasedWhenClosed = false
            panel.hidesOnDeactivate = false

            let hosting = NSHostingView(rootView: DesktopWidgetView(jeanLuc: jeanLuc, kathryn: kathryn))
            // .preferredContentSize alone reports fittingSize/intrinsicContentSize as a
            // degenerate (0, 0) -- verified in isolation. .intrinsicContentSize is what
            // actually makes those properties reflect the real SwiftUI content size.
            hosting.sizingOptions = [.intrinsicContentSize, .preferredContentSize]
            panel.contentView = hosting
            self.hosting = hosting

            // Renamed from "OpencodeMonitorDesktopWidget" -- the panel's content
            // roughly doubled in height (two host sections now), and this call
            // restores a previously-saved frame (origin AND size) immediately,
            // which would otherwise clip the second section until the next poll
            // re-triggers preferredContentSize. Costs the one saved drag position.
            panel.setFrameAutosaveName("OpencodeMonitorDesktopWidgetV2")
            if panel.frame.origin == .zero, let screen = NSScreen.main {
                let margin: CGFloat = 20
                let x = screen.visibleFrame.maxX - panel.frame.width - margin
                let y = screen.visibleFrame.maxY - panel.frame.height - margin
                panel.setFrameOrigin(NSPoint(x: x, y: y))
            }
            self.panel = panel
        }
        panel?.orderFrontRegardless()
        // Deferred, not called inline: showPanel() can run from inside init() (via
        // OpencodeMonitorApp.init() -> DesktopWidgetController.init()), before
        // SwiftUI's own update machinery is running yet. A synchronous, display-
        // forcing resize at that point crashed with an AttributeGraph precondition
        // failure (SIGABRT) -- dispatching to the next run loop turn avoids it,
        // while still resizing quickly rather than waiting up to 1s for resizeLoop.
        DispatchQueue.main.async { [weak self] in self?.resizeToFitContent() }
    }

    /// `NSHostingView.sizingOptions = [.preferredContentSize]` sizes the panel
    /// correctly at first layout, but doesn't reliably re-trigger a resize on an
    /// already-visible panel when the hosted SwiftUI content's ideal size changes
    /// later (observed: stayed at the initial height across several 5s polls
    /// despite a session count that clearly needed more room). Poll `fittingSize`
    /// explicitly instead, same cadence as the status poll, so the widget actually
    /// grows/shrinks as each host's session count changes.
    private func resizeLoop() async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 1_000_000_000)
            if isVisible { resizeToFitContent() }
        }
    }

    private func resizeToFitContent() {
        guard let panel, let hosting else { return }
        let newSize = hosting.fittingSize
        // Never collapse to a degenerate size -- fittingSize returning (0, 0) is a
        // real failure mode observed while debugging this (wrong sizingOptions),
        // and got the panel stuck invisible since nothing ever looked "changed" once
        // it was already 0.
        guard newSize.width > 1, newSize.height > 1 else { return }
        guard abs(newSize.height - panel.frame.height) > 0.5 || abs(newSize.width - panel.frame.width) > 0.5 else { return }
        var frame = panel.frame
        frame.origin.y += frame.height - newSize.height // keep the top edge fixed (AppKit's origin is bottom-left)
        frame.size = newSize
        // display: false -- let AppKit coalesce the redraw on its own run loop turn
        // rather than forcing an immediate synchronous layout pass.
        panel.setFrame(frame, display: false)
    }
}
