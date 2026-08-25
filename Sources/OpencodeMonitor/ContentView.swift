import ServiceManagement
import SwiftUI

struct ContentView: View {
    @ObservedObject var jeanLuc: StatusPoller
    @ObservedObject var kathryn: StatusPoller
    @ObservedObject var widgetController: DesktopWidgetController
    @ObservedObject var panelController: PanelController
    @State private var launchAtLogin = SMAppService.mainApp.status == .enabled

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Capped and scrollable, not left to grow freely -- two hosts' worth of
            // Claude Code + opencode sessions (plus any expanded transcript) can
            // easily outgrow screen height; a fixed max keeps the popover itself a
            // sane, predictable size and the "Quit" button always reachable below.
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    HostSection(poller: jeanLuc, otherPoller: kathryn)
                    Divider()
                    HostSection(poller: kathryn, otherPoller: jeanLuc)
                }
            }
            .frame(maxHeight: 420)

            Divider()
            Toggle("Show desktop widget", isOn: $widgetController.isVisible)
                .toggleStyle(.checkbox)
                .font(.caption)

            HStack(spacing: 5) {
                Toggle("Send to USB panel", isOn: $panelController.isEnabled)
                    .toggleStyle(.checkbox)
                    .font(.caption)
                // Only meaningful once we've actually tried a push, so stays
                // blank until then rather than claiming the panel is missing.
                if panelController.isEnabled, let connected = panelController.isConnected {
                    Text(connected ? "connected" : "not found")
                        .font(.caption2)
                        .foregroundStyle(connected ? .green : .secondary)
                }
            }

            // Hidden unless the panel is on -- a backlight slider for a device
            // you aren't driving is just noise in an already-busy dropdown.
            if panelController.isEnabled {
                HStack(spacing: 6) {
                    Image(systemName: "sun.min.fill")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                    Slider(value: $panelController.brightness, in: 1...100)
                        .controlSize(.mini)
                    // Fixed width, trailing-aligned: lets the readout go 1->100
                    // without the slider resizing under the cursor mid-drag.
                    Text("\(Int(panelController.brightness))")
                        .font(.caption2)
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                        .frame(width: 26, alignment: .trailing)
                }
                .padding(.leading, 18) // line up under the checkbox's label
            }

            Toggle("Launch at Login", isOn: $launchAtLogin)
                .toggleStyle(.checkbox)
                .font(.caption)
                .onChange(of: launchAtLogin) { enabled in
                    do {
                        if enabled { try SMAppService.mainApp.register() }
                        else { try SMAppService.mainApp.unregister() }
                    } catch {
                        launchAtLogin.toggle() // revert on failure
                    }
                }

            Button("Quit") {
                NSApplication.shared.terminate(nil)
            }
        }
        .padding(12)
        .frame(width: 320)
    }
}

private struct HostSection: View {
    @ObservedObject var poller: StatusPoller
    @ObservedObject var otherPoller: StatusPoller

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(poller.label).font(.subheadline).bold()

            if let error = poller.lastError {
                if let reauthURL = poller.reauthURL {
                    VStack(alignment: .leading, spacing: 2) {
                        Text("⚠️ needs Tailscale re-auth")
                            .foregroundStyle(.red)
                            .font(.caption)
                        OpenURLButton(title: "Open sign-in page", url: reauthURL)
                            .font(.caption)
                    }
                } else {
                    Text("⚠️ \(error)")
                        .foregroundStyle(.red)
                        .font(.caption)
                        .lineLimit(2)
                }
            }

            if let status = poller.status {
                if let payloadError = status.error {
                    // Informational, not alarming -- the host IS reachable (that's
                    // what got us this payload); this just says its opencode DB
                    // isn't readable yet (e.g. not installed on a fresh box).
                    Text("\(payloadError)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if status.sessions.recentEnoughToShow.isEmpty {
                    Text("No sessions active in the last 24h")
                        .foregroundStyle(.secondary)
                } else {
                    let recent = status.sessions.recentEnoughToShow
                    let claudeCode = recent.claudeCodeSessions.processingFirst
                    let opencode = recent.opencodeSessions.processingFirst
                    if !claudeCode.isEmpty {
                        Text("Claude Code").font(.caption2).bold().foregroundStyle(.secondary)
                        ForEach(claudeCode) { session in
                            SessionRow(session: session, poller: poller)
                        }
                    }
                    if !opencode.isEmpty {
                        Text("OpenCode").font(.caption2).bold().foregroundStyle(.secondary)
                        ForEach(opencode) { session in
                            SessionRow(session: session, poller: poller)
                        }
                    }
                }
                Divider()
                HostStatsRow(host: status.host, otherLlamaSwap: otherPoller.status?.host.llamaSwap ?? .unavailable)
            } else {
                Text("Waiting for first poll…")
                    .foregroundStyle(.secondary)
            }
        }
    }
}

