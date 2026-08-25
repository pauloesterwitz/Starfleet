import AppKit
import Foundation
import LocalAuthentication
import SwiftUI
import UserNotifications

enum StatusPollerError: Error, LocalizedError {
    case sshFailed(Int32, String)
    case timedOut
    case tailscaleReauth(String?)

    var errorDescription: String? {
        switch self {
        case .sshFailed(let code, let stderr): return "ssh exited \(code): \(stderr)"
        case .timedOut: return "ssh call timed out"
        case .tailscaleReauth(let url):
            return url != nil ? "needs Tailscale re-auth" : "needs Tailscale re-auth (check mode)"
        }
    }
}

@MainActor
final class StatusPoller: ObservableObject {
    @Published var status: StatusPayload?
    @Published var lastError: String?
    @Published var reauthURL: URL? // set only for StatusPollerError.tailscaleReauth, so the UI can render a real Link

    var indicatorColor: Color {
        if lastError != nil { return .gray }
        let anyStalled = status?.sessions.contains(where: { $0.stalled }) ?? false
        return anyStalled ? .red : .green
    }

    let label: String // display identity, e.g. "Jean-Luc" / "Kathryn"

    private let host: String
    private let remoteScript = "~/bin/opencode-status.py"
    // 2s, not the original 5s, so the external USB panel reads as genuinely
    // live (pushing to it faster than this would just re-send stale numbers).
    // Each poll is one exec over an already-established ControlMaster
    // connection -- no TCP/auth handshake -- so ~1 exec/sec across both Sparks
    // is cheap. The menu bar and desktop widget get the faster cadence too.
    private let pollIntervalNanos: UInt64 = 2_000_000_000
    private let timeoutSeconds: TimeInterval = 8
    private var stalledSessionIDs: Set<String> = [] // sessions we've already notified about, while still stalled
    private var reauthPromptedURL: URL? // the reauth URL we've already Touch-ID-prompted for, so we don't re-prompt every 5s poll

    init(host: String, label: String) {
        self.host = host
        self.label = label
        Task { await self.loop() }
    }

    private func loop() async {
        while !Task.isCancelled {
            await poll()
            try? await Task.sleep(nanoseconds: pollIntervalNanos)
        }
    }

    private func poll() async {
        do {
            let data = try await Self.runSSH(host: host, arguments: [remoteScript], timeout: timeoutSeconds)
            let payload = try JSONDecoder().decode(StatusPayload.self, from: data)
            notifyOnNewStalls(payload)
            status = payload
            // NOT payload.error: SSH succeeded and the JSON decoded, so the host is
            // reachable regardless of what the script itself reports (e.g. Kathryn's
            // "unable to open database file" before opencode is set up there) --
            // `lastError`/`indicatorColor` mean "can we reach this host", not "does
            // its opencode DB exist yet". The payload's own error is still shown as
            // informational text in the UI via `status?.error`.
            lastError = nil
            reauthURL = nil
            reauthPromptedURL = nil
        } catch let StatusPollerError.tailscaleReauth(urlString) {
            lastError = StatusPollerError.tailscaleReauth(urlString).localizedDescription
            let url = urlString.flatMap(URL.init(string:))
            reauthURL = url
            if let url, reauthPromptedURL != url {
                reauthPromptedURL = url
                await promptTouchIDAndOpen(url)
            }
        } catch {
            lastError = error.localizedDescription
            reauthURL = nil
            reauthPromptedURL = nil
        }
    }

