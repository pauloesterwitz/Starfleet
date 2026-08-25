import SwiftUI

/// Compact glanceable content for the floating desktop widget panel (see
/// DesktopWidgetController). Deliberately lighter than ContentView's menu
/// bar dropdown -- no expand/transcript here, just status at a glance.
/// Tuned for a small dedicated external panel (a 3.5" portrait screen) as
/// well as floating over a big one: stats are a 2-column grid instead of one
/// wide row, and each host's session list is height-capped with an internal
/// scroll instead of growing without bound -- on a real small screen there's
/// no free space past the physical edge the way there is floating over a
/// 27"+ display.
struct DesktopWidgetView: View {
    @ObservedObject var jeanLuc: StatusPoller
    @ObservedObject var kathryn: StatusPoller

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HostSection(poller: jeanLuc, otherPoller: kathryn)
            Divider()
            HostSection(poller: kathryn, otherPoller: jeanLuc)
        }
        .padding(9)
        .frame(width: 230, alignment: .leading)
        .fixedSize(horizontal: false, vertical: true)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).strokeBorder(.white.opacity(0.12)))
    }
}

/// Max height of one host's session list before it scrolls internally
/// instead of pushing the widget's total height further down. Sized so two
/// host sections plus stats/header chrome still fit a ~3.5" portrait panel
/// (roughly 320x480pt) without the panel extending past the bottom of the
/// physical screen -- unlike floating over a big display, there's no room
/// to just keep growing.
private let sessionListMaxHeight: CGFloat = 130

private struct HostSection: View {
    @ObservedObject var poller: StatusPoller
    @ObservedObject var otherPoller: StatusPoller

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Circle().fill(poller.indicatorColor).frame(width: 8, height: 8)
                Text(poller.label).font(.caption).bold()
                Spacer()
            }

            if let host = poller.status?.host {
                StatsGrid(host: host)

                // Skipped entirely when there's nothing to report, unlike the fuller
                // menu bar dropdown -- this panel stays deliberately lighter. Folds in
                // a cluster model from the other Spark (see effectiveLlamaSwapModels)
                // since llama-swap only runs on one host but such a model genuinely
                // occupies both.
                let servingModels = effectiveLlamaSwapModels(
                    own: host.llamaSwap,
                    other: otherPoller.status?.host.llamaSwap ?? .unavailable
                )
                if !servingModels.isEmpty {
                    Text(servingModels.map { $0.model ?? "?" }.joined(separator: ", "))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            if let error = poller.lastError {
                if let reauthURL = poller.reauthURL {
                    VStack(alignment: .leading, spacing: 1) {
                        Text("⚠️ Needs Tailscale re-auth")
                            .font(.caption2)
                            .foregroundStyle(.red)
                        OpenURLButton(title: "Open sign-in page", url: reauthURL)
                            .font(.caption2)
                    }
                } else {
                    Text("⚠️ \(error)")
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                }
            } else if let payloadError = poller.status?.error {
                // Informational, not alarming -- the host IS reachable; this just
                // says its opencode DB isn't readable yet (e.g. not installed).
                Text(payloadError)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            } else if let sessions = poller.status?.sessions.recentEnoughToShow, !sessions.isEmpty {
                // Capped + scrollable (sessionListMaxHeight), not left to grow freely
                // -- see that constant's doc comment. processingFirst still keeps
                // active sessions at the top so they're never scrolled out of view.
                ScrollView {
                    VStack(alignment: .leading, spacing: 3) {
                        let claudeCode = sessions.claudeCodeSessions.processingFirst
                        let opencode = sessions.opencodeSessions.processingFirst
                        if !claudeCode.isEmpty {
                            Text("Claude Code").font(.caption2).bold().foregroundStyle(.secondary)
                            ForEach(claudeCode) { session in SessionSummaryRow(session: session) }
                        }
                        if !opencode.isEmpty {
                            Text("OpenCode").font(.caption2).bold().foregroundStyle(.secondary)
                            ForEach(opencode) { session in SessionSummaryRow(session: session) }
                        }
                    }
                }
                .frame(maxHeight: sessionListMaxHeight)
            } else {
                Text(poller.status == nil ? "Connecting…" : "No sessions active")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
    }
}

/// The four host stats (GPU util, temp, power, RAM) as a 2-column grid
/// instead of one wide HStack -- four side-by-side labels either overflow or
/// get squeezed unreadably thin on a narrow portrait panel, while a 2x2 grid
/// stays legible at any panel width this widget realistically runs at.
private struct StatsGrid: View {
    let host: HostStats

    private static let columns = [
        GridItem(.flexible(), alignment: .leading),
        GridItem(.flexible(), alignment: .leading),
    ]

    var body: some View {
        LazyVGrid(columns: Self.columns, alignment: .leading, spacing: 3) {
            if let gpu = host.gpuUtilPct {
                Label("\(Int(gpu))%", systemImage: "cpu")
            }
            if let temp = host.gpuTempC {
                Label("\(Int(temp))°", systemImage: "thermometer.medium")
            }
            if let power = host.gpuPowerW {
                // Distinct icon from the "isProcessing" bolt.fill used lower in this
                // same section, so the two don't get visually conflated.
                Label("\(Int(power))W", systemImage: "powerplug.fill")
            }
            Label("\(Int(host.ramPct))%", systemImage: "memorychip")
        }
        .font(.caption2)
        .foregroundStyle(.secondary)
    }
}

private struct SessionSummaryRow: View {
    let session: SessionStatus

    var body: some View {
        HStack(spacing: 6) {
            Circle().fill(session.badgeColor).frame(width: 6, height: 6)
            Text(session.title).font(.caption2).lineLimit(1)
            if session.isProcessing {
                Image(systemName: "bolt.fill")
                    .font(.caption2)
                    .foregroundStyle(.green)
            }
            Spacer()
            let total = session.todos.pending + session.todos.inProgress + session.todos.completed
            Text("\(session.todos.completed)/\(total)")
                .font(.caption2)
                .foregroundStyle(.secondary)
        }
    }
}