private struct SessionRow: View {
    let session: SessionStatus
    let poller: StatusPoller

    @State private var expanded = false
    @State private var detail: SessionDetail?
    @State private var loading = false
    @State private var loadError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Button {
                expanded.toggle()
                if expanded && !loading { loadDetail() } // re-fetch every expand so it doesn't go stale
            } label: {
                HStack(spacing: 6) {
                    Circle().fill(session.badgeColor).frame(width: 6, height: 6)
                    Text(session.title).font(.headline).lineLimit(1)
                    if session.isProcessing {
                        Image(systemName: "bolt.fill")
                            .font(.caption2)
                            .foregroundStyle(.green)
                            .help("Currently being processed")
                    }
                    Spacer()
                    Image(systemName: expanded ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
            .buttonStyle(.plain)

            let totalTodos = session.todos.pending + session.todos.inProgress + session.todos.completed
            Text("\(session.status) · updated \(Int(session.updatedSecsAgo))s ago · todos \(session.todos.completed)/\(totalTodos)")
                .font(.caption)
                .foregroundStyle(.secondary)

            if expanded {
                if loading {
                    ProgressView().controlSize(.small).padding(.leading, 12)
                } else if let loadError {
                    Text("⚠️ \(loadError)").font(.caption2).foregroundStyle(.red).padding(.leading, 12)
                } else if let messages = detail?.messages {
                    SessionTranscriptView(messages: messages, tool: session.tool)
                }
            }
        }
        .padding(.vertical, 2)
        Divider()
    }

    private func loadDetail() {
        loading = true
        loadError = nil
        Task {
            do {
                let result = try await poller.fetchSessionDetail(id: session.id, tool: session.tool)
                if let err = result.error {
                    loadError = err
                } else {
                    detail = result
                }
            } catch {
                loadError = error.localizedDescription
            }
            loading = false
        }
    }
}

private struct SessionTranscriptView: View {
    let messages: [DetailMessage]
    let tool: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if messages.isEmpty {
                Text("No messages yet").font(.caption2).foregroundStyle(.secondary)
            }
            ForEach(messages) { message in
                VStack(alignment: .leading, spacing: 1) {
                    Text(message.role == "user" ? "You" : (tool == "claude-code" ? "Claude Code" : "opencode"))
                        .font(.caption2).bold()
                    ForEach(message.parts) { part in
                        partView(part)
                    }
                }
            }
        }
        .padding(.leading, 12)
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private func partView(_ part: DetailPart) -> some View {
        switch part.type {
        case "text":
            Text(part.text ?? "").font(.caption2).lineLimit(6)
        case "tool":
            let titleSuffix = (part.title?.isEmpty == false) ? ": \(part.title!)" : ""
            Text("🔧 \(part.tool ?? "tool") — \(part.status ?? "?")\(titleSuffix)")
                .font(.caption2)
                .foregroundStyle(.secondary)
        case "file":
            Text("📄 \(part.filename ?? "")").font(.caption2).foregroundStyle(.secondary)
        default:
            EmptyView()
        }
    }
}

private struct HostStatsRow: View {
    let host: HostStats
    // The other Starfleet member's llama-swap report -- llama-swap only runs on
    // one host today, so a both-Sparks cluster model it reports needs folding
    // into THIS host's line too, since it's genuinely running here as well.
    let otherLlamaSwap: LlamaSwapStatus

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("RAM \(host.ramUsedGB, specifier: "%.1f")/\(host.ramTotalGB, specifier: "%.1f") GB (\(host.ramPct, specifier: "%.0f")%)")
                .font(.caption)
            if let util = host.gpuUtilPct, let temp = host.gpuTempC, let power = host.gpuPowerW {
                Text("GPU \(util, specifier: "%.0f")% · \(temp, specifier: "%.0f")°C · \(power, specifier: "%.0f")W")
                    .font(.caption)
            }
            // Shown even when this host's OWN llama-swap isn't reachable, as long
            // as there's a cluster model to report (borrowed from the other
            // host) -- otherwise omitted entirely, same treatment as an absent GPU.
            if host.llamaSwap.reachable || !effectiveModels.isEmpty {
                Text(servingSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private var effectiveModels: [LlamaSwapModel] {
        effectiveLlamaSwapModels(own: host.llamaSwap, other: otherLlamaSwap)
    }

    private var servingSummary: String {
        let models = effectiveModels
        if models.isEmpty { return "llama-swap: no models loaded" }
        let names = models.map { m -> String in
            let name = m.model ?? "?"
            guard let state = m.state, state != "ready" else { return name }
            return "\(name) (\(state))"
        }
        return "Serving: \(names.joined(separator: ", "))"
    }
}