    /// Touch ID gate before auto-opening the Tailscale re-auth page, so the app
    /// doesn't just pop a browser window unprompted -- one fingerprint tap
    /// replaces "notice the error, find the button, click it". Biometrics only
    /// (no passcode fallback), matching what was actually asked for; silently
    /// no-ops -- leaving the manual "Open sign-in page" button as the fallback
    /// -- if this Mac has no Touch ID enrolled, or the user cancels/fails it.
    private func promptTouchIDAndOpen(_ url: URL) async {
        let context = LAContext()
        guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: nil) else { return }
        do {
            let success = try await context.evaluatePolicy(
                .deviceOwnerAuthenticationWithBiometrics,
                localizedReason: "open the Tailscale re-auth page for \(label)"
            )
            if success { NSWorkspace.shared.open(url) }
        } catch {
            // Cancelled or failed -- manual button remains available.
        }
    }

    /// On-demand only (not part of the 5s poll loop) -- fetches the tail of a
    /// session's transcript so you have context before deciding whether to respond.
    /// `tool` picks which side of opencode-export-tail.py runs -- opencode sessions
    /// go through `opencode export`, Claude Code sessions parse the JSONL transcript
    /// directly (there's no export command for those); passing the wrong one back
    /// is exactly what used to surface as a bare "export failed" error.
    func fetchSessionDetail(id: String, tool: String) async throws -> SessionDetail {
        let data = try await Self.runSSH(host: host, arguments: ["~/bin/opencode-export-tail.py", id, "5", tool], timeout: 15)
        return try JSONDecoder().decode(SessionDetail.self, from: data)
    }

    /// Fires one notification per session the moment it becomes stalled, not on every
    /// poll while it stays that way; re-arms once the session clears.
    private func notifyOnNewStalls(_ payload: StatusPayload) {
        var stillStalled: Set<String> = []
        for session in payload.sessions where session.stalled {
            stillStalled.insert(session.id)
            guard !stalledSessionIDs.contains(session.id) else { continue }
            let content = UNMutableNotificationContent()
            content.title = "opencode needs you"
            content.body = "\(label): \(session.title)"
            content.sound = .default
            UNUserNotificationCenter.current().add(
                UNNotificationRequest(identifier: session.id, content: content, trigger: nil)
            )
        }
        stalledSessionIDs = stillStalled
    }

    /// Tailscale SSH "check" mode answers the connection with a banner asking for a
    /// browser re-auth, then stalls until the socket times out. Recognise that exact
    /// state so the UI shows an actionable message (and the auth URL) instead of a
    /// bare "timed out". The banner lands on stderr *before* the stall, so it's
    /// readable even though we terminate the hung process.
    private nonisolated static func tailscaleReauth(from stderr: String) -> StatusPollerError? {
        guard stderr.contains("Tailscale SSH requires an additional check")
            || stderr.contains("login.tailscale.com") else { return nil }
        let url = stderr.split(whereSeparator: \.isWhitespace)
            .map(String.init)
            .first { $0.hasPrefix("https://login.tailscale.com/") }
        return .tailscaleReauth(url)
    }

    /// nonisolated so a hung/slow ssh call (blocking pipe reads, not suspending
    /// ones) doesn't tie up the MainActor executor that BOTH pollers' loops share
    /// -- otherwise one host stalling (e.g. mid Tailscale-check-mode-timeout)
    /// would block the other host's polling too.
    private nonisolated static func runSSH(host: String, arguments: [String], timeout: TimeInterval) async throws -> Data {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        // StrictHostKeyChecking=accept-new: Tailscale SSH's "check mode" proxy can
        // present a host key under a hostname string we haven't trusted yet (e.g.
        // after a host's network path changes and it needs a fresh re-auth) --
        // without this, BatchMode=yes turns that into a bare "Host key verification
        // failed" / sshFailed(255, ...) *before* the connection ever reaches the
        // "Tailscale SSH requires an additional check" banner, so tailscaleReauth(from:)
        // never gets a chance to classify it (observed live: Kathryn's network
        // dropped, needed re-auth, and surfaced as a cryptic ssh-exited-255 instead
        // of the proper re-auth UI). Safe here: exactly two hardcoded, personally-
        // owned hosts, not arbitrary remote input.
        process.arguments = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5", "-o", "StrictHostKeyChecking=accept-new", host] + arguments
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        try process.run()

        // Enforce the timeout by terminating the process; the blocking stdout read
        // below then unblocks on EOF. Keeping one reader per pipe avoids the data
        // race two concurrent stderr readers would otherwise hit.
        let timeoutTask = Task {
            try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
            if process.isRunning { process.terminate() }
        }
        defer { timeoutTask.cancel() }

        let outData = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()

        if process.terminationStatus == 0 { return outData }

        let msg = String(data: errData, encoding: .utf8) ?? ""
        if let reauth = tailscaleReauth(from: msg) { throw reauth }
        // A terminate() from our own timeout surfaces as an uncaught SIGTERM.
        if process.terminationReason == .uncaughtSignal { throw StatusPollerError.timedOut }
        throw StatusPollerError.sshFailed(process.terminationStatus, msg)
    }
}
